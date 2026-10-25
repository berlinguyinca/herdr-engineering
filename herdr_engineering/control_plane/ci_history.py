"""Memory-only, bounded CI backfill; provider runs remain execution authority.

A watermark describes only a validated newest-first prefix retained here. Offset
pagination is revalidated against the head before continuation, so a changed head
restarts the scan instead of quietly skipping runs. Backfill never writes reports.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from .ci_observability import _Measurements
from .redact import redact_payload


def _date(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(UTC) if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _key(row):
    value = row.get('run_number')
    return str(value) if isinstance(value, (int, str)) and str(value).isdigit() and int(value) > 0 else None


def _coverage():
    return dict(complete_since=None, exhausted=False, truncated=False, error=None, checked_at=None)


class CIHistory:
    def __init__(self, *, provider_factory, interval=30, budget=120, now=None,
                 wall_now=None, repo_limit=500, page_limit=60, run_limit=3000,
                 total_limit=20000, byte_limit=16 * 1024 * 1024, pages_per_refresh=600):
        self._measurements = _Measurements()
        self._factory = provider_factory
        self._interval = max(5, interval)
        self._budget = budget
        self._now = now or time.monotonic
        self._wall_now = wall_now or (lambda: datetime.now(UTC))
        self._repo_limit = min(500, repo_limit)
        self._page_limit = min(60, page_limit)
        self._run_limit = min(3000, run_limit)
        self._total_limit = min(20000, total_limit)
        self._byte_limit = min(16 * 1024 * 1024, byte_limit)
        self._pages_per_refresh = pages_per_refresh
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._states = {}
        self._run_count = 0
        self._byte_count = 0
        self._provider_url = None
        self._cursor = 0
        self._last_check = None
        self._view = dict(available=False, status='not_configured', refreshing=False,
                          stale=False, error=None, repos=[], pipelines=[], coverage={}, scope={
                              'memory_only': True, 'refetched_on_restart': True,
                              'inventory_complete': False, 'outcome_refresh_seconds': 300,
                              'limits': {'repositories': self._repo_limit, 'pages_per_repository': self._page_limit,
                                         'runs_per_repository': self._run_limit, 'runs': self._total_limit,
                                         'bytes': self._byte_limit}})

    def _measurement_snapshot(self):
        return self._measurements.snapshot()

    def snapshot(self, *, start=True):
        if start:
            self.start()
        with self._lock:
            result = copy.deepcopy(self._view)
        if self._last_check is not None and self._now() - self._last_check > self._interval * 3:
            result['stale'] = True
        self._measurements.observe(result, len(result['pipelines']), result['stale'])
        return result

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._view.update(refreshing=True, status='syncing')
                self._thread = threading.Thread(target=self._loop, name='herdr-ci-history', daemon=True)
                self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._interval)

    def _update(self, **values):
        with self._lock:
            self._view.update(values)

    def _inventory(self, provider, started):
        repos, seen = [], set()
        complete, error = False, None
        for page in range(1, 12):
            if self._stop.is_set() or self._now() - started >= self._budget:
                error = 'Repository discovery budget exceeded.'
                break
            self._measurements.provider_call()
            batch = provider.repos(page=page, per_page=50)
            unique = []
            for row in batch:
                rid = str(row.get('id', ''))
                if rid and rid not in seen:
                    seen.add(rid)
                    unique.append(redact_payload({**row, 'provider_url': self._provider_url}))
            room = self._repo_limit - len(repos)
            repos.extend(unique[:room])
            if len(unique) > room or len(repos) >= self._repo_limit:
                error = 'Repository inventory reached its safety limit.'
                break
            # Woodpecker v3 may return its entire snapshot, ignoring pagination.
            if getattr(provider, 'repos_are_snapshot', False) or len(batch) != 50:
                complete = True
                break
            if not unique:
                error = 'Repository pagination repeated; inventory may be incomplete.'
                break
        return repos, complete, error

    @staticmethod
    def _new_state():
        return dict(rows={}, sizes={}, seen=set(), page=1, head=None, tail=None, done=False,
                    verified_at=None, validated_this_cycle=False, signatures={}, mutable_pages=set(),
                    pending_coverage=None, pending_at=None,
                    coverage=_coverage())

    def _publish(self, repos, inventory_complete, inventory_error):
        coverage = {str(r['id']): copy.deepcopy(self._states[str(r['id'])]['coverage']) for r in repos}
        for rid, value in coverage.items():
            if not self._states[rid]['validated_this_cycle']:
                value.update(complete_since=None, exhausted=False,
                             error=value['error'] or 'CI history is awaiting head revalidation.')
        rows = [row for repo in repos for row in self._states[str(repo['id'])]['rows'].values()]
        partial = bool(inventory_error) or any(c['error'] or c['truncated'] for c in coverage.values())
        pending = any(not state['done'] for state in self._states.values())
        old_outcomes = any(not state['done'] and set(state['rows']) - state['seen']
                           for state in self._states.values())
        with self._lock:
            scope = {**self._view['scope'], 'inventory_complete': inventory_complete,
                     'inventory_error': inventory_error}
        self._update(available=True, status='partial' if partial else 'syncing' if pending else 'connected',
                     stale=partial or old_outcomes, error=None, repos=repos, pipelines=rows, coverage=coverage, scope=scope)

    def refresh(self):
        if not self._refresh_lock.acquire(blocking=False):
            return
        started = self._now()
        with self._lock:
            # Retain the visible rows while withholding aging coverage evidence.
            coverage = copy.deepcopy(self._view['coverage'])
            for value in coverage.values():
                value.update(complete_since=None, exhausted=False)
            self._view.update(refreshing=True, stale=bool(coverage), coverage=coverage)
        try:
            self._collect()
        except Exception as exc:
            from .ci_monitor import _error
            status, message = _error(exc)
            self._update(available=False, status=status, stale=bool(self._states), error=message)
        finally:
            self._update(refreshing=False)
            self._measurements.refreshed(self._now() - started)
            self._refresh_lock.release()

    def _collect(self):
        started = self._now()
        provider = self._factory()
        if not provider.available:
            self._update(available=False, status='not_configured', stale=bool(self._states),
                         error='Configure a server-side Woodpecker token to connect CI history.')
            return
        provider_url = provider.base_url.rstrip('/')
        if self._provider_url != provider_url:
            self._states = {}
            self._provider_url = provider_url
        repos, complete, inventory_error = self._inventory(provider, started)
        self._states = {str(repo['id']): self._states.get(str(repo['id']), self._new_state()) for repo in repos}
        self._run_count = sum(len(s['rows']) for s in self._states.values())
        self._byte_count = sum(sum(s['sizes'].values()) for s in self._states.values())
        for state in self._states.values():
            state['validated_this_cycle'] = False
        if not repos:
            self._publish(repos, complete, inventory_error)
            self._last_check = self._now()
            return
        order = repos[self._cursor:] + repos[:self._cursor]
        queue = deque(order)
        prepared = set()
        pages = 0
        while queue and pages < self._pages_per_refresh and not self._stop.is_set():
            if self._now() - started >= self._budget:
                break
            repo = queue.popleft()
            rid = str(repo['id'])
            state = self._states[rid]
            try:
                # Existing offset cursors are valid only while the head is unchanged.
                head = None
                if rid not in prepared:
                    prepared.add(rid)
                    head = self._read(provider, repo, 1)
                    signature = [(_key(r), r.get('created_at')) for r in head]
                    unchanged = signature == state['head']
                    if unchanged:
                        for page in sorted(state['mutable_pages'] - {1}):
                            self._recheck(provider, repo, state, page, started)
                    if unchanged and state['pending_coverage'] is not None and self._now() - state['pending_at'] < 300:
                        # A completed scan can outlive its read budget. Validate
                        # its unchanged head instead of repeating the backfill.
                        state['coverage'] = state['pending_coverage']
                        state['pending_coverage'] = None
                        state['verified_at'] = self._now()
                    fresh = state['verified_at'] is not None and self._now() - state['verified_at'] < 300
                    if state['done'] and unchanged and fresh and not state['coverage']['error']:
                        for row in head:
                            if not self._retain(state, row):
                                state['coverage'].update(truncated=True, complete_since=None, exhausted=False)
                        state['coverage']['checked_at'] = self._wall_now().isoformat()
                        state['validated_this_cycle'] = True
                        pages += 1
                        self._cursor = (repos.index(repo) + 1) % len(repos)
                        continue
                    if state['done'] or not unchanged:
                        retained = state if unchanged else None
                        if retained is None:
                            self._run_count -= len(state['rows'])
                            self._byte_count -= sum(state['sizes'].values())
                        state = self._states[rid] = self._new_state()
                        if retained is not None:
                            state['rows'] = retained['rows']
                            state['sizes'] = retained['sizes']
                    if state['page'] > 1:
                        # Update mutable outcomes without weakening the validated prefix.
                        for row in head:
                            if not self._retain(state, row):
                                state['coverage'].update(truncated=True, complete_since=None, exhausted=False)
                                state['done'] = True
                        head = None
                if state['page'] > 1:
                    self._recheck(provider, repo, state, state['page'] - 1, started)
                batch = head if head is not None else self._read(provider, repo, state['page'])
                if state['page'] > 1:
                    self._recheck(provider, repo, state, state['page'] - 1, started)
                pages += 1
                self._accept(state, repo, batch)
                state['coverage']['checked_at'] = self._wall_now().isoformat()
                if state['done'] and not state['coverage']['error']:
                    state['pending_coverage'] = copy.deepcopy(state['coverage'])
                    state['pending_at'] = self._now()
                if not state['done']:
                    queue.append(repo)
            except Exception as exc:
                from .ci_monitor import _error
                # An intervening validation failure invalidates saved evidence;
                # a later head read cannot make unread mutable pages current.
                state['pending_coverage'] = None
                if not isinstance(exc, ValueError) or not state['coverage']['error']:
                    state['coverage']['error'] = _error(exc)[1]
                state['coverage']['checked_at'] = self._wall_now().isoformat()
            self._cursor = (repos.index(repo) + 1) % len(repos)
        # A provider can change while pages are being read. Never advertise
        # exhaustion or a watermark based solely on the head seen before the scan.
        for repo in repos:
            rid = str(repo['id'])
            state = self._states[rid]
            if rid not in prepared or state['validated_this_cycle']:
                continue
            try:
                if self._stop.is_set() or self._now() - started >= self._budget:
                    raise TimeoutError('head revalidation budget exceeded')
                head = self._read(provider, repo, 1)
                signature = [(_key(r), r.get('created_at')) for r in head]
                if signature != state['head']:
                    state['coverage'].update(complete_since=None, exhausted=False,
                                             error='CI history changed during pagination. Retrying automatically.')
                    state['done'] = True
                    state['pending_coverage'] = None
                else:
                    if state['pending_coverage'] is not None:
                        state['coverage'] = state['pending_coverage']
                        state['pending_coverage'] = None
                    state['validated_this_cycle'] = True
                    if state['done'] and not state['coverage']['error']:
                        state['verified_at'] = self._now()
                    for row in head:
                        if not self._retain(state, row):
                            state['coverage'].update(truncated=True, complete_since=None, exhausted=False)
            except Exception as exc:
                from .ci_monitor import _error
                state['coverage'].update(complete_since=None, exhausted=False,
                                         error=state['coverage']['error'] or _error(exc)[1])
        self._publish(repos, complete, inventory_error)
        self._last_check = self._now()

    def _recheck(self, provider, repo, state, page, started):
        if self._stop.is_set() or self._now() - started >= self._budget:
            raise TimeoutError('history validation budget exceeded')
        batch = self._read(provider, repo, page)
        signature = [(_key(r), r.get('created_at')) for r in batch]
        if signature != state['signatures'].get(page):
            state['coverage'].update(complete_since=None, exhausted=False,
                                     error='CI pagination changed at its continuation boundary.')
            state['done'] = True
            raise ValueError('pagination changed')
        for row in batch:
            if not self._retain(state, row):
                state['coverage'].update(truncated=True, complete_since=None, exhausted=False,
                                         error='CI history reached its retention limit.')
                state['done'] = True
                raise ValueError('history retention limit')
        self._track_mutable(state, page, batch)

    @staticmethod
    def _track_mutable(state, page, rows):
        terminal = {'success', 'passed', 'failure', 'failed', 'error', 'killed',
                    'declined', 'skipped', 'canceled', 'cancelled'}
        if any((row.get('state') or row.get('status')) not in terminal for row in rows):
            state['mutable_pages'].add(page)
        else:
            state['mutable_pages'].discard(page)

    def _read(self, provider, repo, page):
        self._measurements.provider_call()
        batch = provider.pipelines(str(repo['id']), page=page, per_page=50)
        return [redact_payload({**(r.to_dict() if hasattr(r, 'to_dict') else dict(r)),
                               'provider_url': self._provider_url, 'provider_repo_id': repo['id'],
                               'repository': repo['repository']}) for r in batch]

    def _retain(self, state, row):
        key = _key(row)
        if key is None:
            return False
        size = len(json.dumps(row, separators=(',', ':')).encode())
        old = state['sizes'].get(key, 0)
        if (key not in state['rows'] and (len(state['rows']) >= self._run_limit or self._run_count >= self._total_limit)) or self._byte_count - old + size > self._byte_limit:
            return False
        self._run_count += int(key not in state['rows'])
        self._byte_count += size - old
        state['rows'][key] = row
        state['sizes'][key] = size
        return True

    def _accept(self, state, repo, batch):
        coverage = state['coverage']
        coverage['error'] = None
        state['signatures'][state['page']] = [(_key(r), r.get('created_at')) for r in batch]
        self._track_mutable(state, state['page'], batch)
        if state['page'] == 1:
            state['head'] = [(_key(r), r.get('created_at')) for r in batch]
        valid = True
        capped = False
        tail = state['tail']
        keys = set(state['seen'])
        for row in batch:
            stamp = _date(row.get('created_at'))
            key = _key(row)
            if stamp is None or key is None or key in keys or (tail is not None and stamp > tail):
                valid = False
            keys.add(key)
            state['seen'].add(key)
            if stamp is not None:
                tail = stamp
            if not self._retain(state, row):
                capped = True
        if not valid:
            coverage.update(complete_since=None, exhausted=False,
                            error='CI pagination has repeated runs, unordered rows, or unavailable timestamps.')
            state['done'] = True
            return
        if not capped:
            state['tail'] = tail
            coverage['complete_since'] = tail.isoformat() if tail is not None else None
        if capped:
            coverage.update(truncated=True, complete_since=None)
            state['done'] = True
        elif len(batch) < 50:
            coverage['exhausted'] = True
            state['done'] = True
        else:
            rows = [state['rows'][key] for key in state['seen'] if key in state['rows']]
            def pr(row):
                return str(row.get('event', '')).startswith('pull_request')
            default = repo.get('default_branch')
            enough = (sum(pr(r) for r in rows) >= 24 and default and
                      sum(r.get('branch') == default and not pr(r) for r in rows) >= 24)
            cutoff = self._wall_now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
            if enough and tail is not None and tail <= cutoff:
                state['done'] = True
            elif state['page'] >= self._page_limit or len(rows) >= self._run_limit:
                coverage['truncated'] = True
                state['done'] = True
            else:
                state['page'] += 1
        if state['done'] and not coverage['error'] and not coverage['truncated']:
            # Kept rows support the UI during a rescan, but successful coverage
            # describes only runs validated in this scan, including early stops.
            for key in set(state['rows']) - state['seen']:
                self._run_count -= 1
                self._byte_count -= state['sizes'].pop(key)
                del state['rows'][key]
