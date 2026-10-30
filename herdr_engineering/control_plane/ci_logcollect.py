"""Zero-config test counts: read the logs of finished runs and keep what the test runners printed.

The control plane pulls each finished run's step logs from the CI provider itself, so no project needs a reporting step or a
credential in its pipeline. Only counts, runner names and a per-test index (names, statuses, line ranges) are kept; log text is
read, parsed and dropped. See ``ci_logtests`` for the parsers and ``docs/ci-test-reports.md`` for the limits.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from . import ci_logtests

DEFAULTS = {'enabled': True, 'runs_per_cycle': 10, 'max_log_mib': 4, 'window_days': 14}
_RANGES = {'runs_per_cycle': (1, 100), 'max_log_mib': (1, 64), 'window_days': (1, 90)}
_DONE_STATES = {'success', 'failure'}
_STEP_STATES = {'success', 'failure'}
_PROVISIONAL_SECONDS = 3600     # a run that ended less than this long before it was read may still have been flushing logs
_RETRY_SECONDS = 6 * 3600
_ERROR_BACKOFF_SECONDS = 300
MAX_ATTEMPTS = 5                # reads of one run's logs per process lifetime, failed or not; then it is left alone


def log_tests_config(raw):
    """Validated ``control_plane.ci_log_tests``; ``None`` means the defaults, which are on."""
    if raw is None:
        return dict(DEFAULTS)
    if not isinstance(raw, dict):
        raise ValueError('ci_log_tests must be a mapping')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"ci_log_tests has unknown keys: {', '.join(sorted(unknown))}")
    cfg = dict(DEFAULTS)
    if 'enabled' in raw:
        if not isinstance(raw['enabled'], bool):
            raise ValueError('ci_log_tests.enabled must be true or false')
        cfg['enabled'] = raw['enabled']
    for key, (low, high) in _RANGES.items():
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'ci_log_tests.{key} must be an integer from {low} to {high}')
            cfg[key] = value
    return cfg


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC).timestamp()
        except ValueError:
            return None
    return None


class _Stopped(Exception):
    """The monitor is closing; abandon the run in hand without penalising it."""


class LogTestCollector:
    """Analyses a bounded number of finished runs per poll cycle, newest first, each run once."""

    def __init__(self, config, *, ingest, known, provider_call=lambda: None, wall=time.time, monotonic=time.monotonic):
        self._cfg = config
        self._ingest = ingest
        self._known = known
        self._provider_call = provider_call
        self._wall = wall
        self._monotonic = monotonic
        self._memo = {}         # (provider_url, repo id, run) -> {'outcome', 'analysed', 'provisional'}
        self._cooldown = {}     # key -> monotonic time before which a failed read is not retried
        self._attempts = {}     # key -> how many times this process has read the run

    @property
    def max_log_bytes(self):
        return self._cfg['max_log_mib'] * 1024 * 1024

    @property
    def enabled(self):
        return bool(self._cfg.get('enabled'))

    def _due(self, key, finished):
        now = self._wall()
        record = self._memo.get(key)
        if record is None:
            found = self._known(*key)
            if found is None:
                return True
            analysed = _epoch(found.get('analysed_at')) or 0.0
            record = self._memo[key] = {'outcome': found.get('outcome'), 'analysed': analysed,
                                        'provisional': bool(found.get('provisional'))}
        if record['outcome'] == 'no_tests' and record['provisional']:
            return now - record['analysed'] >= _RETRY_SECONDS
        return False

    def candidates(self, pipelines):
        """Finished, in-window, not yet analysed runs, newest first."""
        horizon = self._wall() - self._cfg['window_days'] * 86400
        rows, live = [], set()
        clock = self._monotonic()
        self._cooldown = {key: until for key, until in self._cooldown.items() if until > clock}
        for group in pipelines.values():
            for row in group:
                finished = _epoch(row.get('finished_at') or row.get('finished'))
                state = row.get('state') or row.get('status')
                if (state not in _DONE_STATES or finished is None or finished < horizon or not row.get('provider_url')
                        or row.get('provider_repo_id') in (None, '') or not isinstance(row.get('run_number'), int)):
                    continue
                key = (row['provider_url'], str(row['provider_repo_id']), row['run_number'])
                live.add(key)
                if key in self._cooldown or self._attempts.get(key, 0) >= MAX_ATTEMPTS or not self._due(key, finished):
                    continue
                rows.append((finished, key, row))
        # Bookkeeping follows the runs the monitor still shows; a run that left the window takes its entries with it.
        self._memo = {key: value for key, value in self._memo.items() if key in live}
        self._attempts = {key: value for key, value in self._attempts.items() if key in live}
        rows.sort(key=lambda item: item[0], reverse=True)
        return rows

    def run_cycle(self, provider, pipelines, stop=None):
        """Analyse up to ``runs_per_cycle`` runs; returns how many were recorded."""
        if not self.enabled:
            return 0
        done = 0
        for finished, key, row in self.candidates(pipelines)[:self._cfg['runs_per_cycle']]:
            if stop is not None and stop.is_set():
                break
            self._attempts[key] = self._attempts.get(key, 0) + 1
            try:
                body = self.analyse(provider, key, row, finished, stop)
                self._ingest(body)
            except _Stopped:
                self._attempts[key] -= 1
                break
            except Exception as exc:
                self._cooldown[key] = self._monotonic() + _ERROR_BACKOFF_SECONDS
                if getattr(exc, 'reason', '') == 'rate_limited':
                    break           # the provider asked for less; the next cycle tries again
                continue
            self._memo[key] = {'outcome': body['outcome'], 'analysed': self._wall(), 'provisional': body['provisional']}
            done += 1
        return done

    def analyse(self, provider, key, row, finished, stop=None):
        provider_url, repo_id, number = key
        self._provider_call()
        detail = provider.pipeline(repo_id, number) or {}
        limit = self._cfg['max_log_mib'] * 1024 * 1024
        parsed, steps, skipped = [], [], []
        unreadable = False
        for task in detail.get('tasks') or []:
            name = str(task.get('name') or '')
            if task.get('state') not in _STEP_STATES or task.get('id') in (None, '') or name == 'clone':
                continue
            if stop is not None and stop.is_set():
                raise _Stopped
            self._provider_call()
            try:
                fetched = provider.step_log_text(repo_id, number, str(task['id']), max_chars=limit)
            except Exception as exc:
                if getattr(exc, 'reason', '') == 'rate_limited':
                    raise               # the provider asked for less: stop this run and the cycle
                skipped.append({'step': name, 'reason': 'read_failed'})     # one unreadable step must not hide the others
                unreadable = True
                continue
            if fetched is None:
                continue
            text, truncated = fetched
            if truncated:
                skipped.append({'step': name, 'reason': 'log_too_large'})
                continue
            result = ci_logtests.parse_step_log(text, name)
            if result is None:
                continue
            parsed.append((task['id'], name, result))
            steps.append({'step_id': str(task['id']), 'step': name, 'runners': result['runners'],
                          'indexed': bool(result['tests'])})
        combined = ci_logtests.combine_steps(parsed)
        age = self._wall() - finished
        body = {'provider_url': provider_url, 'provider_repo_id': row['provider_repo_id'], 'run_number': number,
                'repository': row.get('repository') or repo_id,
                'branch': row.get('branch') or row.get('ref') or 'unknown',
                'revision': row.get('revision') or row.get('commit') or 'unknown',
                'run_finished_at': row.get('finished_at') or '', 'provisional': age < _PROVISIONAL_SECONDS or unreadable,
                'steps': steps, 'skipped_steps': skipped}
        if combined is None:
            return {**body, 'outcome': 'no_tests'}
        return {**body, 'outcome': 'counted', 'runners': combined['runners'], 'summary': combined['summary'],
                'tests': combined['tests'], 'index_complete': combined['complete']}
