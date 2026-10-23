"""Bounded CI telemetry and deterministic retained-observation statistics.

Cache v1 has no event timestamp or branch: its window uses ingestion time.
Wrapped phase timings and provider start-to-finish durations remain distinct.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 16384
MAX_RECORDS = 200
RECORD_LIMIT = 10000
HISTORY_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 31536000
MAX_TIMESTAMP = 253402300799
STALE_SECONDS = 86400
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
PHASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
OPERATION_ID = re.compile(r'[0-9a-f]{64}\Z')
CACHE_REQUIRED = {'schema_version', 'operation_id', 'phase', 'kind', 'operation', 'outcome'}
CACHE_OPTIONAL = {'repo', 'pipeline', 'wall_seconds', 'compressed_bytes', 'expanded_bytes',
                  'entry_count', 'error_code'}
TIMING_FIELDS = {'schema', 'phase', 'metadata', 'started_at', 'finished_at', 'wall_seconds',
                 'cpu_seconds', 'exit_status', 'record_type', 'operation_id'}


def _number(value, maximum):
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def _identifier(value, pattern=IDENTIFIER):
    return isinstance(value, str) and '..' not in value and pattern.fullmatch(value)


def _metadata(values):
    if not isinstance(values, dict) or values.keys() - {'repo', 'pipeline', 'workflow'}:
        return False
    for key, value in values.items():
        if key == 'repo':
            if not isinstance(value, str) or len(value.split('/')) != 2 or not all(
                    _identifier(part) for part in value.split('/')):
                return False
        elif key == 'pipeline':
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value):
                return False
        elif not _identifier(value):
            return False
    return True


def validate_ci_telemetry(payload):
    """Reject an entire invalid envelope; retain only the v1 producer allowlist."""
    def invalid():
        raise ValueError('Invalid CI telemetry bundle.')

    try:
        size = len(json.dumps(payload, separators=(',', ':'), allow_nan=False).encode())
    except (TypeError, ValueError, OverflowError, RecursionError):
        invalid()
    if (size > MAX_REQUEST_BYTES or not isinstance(payload, dict)
            or payload.keys() != {'schema_version', 'records'}
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or not isinstance(payload['records'], list) or len(payload['records']) > MAX_RECORDS):
        invalid()
    normalized = []
    for row in payload['records']:
        if (not isinstance(row, dict) or len(json.dumps(row).encode()) > MAX_RECORD_BYTES
                or not _identifier(row.get('phase'), PHASE)
                or not isinstance(row.get('operation_id'), str)
                or not OPERATION_ID.fullmatch(row['operation_id'])):
            invalid()
        if row.get('schema_version') == 'cache-operation-v1':
            if (not CACHE_REQUIRED <= row.keys() or row.keys() - CACHE_REQUIRED - CACHE_OPTIONAL
                    or row['kind'] not in ('go', 'cargo', 'maven')
                    or row['operation'] not in ('restore', 'save')
                    or row['outcome'] not in (('hit', 'miss', 'cold', 'failed')
                        if row['operation'] == 'restore' else ('saved', 'failed'))):
                invalid()
            metadata = {key: row[key] for key in ('repo', 'pipeline') if key in row}
            if not _metadata(metadata):
                invalid()
            for field in ('compressed_bytes', 'expanded_bytes', 'entry_count'):
                if field in row and (type(row[field]) is not int or not 0 <= row[field] <= 2**53 - 1):
                    invalid()
            if ('wall_seconds' in row and not _number(row['wall_seconds'], MAX_SECONDS)
                    or 'error_code' in row and row['error_code'] != 'timeout'):
                invalid()
            record_type = 'cache-operation-v1'
        else:
            if (row.keys() != TIMING_FIELDS or row['record_type'] != 'timing-v1'
                    or type(row['schema']) is not int or row['schema'] != 1
                    or not _metadata(row['metadata'])
                    or not all(_number(row[key], MAX_TIMESTAMP) for key in ('started_at', 'finished_at'))
                    or row['finished_at'] < row['started_at']
                    or not all(_number(row[key], MAX_SECONDS) for key in ('wall_seconds', 'cpu_seconds'))
                    or type(row['exit_status']) is not int or not 0 <= row['exit_status'] <= 255):
                invalid()
            metadata = row['metadata']
            record_type = 'timing-v1'
        normalized.append(dict(copy.deepcopy(row), record_type=record_type,
                               repository=metadata.get('repo'), branch=None,
                               run_number=int(metadata['pipeline']) if 'pipeline' in metadata else None))
    return normalized


def _timestamp(value):
    if _number(value, MAX_TIMESTAMP):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                result = parsed.timestamp()
                return result if _number(result, MAX_TIMESTAMP) else None
        except (ValueError, OverflowError):
            pass
    return None


def _iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def _stats(values):
    values = sorted(values)
    count = len(values)
    return dict(sample_count=count, p50=values[math.ceil(count * .5) - 1] if count >= 5 else None,
                p95=values[math.ceil(count * .95) - 1] if count >= 5 else None,
                mean=sum(values) / count if count else None, insufficient_data=count < 5)


def performance(records, pipelines, *, now, persistence, repository=None, branch=None, days=14):
    if type(days) is not int or days not in (7, 14):
        raise ValueError('CI performance days must be 7 or 14.')
    for value in (repository, branch):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 500
                                  or not value.isprintable()):
            raise ValueError('Invalid CI performance filter.')
    start, baseline_start = now - days * 86400, now - 2 * days * 86400
    window = {'start': _iso(start), 'end': _iso(now)}
    # Retained exact repository/run matches may enrich identity, never branch or time.
    by_run = defaultdict(list)
    for row in pipelines:
        by_run[(row.get('repository'), row.get('run_number'))].append(row)
    identity_fields = ('provider_repo_id', 'provider_url', 'repository_id', 'forge_host',
                       'project_id', 'revision', 'related_sessions', 'related_missions')
    samples = []
    for original in records:
        row = copy.deepcopy(original)
        candidates = by_run.get((row.get('repository'), row.get('run_number')), [])
        if len(candidates) == 1:
            for field in identity_fields:
                if field in candidates[0]:
                    row[field] = candidates[0][field]
        cache = row['record_type'] == 'cache-operation-v1'
        row['observed_at'] = row.get('ingested_at') if cache else _iso(row['finished_at'])
        row['measurement_source'] = 'cache_operation' if cache else 'wrapped_phase'
        row['time_source'] = 'ingestion_time' if cache else 'finished_at'
        samples.append(row)
    for original in pipelines:
        began, finished = _timestamp(original.get('started_at')), _timestamp(original.get('finished_at'))
        if began is None or finished is None or not 0 <= finished - began <= MAX_SECONDS:
            continue
        samples.append(dict(copy.deepcopy(original), record_type='provider-execution',
                            operation_id=f"provider:{original.get('provider_repo_id')}:{original.get('run_number')}",
                            observed_at=_iso(finished), wall_seconds=finished - began,
                            phase='pipeline', measurement_source='provider_execution', time_source='finished_at'))
    selected = [row for row in samples if (repository is None or row.get('repository') == repository)
                and (branch is None or row.get('branch') == branch)]
    cache_groups, executions = defaultdict(list), defaultdict(list)
    for row in selected:
        timestamp = _timestamp(row.get('observed_at'))
        if timestamp is None or not baseline_start <= timestamp <= now:
            continue
        identity = (row.get('repository'), row.get('branch'), row.get('project_id'), row.get('provider_repo_id'))
        if row['record_type'] == 'cache-operation-v1':
            if timestamp >= start:
                cache_groups[(*identity, row['kind'])].append(row)
        else:
            executions[(*identity, row['phase'], row['measurement_source'])].append(row)
    groups, trends, alerts = [], [], []

    def metadata(row):
        return {key: copy.deepcopy(row.get(key)) for key in (
            'repository', 'branch', *identity_fields)}

    def alert(group, rule, metric, value, baseline, count, baseline_count=0):
        alerts.append(dict(metadata(group), rule=rule, kind=group.get('kind'), metric=metric,
                           phase=group.get('phase'), measurement_source=group.get('measurement_source'),
                           current_value=value, baseline_value=baseline, sample_count=count,
                           baseline_sample_count=baseline_count, window=window))

    for _, rows in sorted(cache_groups.items(), key=lambda item: str(item[0])):
        restore = [row for row in rows if row['operation'] == 'restore']
        hits = sum(row['outcome'] == 'hit' for row in restore)
        group = dict(metadata(rows[0]), kind=rows[0]['kind'], sample_count=len(rows),
                     hit_count=hits, restore_count=len(restore), hit_rate=hits / len(restore) if restore else None,
                     measurement_source='cache_operation', time_source='ingestion_time')
        for operation in ('restore', 'save'):
            stats = _stats([row['wall_seconds'] for row in rows
                            if row['operation'] == operation and 'wall_seconds' in row])
            group[operation] = stats
            if stats['p95'] is not None and stats['p95'] >= 81:
                alert(group, 'cache_budget', operation + '_p95', stats['p95'], 90, stats['sample_count'])
        group['insufficient_data'] = all(group[op]['insufficient_data'] for op in ('restore', 'save'))
        if len(restore) >= 10 and group['hit_rate'] < .5:
            alert(group, 'low_hit_rate', 'hit_rate', group['hit_rate'], .5, len(restore))
        groups.append(group)
    for _, rows in sorted(executions.items(), key=lambda item: str(item[0])):
        current = [row for row in rows if _timestamp(row['observed_at']) >= start]
        if not current:
            continue
        baseline = _stats([row['wall_seconds'] for row in rows if _timestamp(row['observed_at']) < start])
        stats = _stats([row['wall_seconds'] for row in current])
        trend = dict(metadata(current[0]), **stats, baseline=baseline, phase=current[0]['phase'],
                     measurement_source=current[0]['measurement_source'],
                     samples=sorted(current, key=lambda row: (row['observed_at'], row['operation_id'])))
        if stats['p95'] is not None and baseline['p95'] is not None and stats['p95'] >= 2 * baseline['p95']:
            alert(trend, 'execution_regression', 'execution_p95', stats['p95'], baseline['p95'],
                  stats['sample_count'], baseline['sample_count'])
        trends.append(trend)
    observed = [stamp for row in selected if (stamp := _timestamp(row.get('observed_at'))) is not None and stamp <= now]
    latest = max(observed, default=None)
    scope = dict(kind='retained_observations', complete=False, partial=True,
                 stale=latest is None or now - latest >= STALE_SECONDS, stale_after_seconds=STALE_SECONDS,
                 persistence=persistence, memory_only=persistence == 'memory_only',
                 record_limit=RECORD_LIMIT, record_count=len(records), byte_limit=HISTORY_BYTES,
                 description='Retained uploads and observed provider runs only; missing measurements are not zero. '
                             'Cache v1 windows use ingestion time; branch is unknown. Deduplication covers retained operations.')
    coverage = dict(complete=False, partial=True, cache_time_source='ingestion_time',
                    cache_branch='unknown', latest_observed_at=_iso(latest) if latest is not None else None,
                    selected_sample_count=len(selected), minimum_percentile_samples=5,
                    maintenance_budget_seconds=90, baseline_window={'start': _iso(baseline_start), 'end': _iso(start)})
    return dict(schema_version=1, days=days, window=window, cache_groups=groups,
                execution_trends=trends, alerts=alerts, scope=scope, coverage=coverage)


def annotate_performance(view, repo, inventory, languages):
    """Reuse CI project annotation without provider requests or stored-row mutation."""
    from .project_links import annotate_ci_history

    result = copy.deepcopy(view)
    for key in ('cache_groups', 'execution_trends', 'alerts'):
        annotated = annotate_ci_history({'repos': inventory, 'pipelines': result[key]}, repo,
                                        repository_languages=languages)
        result[key] = annotated['pipelines']
        result['project_mapping_status'] = annotated['project_mapping_status']
        result['project_mapping_error'] = annotated['project_mapping_error']
    for trend in result['execution_trends']:
        trend['samples'] = annotate_ci_history({'repos': inventory, 'pipelines': trend['samples']}, repo,
                                               repository_languages=languages)['pipelines']
    return result
