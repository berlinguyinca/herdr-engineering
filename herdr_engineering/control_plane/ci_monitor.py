"""Bounded, single-flight Woodpecker projection shared by web clients.

Snapshots are last-known observations, never execution authority. Network reads
run off the request path. Progress measures repositories checked, not job work.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .ci_observability import _Measurements
from .redact import _BEGIN as _PEM_BEGIN
from .redact import _END as _PEM_END
from .redact import redact_payload, redact_text


def _stamp():
    return datetime.now(UTC).isoformat()


def _provider():
    from ..ci import WoodpeckerProvider
    return WoodpeckerProvider()


def _error(exc):
    reason = getattr(exc, 'reason', 'unavailable')
    status = getattr(exc, 'status_code', None)
    if status == 401 or reason in {'auth_error', 'unauthorized', 'authentication'}:
        return 'auth_error', 'Woodpecker rejected the credential (HTTP 401).'
    if status == 403 or reason == 'forbidden':
        return 'permission_denied', 'Woodpecker denied access (HTTP 403).'
    messages = {
        'response_too_large': 'This output exceeds the preview limit. Open the run in Woodpecker for the full log.',
        'timeout': 'Woodpecker did not respond before the read timeout. Retrying automatically.',
        'rate_limited': 'Woodpecker is limiting requests. Retrying automatically.',
        'not_found': 'This Woodpecker resource is unavailable or has been removed.',
    }
    return 'unavailable', messages.get(reason, 'Woodpecker could not be read. Check connection and provider configuration.')


def _dict(row):
    return row.to_dict() if hasattr(row, 'to_dict') else dict(row)


def _credential_redactor(token):
    """Redact output values without changing metric keys or reading the provider."""
    def scrub(value):
        if isinstance(value, str):
            # Older parser projections may end partway through a credential.
            # Their lost suffix cannot be recovered. At a parser boundary,
            # conservatively withhold the field if even one prefix character
            # matches; never rewrite the retained record or its identity hash.
            if token and len(value) in (500, 2000, 4000) and any(
                    value.endswith(token[:length])
                    for length in range(1, min(len(token), len(value) + 1))):
                return '[REDACTED]'
            return value.replace(token, '[REDACTED]') if token else value
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    return scrub


def bound_ci_provider(factory, provider_url=None):
    """Resolve once and reject a retained identity before any upstream I/O."""
    provider = factory()
    if provider_url is not None:
        def canonical(value):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Exact CI provider identity is required')
            try:
                url = urlsplit(value.strip())
                if (url.scheme != 'https' or not url.hostname or url.username or url.password
                        or url.query or url.fragment):
                    raise ValueError
                port = url.port
                return (url.scheme, url.hostname.lower(), port or 443, url.path.rstrip('/'))
            except ValueError:
                raise ValueError('Exact CI provider identity is invalid') from None
        if canonical(provider_url) != canonical(getattr(provider, 'base_url', None)):
            raise ValueError('CI provider changed; this retained run cannot be read or acted on')
    return provider


SLICE_TTL = 30              # seconds a step log read for an excerpt is reused
SLICE_CACHE_LOGS = 4        # logs kept for that, each at most the collector's size cap
SLICE_READS = 4             # excerpt reads that may be in flight at once
_REDACTED_LINE = '[REDACTED]'


def _private_key_lines(lines):
    """Indexes of lines inside a PEM private-key block, found over the whole log.

    Redacting an excerpt line by line cannot see a BEGIN line outside the excerpt, so the body of a key that starts above
    it would be shown. An unterminated block runs to the end of the log, as `redact_text` treats it.
    """
    masked, inside = set(), False
    for i, text in enumerate(lines):
        if not inside and 'PRIVATE KEY-----' not in text:
            continue
        start = 0
        if not inside:
            begin = _PEM_BEGIN.search(text)
            if begin is None:
                continue
            inside, start = True, begin.end()
        masked.add(i)
        if _PEM_END.search(text, start):
            inside = False
    return masked


class CIMonitor:
    def __init__(self, *, provider_factory=None, interval=30, repo_limit=500,
                 budget=120, now=None, on_transition=None, actions_enabled=False,
                 on_observation=None, max_in_flight=4, max_reads_per_refresh=600,
                 on_log_report=None, log_known=None, log_tests=None):
        from ..ci import _ProviderReadScheduler, _read_allowance
        self._scheduler = _ProviderReadScheduler(max_in_flight)
        self._max_reads_per_refresh = _read_allowance(max_reads_per_refresh)
        self._cursor = 0
        self._measurements = _Measurements()
        self._factory = provider_factory or _provider
        from .ci_history import CIHistory
        self._history = CIHistory(provider_factory=self._factory, interval=interval,
                                  budget=budget, now=now, repo_limit=repo_limit,
                                  scheduler=self._scheduler, max_reads_per_refresh=max_reads_per_refresh)
        #: control_plane.ci_actions in the fleet file; retry/cancel are off unless set.
        self.actions_enabled = bool(actions_enabled)
        self._interval = max(5, interval)
        self._repo_limit = repo_limit
        self._budget = budget
        self._now = now or time.monotonic
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._log_thread = None
        self._log_wake = threading.Event()
        self._slice_cache = {}      # (provider, repository, run, step) -> (monotonic time, lines, truncated, masked line indexes)
        self._slice_lock = threading.Lock()
        self._slice_reads = threading.BoundedSemaphore(SLICE_READS)
        self._last_success = None
        self._on_transition = on_transition
        self._on_observation = on_observation
        #: control_plane.ci_log_tests: counts parsed from finished runs' logs. Needs somewhere to keep them.
        from .ci_logcollect import LogTestCollector, log_tests_config
        self._log_collector = LogTestCollector(
            log_tests_config(log_tests), ingest=on_log_report or (lambda body: None),
            known=log_known or (lambda *key: None), provider_call=self._measurements.provider_call,
        ) if on_log_report is not None else None
        self._observation_fingerprints = {}
        self._observed = {}
        self._baselined = set()
        self._view = {
            'available': False, 'status': 'not_configured', 'refreshing': False,
            'stale': False, 'repos': [], 'pipelines': {}, 'repo_errors': {},
            'agents': [], 'agents_error': None, 'error': None,
            'checked_at': None, 'last_success_at': None,
            'progress': {'completed': 0, 'total': None, 'current': 'Waiting for CI connection'},
        }

    def _measurement_snapshot(self):
        return self._measurements.snapshot()

    def snapshot(self, *, start=True):
        if start:
            self.start()
        with self._lock:
            view = copy.deepcopy(self._view)
        if self._last_success is not None and self._now() - self._last_success > self._interval * 3:
            view['stale'] = True
        self._measurements.observe(view, sum(map(len, view['pipelines'].values())), view['stale'])
        return view

    def history(self, *, start=True):
        return self._history.snapshot(start=start)

    def summary_snapshot(self, *, start=True):
        return self._history.summary_snapshot(start=start)

    def report_redactor(self):
        # Provider construction loads local configuration only. No availability
        # probe, inventory, detail, or log request is needed for report reads.
        return _credential_redactor(getattr(self._factory(), 'token', None))

    def summary(self, repo, **filters):
        # Preserve the existing lazy background-collector startup contract.
        # Provider scans run only on its worker, never on the request thread.
        self._history.start()
        # Published history rows are replaced, never mutated. Capture references
        # under the publication lock without copying raw details or serializing
        # the full compatibility snapshot. All aggregation is local.
        with self._history._lock:
            cached = self.summary_snapshot(start=False)
            pipelines = tuple(self._history._view['pipelines'])
            inventory = tuple(self._history._view['repos'])
        view = self.report_redactor()(repo.ci_summary(
            pipelines=pipelines, inventory=inventory, cached=cached, **filters))
        # Derive serialized identities only from scrubbed fields: JSON escaping
        # can otherwise conceal a configured credential from exact replacement.
        for project in view['projects']:
            project['project_id'] = json.dumps([project[key] for key in
                ('provider_url', 'provider_repo_id', 'repository')], separators=(',', ':'))
        return view

    def tests(self, repo, **filters):
        with self._history._lock:
            pipelines = tuple(self._history._view['pipelines'])
            inventory = tuple(self._history._view['repos'])
        return repo.ci_tests(redact=self.report_redactor(), pipelines=pipelines,
                             inventory=inventory, **filters)

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._view.update(refreshing=True, status='syncing')
                self._thread = threading.Thread(target=self._loop, name='herdr-ci-poll', daemon=True)
                self._thread.start()
                if self._log_collector is not None:
                    self._log_thread = threading.Thread(target=self._log_loop, name='herdr-ci-logs', daemon=True)
                    self._log_thread.start()

    def close(self):
        self._stop.set()
        self._log_wake.set()
        self._scheduler.close()
        self._history.close()
        if self._thread:
            self._thread.join(timeout=1)
        if self._log_thread:
            self._log_thread.join(timeout=1)

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._interval)

    def _update(self, **kwargs):
        with self._lock:
            self._view.update(kwargs)

    def refresh(self):
        if not self._refresh_lock.acquire(blocking=False):
            return
        started = self._now()
        try:
            reads = self._scheduler.refresh(budget=self._budget, max_reads=self._max_reads_per_refresh,
                                            now=self._now, measurements=self._measurements)
            self._collect(reads)
            self._log_wake.set()        # the log reader runs on its own thread: it must never hold up, or count in, a poll
        finally:
            self._update(refreshing=False, checked_at=_stamp())
            self._measurements.refreshed(self._now() - started)
            self._refresh_lock.release()

    def _log_loop(self):
        while not self._stop.is_set():
            self._log_wake.wait()
            self._log_wake.clear()
            if not self._stop.is_set():
                self._analyse_logs()

    def _analyse_logs(self):
        # Runs on its own thread after a poll has published its view, so reading logs never delays what the pages show,
        # holds the refresh lock, or lengthens the measured refresh.
        if self._log_collector is None or not self._log_collector.enabled or self._stop.is_set():
            return
        try:
            provider = self._factory()
            if not provider.available:
                return
            with self._lock:
                pipelines = copy.deepcopy(self._view['pipelines'])
            self._log_collector.run_cycle(provider, pipelines, self._stop)
        except Exception:
            pass        # a log that cannot be read leaves the run for the next cycle

    def _collect(self, reads):
        self._update(refreshing=True, stale=bool(self._view['pipelines']), error=None,
                     progress={'completed': 0, 'total': None, 'current': 'Discovering repositories'})
        started = self._now()
        try:
            provider = self._factory()
            if not provider.available:
                self._update(available=False, status='not_configured', stale=bool(self._view['pipelines']) or self._last_success is not None,
                             error='Configure a server-side Woodpecker token to connect CI.')
                return
            repos = []
            page = 1
            seen = set()
            while len(repos) < self._repo_limit:
                batch = reads.call(provider.repos, page=page, per_page=50)
                unique = [row for row in batch if str(row.get('id', row.get('repository'))) not in seen]
                seen.update(str(row.get('id', row.get('repository'))) for row in unique)
                repos.extend(unique[:self._repo_limit - len(repos)])
                if getattr(provider, 'repos_are_snapshot', False) or len(batch) != 50 or not unique:
                    break
                page += 1
                if self._now() - started > self._budget:
                    raise TimeoutError('discovery budget exceeded')
            with self._lock:
                previous = copy.deepcopy(self._view['pipelines'])
            pipelines, errors = {}, {}
            successful_repos = set()
            counts = Counter(row.get('repository') for row in repos)
            from ..ci import _ReadBudgetExceeded
            if repos:
                self._cursor %= len(repos)
            order = repos[self._cursor:] + repos[:self._cursor]
            attempted = set()
            for index, (repo, rows, read_error) in enumerate(reads.map(
                    lambda repo: provider.pipelines(str(repo['id']), per_page=20), order)):
                name = repo.get('repository')
                if not isinstance(name, str) or not name:
                    continue
                group = name if counts[name] == 1 else f"{name} ({urlsplit(repo.get('forge_url') or '').hostname or 'forge'} #{repo.get('id')})"
                self._update(progress={'completed': index, 'total': len(repos), 'current': name})
                if self._stop.is_set():
                    return
                try:
                    if read_error is not None:
                        raise read_error
                    successful_repos.add(str(repo['id']))
                    pipelines[group] = [redact_payload({**_dict(row), 'repository': name,
                                         'provider_repo_id': repo['id'], 'provider_url': provider.base_url.rstrip('/')}) for row in rows]
                except Exception as exc:
                    pipelines[group] = [row for row in previous.get(group, [])
                                        if row.get('provider_url') == provider.base_url.rstrip('/')
                                        and str(row.get('provider_repo_id')) == str(repo['id'])
                                        and row.get('repository') == name]
                    errors[group] = _error(exc)[1]
                if not isinstance(read_error, _ReadBudgetExceeded):
                    attempted.add(str(repo['id']))
                self._update(pipelines=copy.deepcopy({**previous, **pipelines}), repos=redact_payload(repos),
                             progress={'completed': index + 1, 'total': len(repos), 'current': name})
            # Completion order is nondeterministic; fairness follows submission
            # order and skips reads that could not acquire a budget/capacity slot.
            for repo in order:
                if str(repo['id']) in attempted:
                    self._cursor = (repos.index(repo) + 1) % len(repos)
            agents, agents_error = [], None
            try:
                agents = redact_payload(reads.call(provider.agents))
            except Exception:
                agents_error = 'Runner inventory requires provider permission or is unavailable.'
            # A full page at the safety cap must never look like a complete inventory.
            limited = len(repos) >= self._repo_limit
            if limited:
                errors['_inventory'] = f'Inventory limited to {self._repo_limit} repositories.'
            history_error = self._persist_observations(pipelines)
            if history_error:
                errors['_history'] = history_error
            self._transitions(pipelines, successful_repos)
            if not errors:
                self._last_success = self._now()
                self._update(last_success_at=_stamp())
            self._update(available=True, status='partial' if errors else 'connected',
                         stale=bool(errors), repos=redact_payload(repos), pipelines=pipelines,
                         repo_errors=errors, agents=agents, agents_error=agents_error,
                         error=None)
        except Exception as exc:
            status, message = _error(exc)
            self._update(available=False, status=status, stale=bool(self._view['pipelines']) or self._last_success is not None, error=message)

    def _persist_observations(self, pipelines):
        """Deduplicate the poll inventory independently of retained history.

        Keep only acknowledged fingerprints in this snapshot, bounded by the
        repository inventory and provider's per-repository recent-run limit.
        Failed writes remain retryable even when a run itself is unchanged.
        """
        if self._on_observation is None:
            return None
        fingerprints, error = {}, None
        for rows in pipelines.values():
            for row in rows:
                key = (str(row.get('provider_repo_id')), str(row.get('run_number')))
                digest = hashlib.sha256(json.dumps(
                    row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                previous = fingerprints.get(key, self._observation_fingerprints.get(key))
                if previous != digest:
                    try:
                        self._on_observation(copy.deepcopy(row))
                    except Exception:
                        error = 'CI history could not be persisted. Retrying automatically.'
                        continue
                fingerprints[key] = digest
        self._observation_fingerprints = fingerprints
        return error

    def _transitions(self, pipelines, successful_repos):
        failed = {'failure', 'error', 'failed', 'killed'}
        finished = {'success', 'passed', 'skipped', 'declined', 'canceled', 'cancelled'}
        for rows in pipelines.values():
            for row in rows:
                key = f"{row.get('provider_repo_id')}:{row.get('run_number')}"
                state = row.get('state') or row.get('status')
                old = self._observed.get(key)
                self._observed[key] = state
                # First observation establishes a baseline, not a notification.
                if str(row.get('provider_repo_id')) in self._baselined and old != state and state in failed | finished:
                    if self._on_transition:
                        try:
                            self._on_transition(key, 'CIPipelineFailed' if state in failed else
                                                'CIPipelineFinished', copy.deepcopy(row))
                        except Exception:
                            pass
        self._baselined.update(successful_repos)
        # Match the bounded recent pipeline window rather than grow forever.
        current = {f"{r.get('provider_repo_id')}:{r.get('run_number')}"
                   for rows in pipelines.values() for r in rows}
        self._observed = {k: v for k, v in self._observed.items() if k in current}

    def detail(self, repository, number, *, provider_url=None):
        repository, number = self._validate(repository, number)
        provider = bound_ci_provider(self._factory, provider_url)
        if not provider.available:
            return {'available': False, 'error': 'Woodpecker is not configured.'}
        try:
            self._measurements.provider_call()
            row = provider.pipeline(repository, number)
            if row is None:
                return {'available': False, 'error': 'Pipeline was not found.'}
            row = _dict(row)
            # Details carry execution metadata only; logs stay on the lazy route.
            row = {key: value for key, value in row.items() if key in {
                'id', 'number', 'run_number', 'repository', 'provider_repo_id', 'provider_url',
                'revision', 'commit', 'branch', 'state', 'status', 'event', 'ref', 'forge_url',
                'author', 'message', 'title', 'created', 'started', 'finished',
                'created_at', 'started_at', 'finished_at', 'url', 'can_retry', 'can_cancel',
                'tasks', 'workflows'}}
            for field in ('tasks', 'workflows'):
                row[field] = [{key: value for key, value in item.items() if key in {
                    'id', 'name', 'state', 'status', 'exit_code', 'type', 'workflow', 'workflow_id',
                    'depends_on', 'duration', 'started', 'finished', 'started_at', 'finished_at', 'error'}}
                    for item in row.get(field, []) or [] if isinstance(item, dict)]
            if getattr(provider, 'base_url', None):
                row['provider_url'] = provider.base_url.rstrip('/')
            row.setdefault('run_number', number)
            if repository.isdigit():
                row.setdefault('provider_repo_id', repository)
            with self._history._lock:
                inventory = tuple(self._history._view['repos'])
            with self._lock:
                inventory += tuple(self._view['repos'])
            identities = {(item.get('provider_url', '').rstrip('/'), str(item.get('id')), item.get('repository'))
                          for item in inventory
                          if item.get('provider_url', '').rstrip('/') == row.get('provider_url') and
                          row.get('provider_repo_id') is not None and
                          str(item.get('id')) == str(row['provider_repo_id'])}
            if len(identities) == 1:
                url, rid, name = identities.pop()
                if (row.get('repository') in (rid, name) and
                        (row.get('provider_repo_id') is None or str(row['provider_repo_id']) == rid)):
                    row.update(provider_url=url, provider_repo_id=rid, repository=name)
            if provider_url is not None:
                row.update(provider_url=provider.base_url.rstrip('/'), run_number=number)
            if not self.actions_enabled:
                row.update(can_retry=False, can_cancel=False)
            scrub = _credential_redactor(getattr(provider, 'token', None))
            row = redact_payload(scrub(row))
            return {'available': True, 'pipeline': row, 'checked_at': _stamp(),
                    **({'provider_url': provider.base_url.rstrip('/')} if provider_url is not None else {})}
        except Exception as exc:
            return {'available': False, 'status': _error(exc)[0], 'error': _error(exc)[1]}

    def log(self, repository, number, step, *, provider_url=None):
        repository, number = self._validate(repository, number)
        if not isinstance(step, str) or not step or len(step) > 200:
            raise ValueError('step is required')
        provider = bound_ci_provider(self._factory, provider_url)
        try:
            self._measurements.provider_call()
            value = provider.step_log(repository, number, step)
            if value is None:
                return {'available': False, 'log': '', 'error': 'Step log is unavailable.'}
            # Redact exact credential as well as common secret patterns.
            value = value.replace(provider.token, '[REDACTED]') if provider.token else value
            return {'available': True, 'log': redact_text(value[:262144]),
                    **({'provider_url': provider.base_url.rstrip('/')} if provider_url is not None else {}),
                    'truncated': len(value) > 262144 or value.endswith('[log truncated; open CI for the full log]'),
                    'checked_at': _stamp()}
        except Exception as exc:
            return {'available': False, 'log': '', 'error': _error(exc)[1]}

    def log_slice(self, repository, number, step, start, end, *, context=40, expect='', provider_url=None):
        """Lines of one step's live log around an indexed test, redacted, or a plain statement of why not.

        Line numbers are those of the log text as the collector read it. When the log is shorter than the range, or the
        test's name is not where the index says it was, no slice is shown: a wrong slice would be worse than none.
        """
        repository, number = self._validate(repository, number)
        if not isinstance(step, str) or not step.isascii() or not step.isdigit():
            raise ValueError('a numeric step id is required')
        start, end = int(start), int(end)
        if start < 1 or end < start or end - start > 2000:
            raise ValueError('a valid line range is required')
        context = max(0, min(int(context), 200))
        provider = bound_ci_provider(self._factory, provider_url)
        limit = self._log_collector.max_log_bytes if self._log_collector else 4 * 1024 * 1024
        base = {'provider_url': provider.base_url.rstrip('/')} if provider_url is not None else {}
        try:
            loaded = self._slice_log(provider, repository, number, step, limit)
            if loaded is None:
                return {'available': False, 'error': 'Step log is unavailable.', **base}
            if loaded == 'busy':
                return {'available': False, 'error': 'Several log excerpts are being read; try again in a moment.', **base}
            lines, truncated, masked = loaded
            if end > len(lines):
                reason = ('The log is too large to read in full, and this range lies beyond what was read.'
                          if truncated else 'The log is shorter than the stored line range; it may have been replaced.')
                return {'available': True, 'slice': None, 'reason': reason, 'truncated': truncated, 'total_lines': len(lines), **base}
            import re
            tokens = [t for t in re.split(r'[^A-Za-z0-9_]+', str(expect or '')) if len(t) >= 4]
            if tokens and not any(max(tokens, key=len) in text for text in lines[max(0, start - 2):end + 1]):
                return {'available': True, 'slice': None, 'truncated': truncated, 'total_lines': len(lines),
                        'reason': 'The log no longer matches the stored line range, so no excerpt is shown.', **base}
            first, last = max(1, start - 1), min(len(lines), end + context)
            return {'available': True, 'truncated': truncated, 'total_lines': len(lines), 'checked_at': _stamp(),
                    'slice': {'first': first, 'last': last, 'start': start, 'end': end,
                              'lines': [_REDACTED_LINE if first + i - 1 in masked else redact_text(text[:2000])
                                        for i, text in enumerate(lines[first - 1:last])]}, **base}
        except Exception as exc:
            return {'available': False, 'error': _error(exc)[1], **base}

    def _slice_log(self, provider, repository, number, step, limit):
        """(lines, truncated, masked line indexes) of one step's log, read at most once per `SLICE_TTL` seconds.

        Opening one test fires a request per click; the same log is read again for the next click on the same run.
        """
        key = (provider.base_url.rstrip('/'), repository, number, step, limit)
        with self._slice_lock:
            hit = self._slice_cache.get(key)
            if hit and self._now() - hit[0] < SLICE_TTL:
                return hit[1:]
        if not self._slice_reads.acquire(timeout=5):
            return 'busy'
        try:
            self._measurements.provider_call()
            fetched = provider.step_log_text(repository, number, step, max_chars=limit)
        finally:
            self._slice_reads.release()
        if fetched is None:
            return None
        from .ci_logtests import clean_lines
        lines = clean_lines(fetched[0])
        entry = (self._now(), lines, fetched[1], _private_key_lines(lines))
        with self._slice_lock:
            now = entry[0]
            self._slice_cache = {k: v for k, v in self._slice_cache.items() if now - v[0] < SLICE_TTL}
            while len(self._slice_cache) >= SLICE_CACHE_LOGS:
                del self._slice_cache[min(self._slice_cache, key=lambda k: self._slice_cache[k][0])]
            self._slice_cache[key] = entry
        return entry[1:]

    def action(self, repository, number, action, *, provider_url=None):
        repository, number = self._validate(repository, number)
        if not self.actions_enabled:
            raise PermissionError('CI actions are disabled by this deployment')
        if action not in {'retry', 'cancel'}:
            raise ValueError('unsupported CI action')
        provider = bound_ci_provider(self._factory, provider_url)
        self._measurements.provider_call()
        detail = provider.pipeline(repository, number)
        if not detail or not detail.get('can_retry' if action == 'retry' else 'can_cancel'):
            raise PermissionError('Woodpecker does not permit this action for this pipeline')
        self._measurements.provider_call()
        getattr(provider, action)(repository, str(number), authorized=True)
        return {'ok': True, 'action': action, 'repository': repository, 'run_number': number}

    @staticmethod
    def _validate(repository, number):
        if not isinstance(repository, str) or not repository or len(repository) > 300:
            raise ValueError('repository is required')
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise ValueError('positive pipeline number is required') from None
        if number < 1:
            raise ValueError('positive pipeline number is required')
        return repository, number
