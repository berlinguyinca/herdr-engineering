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


class CIMonitor:
    def __init__(self, *, provider_factory=None, interval=30, repo_limit=500,
                 budget=120, now=None, on_transition=None, actions_enabled=False,
                 on_observation=None):
        self._factory = provider_factory or _provider
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
        self._last_success = None
        self._on_transition = on_transition
        self._on_observation = on_observation
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

    def snapshot(self, *, start=True):
        if start:
            self.start()
        with self._lock:
            view = copy.deepcopy(self._view)
        if self._last_success is not None and self._now() - self._last_success > self._interval * 3:
            view['stale'] = True
        return view

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._view.update(refreshing=True, status='syncing')
                self._thread = threading.Thread(target=self._loop, name='herdr-ci-poll', daemon=True)
                self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

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
        try:
            self._collect()
        finally:
            self._update(refreshing=False, checked_at=_stamp())
            self._refresh_lock.release()

    def _collect(self):
        self._update(refreshing=True, error=None,
                     progress={'completed': 0, 'total': None, 'current': 'Discovering repositories'})
        started = self._now()
        try:
            provider = self._factory()
            if not provider.available:
                self._update(available=False, status='not_configured', stale=bool(self._last_success),
                             error='Configure a server-side Woodpecker token to connect CI.')
                return
            repos = []
            page = 1
            seen = set()
            while len(repos) < self._repo_limit:
                batch = provider.repos(page=page, per_page=50)
                unique = [row for row in batch if str(row.get('id', row.get('repository'))) not in seen]
                seen.update(str(row.get('id', row.get('repository'))) for row in unique)
                repos.extend(unique[:self._repo_limit - len(repos)])
                if len(batch) != 50 or not unique:
                    break
                page += 1
                if self._now() - started > self._budget:
                    raise TimeoutError('discovery budget exceeded')
            with self._lock:
                previous = copy.deepcopy(self._view['pipelines'])
            pipelines, errors = {}, {}
            successful_repos = set()
            counts = Counter(row.get('repository') for row in repos)
            for index, repo in enumerate(repos):
                name = repo.get('repository')
                if not isinstance(name, str) or not name:
                    continue
                group = name if counts[name] == 1 else f"{name} ({urlsplit(repo.get('forge_url') or '').hostname or 'forge'} #{repo.get('id')})"
                self._update(progress={'completed': index, 'total': len(repos), 'current': name})
                if self._stop.is_set():
                    return
                try:
                    if self._now() - started > self._budget:
                        raise TimeoutError('refresh budget exceeded')
                    rows = provider.pipelines(str(repo['id']), per_page=20)
                    successful_repos.add(str(repo['id']))
                    pipelines[group] = [redact_payload({**_dict(row), 'repository': name,
                                         'provider_repo_id': repo['id']}) for row in rows]
                except Exception as exc:
                    pipelines[group] = previous.get(group, [])
                    errors[group] = _error(exc)[1]
                self._update(pipelines=copy.deepcopy({**previous, **pipelines}), repos=redact_payload(repos),
                             progress={'completed': index + 1, 'total': len(repos), 'current': name})
            agents, agents_error = [], None
            try:
                agents = redact_payload(provider.agents())
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
            self._update(available=False, status=status, stale=bool(self._last_success), error=message)

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

    def detail(self, repository, number):
        repository, number = self._validate(repository, number)
        provider = self._factory()
        if not provider.available:
            return {'available': False, 'error': 'Woodpecker is not configured.'}
        try:
            row = provider.pipeline(repository, number)
            if row is None:
                return {'available': False, 'error': 'Pipeline was not found.'}
            row = redact_payload(_dict(row))
            if not self.actions_enabled:
                row.update(can_retry=False, can_cancel=False)
            return {'available': True, 'pipeline': row, 'checked_at': _stamp()}
        except Exception as exc:
            return {'available': False, 'status': _error(exc)[0], 'error': _error(exc)[1]}

    def log(self, repository, number, step):
        repository, number = self._validate(repository, number)
        if not isinstance(step, str) or not step or len(step) > 200:
            raise ValueError('step is required')
        provider = self._factory()
        try:
            value = provider.step_log(repository, number, step)
            if value is None:
                return {'available': False, 'log': '', 'error': 'Step log is unavailable.'}
            # Redact exact credential as well as common secret patterns.
            value = value.replace(provider.token, '[REDACTED]') if provider.token else value
            return {'available': True, 'log': redact_text(value[:262144]),
                    'truncated': len(value) > 262144 or value.endswith('[log truncated; open CI for the full log]'),
                    'checked_at': _stamp()}
        except Exception as exc:
            return {'available': False, 'log': '', 'error': _error(exc)[1]}

    def action(self, repository, number, action):
        repository, number = self._validate(repository, number)
        if not self.actions_enabled:
            raise PermissionError('CI actions are disabled by this deployment')
        if action not in {'retry', 'cancel'}:
            raise ValueError('unsupported CI action')
        provider = self._factory()
        detail = provider.pipeline(repository, number)
        if not detail or not detail.get('can_retry' if action == 'retry' else 'can_cancel'):
            raise PermissionError('Woodpecker does not permit this action for this pipeline')
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
