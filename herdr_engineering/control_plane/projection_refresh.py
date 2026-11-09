"""Explicit checkpoint prewarming and isolated, serial background refreshes."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time

from . import bridge
from .heap_release import release_heap_if_enabled
from .repo import ControlPlaneRepo

log = logging.getLogger(__name__)
REFRESH_INTERVAL = 300.0
_URL = re.compile(r'\S*://\S*')


def _safe_message(exc, limit=500):
    """Exception text for the log, with anything URL-shaped (a DSN) redacted."""
    return _URL.sub('<url>', str(exc))[:limit]


class CheckpointNotPublished(RuntimeError):
    """Recovery completed without publishing a durable projection checkpoint."""


def refresh_checkpoint(dsn, *, host_aliases=None, inspector_limits=None, lock_wait=0.0,
                       checkpoint_budgets=None):
    """Build the same projection policy as the web host; always release the source."""
    repo = ControlPlaneRepo(host_aliases=host_aliases, inspector_limits=inspector_limits)
    options = {'checkpoint_lock_wait': lock_wait} if lock_wait else {}
    if checkpoint_budgets is not None:
        options['checkpoint_budgets'] = checkpoint_budgets
    source = bridge.PostgresSource(dsn=dsn, checkpoint_publish=True, **options)
    try:
        counts = bridge.load_into(repo, source)
        # Only known modes and numeric counters cross the CLI/log boundary.
        info = source.recovery_info or {}
        # The publishing recovery path sets this receipt only after publish()
        # succeeds. Legacy replay can load successfully without creating a CP.
        if (info.get('mode') not in {'checkpoint', 'full_replay'}
                or type(info.get('frontier')) is not int or info['frontier'] < 0
                or 'reason' in info):
            raise CheckpointNotPublished('projection checkpoint was not published')
        recovery = {key: value for key, value in info.items()
                    if key in {'frontier', 'tail_events', 'event_rows_scanned', 'elapsed_ms'}
                    and type(value) in (int, float) and math.isfinite(value)}
        if info.get('mode') in {'checkpoint', 'full_replay'}:
            recovery['mode'] = info['mode']
        return {'counts': {key: value for key, value in counts.items()
                           if key in {'missions', 'sessions', 'services', 'hosts', 'events'}
                           and type(value) is int}, 'recovery_info': recovery}
    finally:
        source.close()


def checkpoint_is_current(dsn, *, host_aliases=None, inspector_limits=None):
    """True only when a published checkpoint for this code and policy already
    sits at the current projection position. Any doubt (no checkpoint, schema
    missing, query failure) returns False so the caller does the full pass; a
    skip is purely an optimisation and a concurrent append just means the next
    pass runs."""
    async def check():
        import asyncpg

        from .projection_checkpoint import code_fingerprint, policy_fingerprint
        from .projection_store import ProjectionStore
        repo = ControlPlaneRepo(host_aliases=host_aliases, inspector_limits=inspector_limits)
        conn = await asyncpg.connect(dsn)
        try:
            store = ProjectionStore(conn)
            if not await store.available():
                return False
            candidates = await store.candidates(code_fingerprint(), policy_fingerprint(repo))
            if not candidates:
                return False
            current = await conn.fetchval(
                'SELECT GREATEST(replay_floor,COALESCE((SELECT max(position) FROM projection_event_changes),0)) '
                'FROM projection_checkpoint_control WHERE singleton')
            return current is not None and any(row['frontier'] == current for row in candidates)
        finally:
            await conn.close()
    try:
        return asyncio.run(check())
    except Exception as exc:
        log.info('projection refresh: currency check failed (%s); doing the pass', type(exc).__name__)
        return False


class ProjectionRefresh:
    """One refresh at a time, with an interruptible delay after each pass.

    The dedicated thread owns each source and its connection. Stop drains the
    active pass so its source is closed before returning; no new pass can start.
    """

    def __init__(self, dsn, *, host_aliases=None, inspector_limits=None,
                 interval=REFRESH_INTERVAL, checkpoint_budgets=None, heap_release=True):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('checkpoint interval must be positive and finite')
        self._dsn = dsn
        self._policy = dict(host_aliases=host_aliases, inspector_limits=inspector_limits,
                            checkpoint_budgets=checkpoint_budgets)
        self._interval = interval
        self._heap_release = heap_release
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='projection-refresh', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()

    def _pass(self):
        started = time.monotonic()
        outcome = 'failed'
        try:
            if checkpoint_is_current(self._dsn, **self._policy):
                outcome = 'skipped'
            else:
                refresh_checkpoint(self._dsn, **self._policy)
                outcome = 'published'
        except Exception as exc:
            log.warning('projection checkpoint refresh failed (%s): %s',
                        type(exc).__name__, _safe_message(exc))
        log.info('projection refresh: %s in %.1fs', outcome, time.monotonic() - started)

    def _run(self):
        while not self._stop.is_set():
            self._pass()
            # The pass's repo and replay rows are unreachable now; hand the fragmented heap back to the OS.
            release_heap_if_enabled(self._heap_release)
            if self._stop.wait(self._interval):
                break
