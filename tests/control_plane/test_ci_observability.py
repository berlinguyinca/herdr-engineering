"""Contracts for compact projections of existing CI observation/report payloads."""
import json
from datetime import datetime

import pytest

from herdr_engineering.control_plane.ci_reports import normalize_report

STAMP = '2026-10-25T12:00:00+00:00'


def pipeline(**values):
    return {'provider_url': 'https://ci.example', 'provider_repo_id': 17,
            'repository': 'org/app', 'run_number': 1, 'revision': 'abc',
            'branch': 'main', 'state': 'failure', 'created_at': STAMP,
            'started_at': STAMP, 'finished_at': STAMP, **values}


@pytest.fixture
def store():
    from herdr_engineering.control_plane.ci_observability import CIObservationStore
    return CIObservationStore(wall_now=lambda: datetime.fromisoformat(STAMP))


def report(**values):
    return {**normalize_report({**pipeline(), 'xml':
        '<testsuite><testcase name="ok"/><testcase name="bad"><failure>oops</failure>'
        '</testcase></testsuite>'}, STAMP), 'provider_url': 'https://ci.example', **values}


def test_failed_pipeline_without_report_preserves_unknown_test_counts(store):
    store.record_pipeline(pipeline())
    view = store.summary({})
    row = view['pipelines'][0]
    assert {key: row[key] for key in pipeline()} == pipeline()
    assert row['report_availability'] == 'not_reported'
    assert row['test_summary'] is None
    assert row['coverage']['complete'] is False
    assert view['scope']['partial'] is True
    assert view['scope']['complete'] is False
    assert view['scope']['memory_only'] is True


def test_only_compact_redacted_fields_are_retained(store):
    store.record_pipeline(pipeline(branch='token=synthetic-sensitive-value',
                                   logs='RAW LOG', workflows=[{'steps': ['RAW DETAIL']}],
                                   token='DO NOT RETAIN'))
    store.record_report(report(xml='RAW XML', tests=[{'trace': 'RAW TRACE'}]))
    view = store.summary({})
    serialized = json.dumps(view)
    assert 'synthetic-sensitive-value' not in serialized
    assert all(value not in serialized for value in ('RAW LOG', 'RAW DETAIL', 'RAW XML',
                                                    'RAW TRACE', 'DO NOT RETAIN'))
    assert view['pipelines'][0]['branch'] == '[REDACTED]'
    assert view['pipelines'][0]['report_availability'] == 'not_reported'


def test_report_counts_join_only_exact_run_branch_revision_and_provider(store):
    store.record_pipeline(pipeline())
    for changes in ({'provider_url': 'https://other.example'}, {'provider_repo_id': 18},
                    {'revision': 'different'}, {'branch': 'topic'}, {'provider_url': None}):
        store.record_report(report(**changes))
    assert store.summary({})['pipelines'][0]['test_summary'] is None
    store.record_report(report())
    row = store.summary({})['pipelines'][0]
    assert row['report_availability'] == 'reported'
    assert row['test_summary'] == {'passed': 1, 'failed': 1, 'errored': 0, 'skipped': 0,
                                   'total': 2, 'duration_ms': 0}
    store.record_report(report())
    assert store.summary({})['pipelines'][0]['test_summary']['total'] == 2


def test_same_named_repositories_and_provider_ids_never_merge(store):
    store.record_pipeline(pipeline())
    store.record_pipeline(pipeline(provider_url='https://other.example', state='running'))
    store.record_pipeline(pipeline(provider_repo_id=18, state='skipped'))
    rows = store.summary({})['pipelines']
    assert len(rows) == 3
    assert {row['state'] for row in rows} == {'failure', 'running', 'skipped'}
    selected = store.summary({'provider_url': 'https://ci.example', 'provider_repo_id': '17'})
    assert len(selected['pipelines']) == 1
    assert selected['pipelines'][0]['state'] == 'failure'


def test_filters_and_detached_snapshots_do_not_mutate_store(store):
    store.record_pipeline(pipeline())
    store.record_pipeline(pipeline(run_number=2, branch='topic'))
    view = store.summary({'repository': 'org/app', 'branch': 'main'})
    assert len(view['pipelines']) == 1
    view['pipelines'][0]['state'] = 'success'
    assert store.summary({'branch': 'main'})['pipelines'][0]['state'] == 'failure'
    assert store.summary({'repository': 'org/missing'})['pipelines'] == []


def test_retention_bounds_and_eviction_are_explicit():
    from herdr_engineering.control_plane.ci_observability import CIObservationStore
    store = CIObservationStore(run_limit=2, report_limit=1, byte_limit=4096)
    for number in range(1, 4):
        store.record_pipeline(pipeline(run_number=number))
    assert [row['run_number'] for row in store.summary({})['pipelines']] == [3, 2]
    assert store.summary({})['scope']['truncated'] is True
    store.record_report(report(run_number=2))
    store.record_report(report(run_number=3))
    rows = store.summary({})['pipelines']
    assert rows[0]['report_availability'] == 'reported'
    assert rows[1]['report_availability'] == 'not_reported'
    tiny = CIObservationStore(byte_limit=1)
    tiny.record_pipeline(pipeline())
    assert tiny.summary({})['pipelines'] == []
    assert tiny.summary({})['scope']['truncated'] is True


def test_partial_coverage_stays_explicit_and_redacted(store):
    store.record_pipeline(pipeline(coverage={'complete_since': STAMP, 'exhausted': False,
        'truncated': True, 'error': 'token=synthetic-sensitive-value', 'checked_at': STAMP},
        stale=True))
    view = store.summary({})
    assert view['scope']['stale'] is True
    assert view['scope']['partial'] is True
    assert view['pipelines'][0]['coverage']['truncated'] is True
    assert view['pipelines'][0]['coverage']['checked_at'] == STAMP
    assert 'synthetic-sensitive-value' not in json.dumps(view)


def test_measurements_are_private_and_summary_reads_do_not_access_provider(store):
    store.record_pipeline(pipeline())
    first = store.summary({})
    second = store.summary({})
    assert first == second
    measurements = store._measurement_snapshot()
    assert measurements['provider_calls'] == 0
    assert measurements['summary_rows'] == 1
    assert measurements['cache_hits'] == 2
    assert measurements['payload_bytes'] == len(json.dumps(second, separators=(',', ':')).encode())
    assert measurements['snapshot_at'] is not None
    assert 'measurements' not in second and 'provider_calls' not in json.dumps(second)
    measurements['provider_calls'] = 99
    assert store._measurement_snapshot()['provider_calls'] == 0


def test_report_before_observation_is_joined_without_claiming_execution_success(store):
    store.record_report(report())
    assert store.summary({})['pipelines'] == []
    store.record_pipeline(pipeline(state='canceled'))
    row = store.summary({})['pipelines'][0]
    assert row['state'] == 'canceled'
    assert row['test_summary']['passed'] == 1


@pytest.mark.parametrize('changes', [
    {'provider_url': 'https://user:synthetic-sensitive-value@ci.example'},
    {'provider_url': 'https://ci.example?token=synthetic-sensitive-value'},
    {'repository': 'token=synthetic-sensitive-value'},
    {'provider_repo_id': 'token=synthetic-sensitive-value'},
])
def test_unsafe_identity_is_not_retained_even_in_internal_keys(store, changes):
    assert store.record_pipeline(pipeline(**changes)) is False
    assert store.record_report(report(**changes)) is False
    assert store.summary({})['pipelines'] == []
    assert 'synthetic-sensitive-value' not in repr(store._entries)


def test_redaction_cannot_create_a_false_report_match(store):
    store.record_pipeline(pipeline(revision='token=synthetic-first-value'))
    store.record_report(report(revision='token=synthetic-second-value'))
    assert store.summary({})['pipelines'][0]['report_availability'] == 'not_reported'


def test_identical_observations_are_idempotent_and_changed_outcomes_replace(store):
    store.record_pipeline(pipeline())
    first = store.summary({})
    store.record_pipeline(pipeline())
    assert store.summary({}) == first
    store.record_pipeline(pipeline(state='success'))
    changed = store.summary({})
    assert changed['revision'] > first['revision']
    assert len(changed['pipelines']) == 1
    assert changed['pipelines'][0]['state'] == 'success'


def test_report_projection_rejects_unvalidated_counts(store):
    for summary in ({}, {'passed': True, 'failed': 0, 'errored': 0, 'skipped': 0,
                        'total': 1, 'duration_ms': 0},
                    {'passed': 2, 'failed': 0, 'errored': 0, 'skipped': 0,
                     'total': 1, 'duration_ms': 0}):
        with pytest.raises(ValueError, match='validated'):
            store.record_report(report(summary=summary))
    store.record_pipeline(pipeline())
    assert store.summary({})['pipelines'][0]['test_summary'] is None


def test_combined_byte_budget_bounds_pipeline_and_report_projections():
    from herdr_engineering.control_plane.ci_observability import CIObservationStore
    store = CIObservationStore(byte_limit=1500)
    for number in range(1, 30):
        store.record_pipeline(pipeline(run_number=number))
        store.record_report(report(run_number=number))
    assert store._bytes <= 1500
    assert store._bytes == sum(len(json.dumps(row, separators=(',', ':')).encode())
                               for row, _ in store._entries.values())
    assert store.summary({})['scope']['truncated'] is True
