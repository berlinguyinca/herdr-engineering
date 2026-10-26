"""Bounded, memory-only projections of CI repository/event payloads.

Callers supply observed pipelines and normalized reports; this module performs no
provider reads or persistence. It never treats retained observations as complete
history. Only bounded, redacted structured test excerpts are retained; raw
details, XML and logs do not belong in this store.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections import Counter, OrderedDict
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .ci_performance import _iso, _stats, _timestamp
from .ci_reports import HISTORY_BYTES, MAX_CASES, PIPELINE_LIMIT, REPORT_LIMIT
from .redact import redact_text


def _stamp():
    return datetime.now(UTC).isoformat()


def _size(value):
    return len(json.dumps(value, separators=(',', ':')).encode())


class _Measurements:
    """Private numeric/timestamp test seam, never part of an API snapshot."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict(provider_calls=0, summary_rows=0, payload_bytes=0,
                            cache_hits=0, stale=False, snapshot_at=None,
                            refresh_duration_seconds=None, refresh_finished_at=None)

    def provider_call(self):
        with self._lock:
            self._values['provider_calls'] += 1

    def observe(self, view, rows, stale):
        size = _size(view)
        with self._lock:
            self._values.update(summary_rows=rows, payload_bytes=size, stale=bool(stale),
                                snapshot_at=_stamp())
            self._values['cache_hits'] += 1

    def refreshed(self, duration):
        with self._lock:
            self._values.update(refresh_duration_seconds=max(0, duration),
                                refresh_finished_at=_stamp())

    def snapshot(self):
        with self._lock:
            return dict(self._values)


_PIPELINE_FIELDS = ('provider_url', 'provider_repo_id', 'repository', 'run_number',
                    'revision', 'branch', 'state', 'created_at', 'started_at', 'finished_at', 'event', 'ref')
_REPORT_FIELDS = ('provider_url', 'provider_repo_id', 'repository', 'run_number',
                  'revision', 'branch', 'reported_at', 'ingested_at')
_COUNTS = ('passed', 'failed', 'errored', 'skipped', 'total', 'duration_ms')
_FILTERS = ('provider_url', 'provider_repo_id', 'repository', 'branch')
_PIPELINE_OUTCOMES = {
    state: outcome
    for outcome, states in (
        ('success', ('success', 'passed')),
        ('failure', ('failure', 'failed', 'error', 'errored', 'timed_out', 'crashed')),
        ('running', ('running', 'started')),
        ('queued', ('queued', 'pending', 'waiting')),
        ('skipped', ('skipped',)),
        ('blocked', ('blocked',)),
        ('canceled', ('canceled', 'cancelled', 'killed', 'declined')),
    )
    for state in states
}


def pipeline_outcome(row):
    """Keep untrusted provider states out of aggregate keys without dropping runs."""
    state = row.get('state') or row.get('status')
    return _PIPELINE_OUTCOMES.get(state, 'unknown') if isinstance(state, str) else 'unknown'


def run_in_scope(row, scope, default_branch=None):
    event = row.get('event') or ''
    is_pr = event in ('pull_request', 'pull_request_target', 'pull_request_closed',
                     'pull_request_opened', 'pull_request_sync', 'merge_request') or event.startswith('pull_request:') or bool(
        re.match(r'^refs/(pull|merge-requests)/\d+/(head|merge)$', row.get('ref') or ''))
    return (scope == 'all' or scope == 'pr' and is_pr or
            scope == 'default' and bool(default_branch) and not is_pr and row.get('branch') == default_branch)


def run_window(now, days, start=None, end=None):
    """UTC creation-day window, intersected with a half-open selected bucket."""
    return (max(now // 86400 * 86400 - (days - 1) * 86400,
                _timestamp(start) if start is not None else float('-inf')),
            min(now, _timestamp(end) if end is not None else now))


def run_in_window(row, now, days, start=None, end=None):
    lower, upper = run_window(now, days, start, end)
    stamp = _timestamp(row.get('created_at'))
    return (stamp is not None and lower <= stamp <= upper and
            (end is None or stamp < _timestamp(end)))


def _project(row, fields):
    result = {}
    for field in fields:
        value = row.get(field)
        if value is not None and type(value) not in (str, int, bool):
            raise ValueError('CI summary fields must be scalar values')
        if isinstance(value, str):
            if len(value) > 1000:
                raise ValueError('CI summary field exceeds its size limit')
            value = redact_text(value)
        result[field] = value
    return result


def _joinable(value):
    return (isinstance(value, str) and 0 < len(value) <= 1000 and
            '[REDACTED]' not in value and redact_text(value) == value)


def _identity(row):
    """Exact provider identity; absent/credential-bearing URLs cannot be joined."""
    url = row.get('provider_url')
    if not _joinable(url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            return None
        parsed.port  # Reject malformed ports without retaining their text.
    except ValueError:
        return None
    repo_id, name, number = (row.get(key) for key in
                             ('provider_repo_id', 'repository', 'run_number'))
    if (type(repo_id) not in (str, int) or not _joinable(str(repo_id)) or
            not _joinable(name) or
            type(number) not in (str, int) or not str(number).isdigit() or int(number) < 1):
        return None
    return url.rstrip('/'), str(repo_id), name, int(number)


def _test_cases(report):
    """Project validated parser output; legacy count-only reports stay count-only."""
    cases = report.get('tests')
    summary = report['summary']
    if (not isinstance(cases, list) or len(cases) > MAX_CASES or
            len(cases) != summary['total']):
        return None
    projected, ids = [], set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('id'), str) or
                not re.fullmatch(r'[0-9a-f]{24}', case['id']) or case['id'] in ids or
                case.get('status') not in _COUNTS[:4] or
                type(case.get('duration_ms')) is not int or
                not 0 <= case['duration_ms'] <= 31536000000):
            return None
        ids.add(case['id'])
        row = {key: case[key] for key in ('id', 'status', 'duration_ms')}
        row['identity_ambiguous'] = case.get('identity_ambiguous') is True
        for field, limit in (('name', 500), ('suite', 500), ('classname', 500),
                             ('file', 500), ('message', 2000), ('trace', 4000)):
            value = case.get(field, '')
            if not isinstance(value, str):
                return None
            row[field] = redact_text(value)[:limit]
        projected.append(row)
    # Old journals lack the parser's ambiguity flag. Display-label collisions
    # cannot prove identity either, so conservatively exclude those from proof.
    labels = ('suite', 'classname', 'name', 'file')
    occurrences = Counter(tuple(case[key] for key in labels) for case in projected)
    for case in projected:
        if occurrences[tuple(case[key] for key in labels)] > 1:
            case['identity_ambiguous'] = True
    if (any(sum(case['status'] == state for case in projected) != summary[state]
            for state in _COUNTS[:4]) or
            sum(case['duration_ms'] for case in projected) != summary['duration_ms']):
        return None
    return projected


def bind_report_provider(report, pipelines):
    """Resolve legacy uploads only against one exact, full provider identity.

    Use the uploaded ID, not the compatibility API's name-based reconciliation.
    This projection never rewrites the durable upload or its receipt.
    """
    projected = dict(report)
    provided = report.get('uploaded_provider_repo_id', report.get('provider_repo_id'))
    projected['provider_repo_id'] = provided
    if _identity(projected) is not None:
        return projected
    matches = set()
    for pipeline in pipelines:
        identity = _identity(pipeline)
        if (identity is None or any(pipeline.get(key) != report.get(key)
                for key in ('repository', 'run_number', 'branch', 'revision')) or
                (provided is not None and str(provided) != identity[1]) or
                (report.get('provider_url') is not None and
                 report['provider_url'].rstrip('/') != identity[0])):
            continue
        matches.add(identity[:3])
    if len(matches) == 1:
        projected['provider_url'], projected['provider_repo_id'], _ = matches.pop()
    return projected


def _run_reference(report):
    return {key: report.get(key) for key in
            ('provider_url', 'provider_repo_id', 'repository', 'run_number',
             'revision', 'branch', 'report_id', 'reported_at')}


def _aggregate_test(observations):
    # Stable sort retains receipt order when report timestamps are equal.
    observations.sort(key=lambda item: item[0])
    _, last_report, last_case = observations[-1]
    counts = {state: sum(case['status'] == state for _, _, case in observations)
              for state in _COUNTS[:4]}
    failures = [item for item in observations if item[2]['status'] in ('failed', 'errored')]
    passes = [item for item in observations if item[2]['status'] == 'passed']
    completed = [item for item in observations if item[2]['status'] != 'skipped']
    ambiguous = any(item[2]['identity_ambiguous'] for item in observations)
    revisions = {}
    for item in completed:
        revisions.setdefault(item[1]['revision'], []).append(item)
    retries = []
    inconsistent_count = 0
    for revision, items in revisions.items():
        if {item[2]['status'] == 'passed' for item in items} == {True, False}:
            inconsistent_count += 1
            if ambiguous:
                continue
            # Two opposing attempts are sufficient evidence; avoid an unbounded
            # per-case copy of every run in the report window.
            failed = next(item for item in items if item[2]['status'] != 'passed')
            passed = next(item for item in reversed(items) if item[2]['status'] == 'passed')
            retries.append({'revision': revision,
                            'runs': [_run_reference(failed[1]), _run_reference(passed[1])]})
    representatives = []
    for item in ([failures[0], failures[-1]] if failures else []) + (
            [passes[-1]] if passes else [observations[-1]]):
        reference = _run_reference(item[1])
        if reference not in representatives:
            representatives.append(reference)
    return {
        **{key: last_report[key] for key in
           ('provider_url', 'provider_repo_id', 'repository', 'branch')},
        **{key: last_case[key] for key in ('id', 'name', 'suite', 'classname', 'file')},
        **counts, 'completed_count': len(completed), 'failure_count': len(failures),
        'failure_rate': len(failures) / len(completed) if completed else None,
        'inconsistent_count': inconsistent_count, 'identity_ambiguous': ambiguous,
        'classification': 'proven_flaky' if retries else
                          'inconsistent_outcomes' if failures and passes else 'stable',
        'first_failure': _iso(failures[0][0]) if failures else None,
        'last_pass': _iso(passes[-1][0]) if passes else None,
        'last_duration_ms': last_case['duration_ms'],
        'duration_ms': _stats([case['duration_ms'] for _, _, case in completed]),
        'same_revision_retries': retries[:5],
        'representative_runs': representatives,
        'evidence_availability': 'reported',
        'failure_evidence': ({'message': failures[-1][2]['message'],
                              'trace': failures[-1][2]['trace'],
                              'run': _run_reference(failures[-1][1])} if failures else None),
    }


def project_report(report):
    """Validate and detach one compact report projection for replay reuse."""
    identity = _identity(report)
    if identity is None or any(not _joinable(report.get(key)) for key in ('revision', 'branch')):
        return None
    summary = report.get('summary')
    if (not isinstance(summary, dict) or
            any(type(summary.get(key)) is not int or summary[key] < 0 for key in _COUNTS) or
            summary['total'] != sum(summary[key] for key in _COUNTS[:4])):
        raise ValueError('A validated CI report summary is required')
    projected = _project(report, _REPORT_FIELDS)
    projected['provider_url'] = identity[0]
    projected['run_number'] = identity[3]
    projected['summary'] = {key: summary[key] for key in _COUNTS}
    cases = _test_cases(report)
    projected['tests'] = cases
    # Content identity makes replay and repeated uploads idempotent even
    # when optional provider metadata was resolved after ingestion.
    digest = hashlib.sha256(json.dumps([projected['summary'], cases],
                                      sort_keys=True).encode()).hexdigest()
    report_id = report.get('report_id')
    projected['report_id'] = (report_id if isinstance(report_id, str) and
                              re.fullmatch(r'[0-9a-f]{64}', report_id) else digest)
    key = (*identity, projected['branch'], projected['revision'], digest)
    return key, projected


class CIObservationStore:
    """Compact projection reusable by repository replay and observation callbacks.

    Limits apply across retained pipeline/report projections; eviction is oldest
    observation first. Report summaries require an exact provider/run/revision/
    branch match. Legacy reports without a provider URL remain unjoined, rather
    than guessing identity from repository names. Test cases share the existing
    report/byte budgets, while durable uploads remain owned by the repository.
    """

    def __init__(self, *, run_limit=PIPELINE_LIMIT, report_limit=REPORT_LIMIT,
                 byte_limit=HISTORY_BYTES, wall_now=None):
        self._limits = {'pipeline': max(0, min(PIPELINE_LIMIT, run_limit)),
                        'report': max(0, min(REPORT_LIMIT, report_limit))}
        self._byte_limit = max(0, min(HISTORY_BYTES, byte_limit))
        self._wall_now = wall_now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._entries = OrderedDict()
        self._counts = {'pipeline': 0, 'report': 0}
        self._bytes = 0
        self._truncated = False
        self._revision = 0
        self._generated_at = self._wall_now().isoformat()
        self._measurements = _Measurements()

    def _measurement_snapshot(self):
        return self._measurements.snapshot()

    def _remove(self, key):
        _, size = self._entries.pop(key)
        self._bytes -= size
        self._counts[key[0]] -= 1

    def _retain(self, kind, identity, row):
        key, size = (kind, identity), _size(row)
        with self._lock:
            if key in self._entries and self._entries[key][0] == row:
                return
            if key in self._entries:
                self._remove(key)
            self._entries[key] = (row, size)
            self._counts[kind] += 1
            self._bytes += size
            while self._counts[kind] > self._limits[kind]:
                self._remove(next(k for k in self._entries if k[0] == kind))
                self._truncated = True
            while self._bytes > self._byte_limit:
                self._remove(next(iter(self._entries)))
                self._truncated = True
            self._revision += 1
            self._generated_at = self._wall_now().isoformat()

    def record_pipeline(self, row):
        identity = _identity(row)
        if identity is None:
            return False
        projected = _project(row, _PIPELINE_FIELDS)
        projected['provider_url'] = identity[0]
        projected['run_number'] = identity[3]
        projected['state'] = projected['state'] or _project(row, ('status',))['status'] or 'unknown'
        coverage = row.get('coverage') or {}
        projected['coverage'] = {
            **_project(coverage, ('complete_since', 'checked_at', 'error')),
            'complete': False,
            'exhausted': coverage.get('exhausted') is True,
            'truncated': coverage.get('truncated') is True,
        }
        projected['stale'] = row.get('stale') is True
        self._retain('pipeline', identity, projected)
        return True

    def record_report(self, report):
        """Accept normalized counts and bounded parser projections, never XML."""
        return self._record_report_projection(project_report(report))

    def _record_report_projection(self, projection):
        # Projections are produced only by project_report, and are immutable
        # inside the store. Shared replay caches never contain raw reports.
        if projection is None:
            return False
        key, projected = projection
        with self._lock:
            # A replay cannot move the first observation's timestamp or become
            # the latest attempt merely by being delivered again.
            if ('report', key) not in self._entries:
                self._retain('report', key, projected)
        return True

    def summary(self, filters=None):
        """Return detached compact rows using exact identity/branch filters.

        Window aggregation and API filter parsing are supplied by later consumers.
        No arbitrary filter cache is retained and no provider work is triggered.
        """
        filters = filters or {}
        if set(filters) - set(_FILTERS):
            raise ValueError('Unsupported CI summary filter')
        with self._lock:
            rows = []
            reports = {identity[:-1]: row for (kind, identity), (row, _) in self._entries.items()
                       if kind == 'report'}
            for (kind, identity), (row, _) in reversed(self._entries.items()):
                if kind != 'pipeline' or any(
                        value is not None and str(row.get(key)) != str(value)
                        for key, value in filters.items()):
                    continue
                projected = copy.deepcopy(row)
                report = reports.get((*identity, row['branch'], row['revision']))
                projected['report_availability'] = 'reported' if report else 'not_reported'
                projected['test_summary'] = copy.deepcopy(report['summary']) if report else None
                rows.append(projected)
            view = {'revision': self._revision, 'generated_at': self._generated_at,
                    'pipelines': rows,
                    'scope': {'kind': 'retained_observations', 'complete': False, 'partial': True,
                              'memory_only': True, 'truncated': self._truncated,
                              'stale': any(row['stale'] for row in rows),
                              'pipeline_count': len(rows), 'pipeline_limit': self._limits['pipeline'],
                              'report_limit': self._limits['report'], 'byte_limit': self._byte_limit}}
        self._measurements.observe(view, len(rows), view['scope']['stale'])
        return view

    def test_summary(self, repository=None, branch=None, test=None, days=30, *,
                     provider_url=None, provider_repo_id=None, scope='all', start=None, end=None,
                     pipelines=(), inventory=()):
        """Aggregate retained parsed cases in an inclusive UTC report-time window.

        Rates use reported completed test attempts (passed/failed/errored), never
        missing reports or skipped cases. Quantiles require five samples, as in
        CI performance. Retry evidence is capped at five revisions per test;
        inconsistent_count reports the full number of mixed-outcome revisions.
        """
        if type(days) is not int or days not in (7, 14, 30):
            raise ValueError('CI test history days must be 7, 14 or 30')
        for value in (repository, branch, test):
            if value is not None and (not isinstance(value, str) or not value or
                                      len(value) > 500 or not value.isprintable() or
                                      redact_text(value) != value):
                raise ValueError('Invalid CI test history filter')
        now = self._wall_now().timestamp()
        report_start = now - days * 86400
        groups, reported_runs = {}, set()
        report_count = unknown_time = 0
        filters = {'repository': repository, 'branch': branch, 'provider_url': provider_url,
                   'provider_repo_id': provider_repo_id}
        with self._lock:
            runs = {identity: row for (kind, identity), (row, _) in self._entries.items()
                    if kind == 'pipeline'}
            runs.update((identity, row) for row in pipelines if (identity := _identity(row)))
            defaults = {(row.get('provider_url', '').rstrip('/'), str(row.get('id')), row.get('repository')):
                        row.get('default_branch') for row in inventory}
            selected = {(*identity, row['branch'], row['revision']): row for identity, row in runs.items()
                        if not any(value is not None and str(row.get(key)) != str(value)
                                   for key, value in filters.items())
                        and run_in_scope(row, scope, defaults.get(identity[:3]))
                        and run_in_window(row, now, days, start, end)}
            restrict_reports = scope != 'all' or start is not None or end is not None
            for (kind, identity), (report, _) in self._entries.items():
                if kind != 'report' or any(value is not None and str(report[key]) != str(value)
                                          for key, value in filters.items()):
                    continue
                if restrict_reports and identity[:-1] not in selected:
                    continue
                stamp = _timestamp(report['reported_at'])
                if stamp is None:
                    unknown_time += 1
                    continue
                if not report_start <= stamp <= now or report['tests'] is None:
                    continue
                report_count += 1
                reported_runs.add(identity[:-1])
                for case in report['tests']:
                    if test is not None and test not in (case['id'], case['name']):
                        continue
                    key = (*identity[:3], report['branch'], case['id'])
                    groups.setdefault(key, []).append((stamp, report, case))
            missing = 0
            stale = False
            for identity, row in selected.items():
                stale |= bool(row.get('stale'))
                if identity not in reported_runs:
                    missing += 1
            tests = [_aggregate_test(groups[key]) for key in sorted(groups)]
            scope = {'kind': 'retained_observations', 'complete': False, 'partial': True,
                     'memory_only': True, 'persistence': 'memory_only',
                     'truncated': self._truncated, 'stale': stale,
                     'days': days, 'window': {'start': _iso(report_start), 'end': _iso(now)},
                     'scope': scope, 'start': start, 'end': end,
                     'run_window': dict(zip(('start', 'end'), map(_iso, run_window(now, days, start, end)))),
                     'run_time_basis': 'created_at', 'run_window_basis': 'utc_calendar_days_including_today',
                     'repository': repository, 'branch': branch, 'test': test,
                     'time_basis': 'reported_at', 'denominator': 'reported_completed_test_attempts',
                     'minimum_percentile_samples': 5,
                     'report_count': report_count, 'not_reported_run_count': missing,
                     'unknown_time_report_count': unknown_time,
                     'evidence_availability': 'reported' if report_count else 'not_reported',
                     'report_limit': self._limits['report'], 'byte_limit': self._byte_limit}
            return copy.deepcopy({'tests': tests, 'scope': scope})
