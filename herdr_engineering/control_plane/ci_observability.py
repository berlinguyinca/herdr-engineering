"""Bounded, memory-only projections of CI repository/event payloads.

Callers supply observed pipelines and normalized reports; this module performs no
provider reads or persistence. It never treats retained observations as complete
history. Raw details, XML, test traces and logs do not belong in this store.
"""
from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .ci_reports import HISTORY_BYTES, PIPELINE_LIMIT, REPORT_LIMIT
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
                    'revision', 'branch', 'state', 'created_at', 'started_at', 'finished_at')
_REPORT_FIELDS = ('provider_url', 'provider_repo_id', 'repository', 'run_number',
                  'revision', 'branch', 'reported_at', 'ingested_at')
_COUNTS = ('passed', 'failed', 'errored', 'skipped', 'total', 'duration_ms')
_FILTERS = ('provider_url', 'provider_repo_id', 'repository', 'branch')


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


class CIObservationStore:
    """Compact projection reusable by repository replay and observation callbacks.

    Limits apply across retained pipeline/report projections; eviction is oldest
    observation first. Report summaries require an exact provider/run/revision/
    branch match. Legacy reports without a provider URL remain unjoined, rather
    than guessing identity from repository names. Test-case history belongs to
    the report repository, not this initial run-summary projection.
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
        """Accept a normalized report summary, never XML or raw test evidence."""
        identity = _identity(report)
        if identity is None or any(not _joinable(report.get(key)) for key in ('revision', 'branch')):
            return False
        summary = report.get('summary')
        if (not isinstance(summary, dict) or
                any(type(summary.get(key)) is not int or summary[key] < 0 for key in _COUNTS) or
                summary['total'] != sum(summary[key] for key in _COUNTS[:4])):
            raise ValueError('A validated CI report summary is required')
        projected = _project(report, _REPORT_FIELDS)
        projected['provider_url'] = identity[0]
        projected['run_number'] = identity[3]
        projected['summary'] = {key: summary[key] for key in _COUNTS}
        self._retain('report', (*identity, projected['branch'], projected['revision']), projected)
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
            for (kind, identity), (row, _) in reversed(self._entries.items()):
                if kind != 'pipeline' or any(
                        value is not None and str(row.get(key)) != str(value)
                        for key, value in filters.items()):
                    continue
                projected = copy.deepcopy(row)
                report_key = ('report', (*identity, row['branch'], row['revision']))
                report = self._entries.get(report_key)
                projected['report_availability'] = 'reported' if report else 'not_reported'
                projected['test_summary'] = copy.deepcopy(report[0]['summary']) if report else None
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
