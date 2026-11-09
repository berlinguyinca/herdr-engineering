"""Explicit checkpoint prewarming and isolated, serial background refreshes."""
from __future__ import annotations

import logging
import math
import threading

from . import bridge
from .heap_release import release_heap_if_enabled
from .repo import ControlPlaneRepo

log = logging.getLogger(__name__)
REFRESH_INTERVAL = 300.0


class CheckpointNotPublished(RuntimeError):
    """Recovery completed without publishing a durable projection checkpoint."""


def refresh_checkpoint(dsn, *, host_aliases=None, inspector_limits=None, lock_wait=0.0):
    """Build the same projection policy as the web host; always release the source."""
    repo = ControlPlaneRepo(host_aliases=host_aliases, inspector_limits=inspector_limits)
    options = {'checkpoint_lock_wait': lock_wait} if lock_wait else {}
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


class ProjectionRefresh:
    """One refresh at a time, with an interruptible delay after each pass.

    The dedicated thread owns each source and its connection. Stop drains the
    active pass so its source is closed before returning; no new pass can start.
    """

    def __init__(self, dsn, *, host_aliases=None, inspector_limits=None,
                 interval=REFRESH_INTERVAL, heap_release=True):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('checkpoint interval must be positive and finite')
        self._dsn = dsn
        self._policy = dict(host_aliases=host_aliases, inspector_limits=inspector_limits)
        self._interval = interval
        self._heap_release = heap_release
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='projection-refresh', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            try:
                refresh_checkpoint(self._dsn, **self._policy)
            except Exception as exc:
                log.warning('projection checkpoint refresh failed (%s)', type(exc).__name__)
            # The pass's repo and replay rows are unreachable now; hand the fragmented heap back to the OS.
            release_heap_if_enabled(self._heap_release)
            if self._stop.wait(self._interval):
                break
