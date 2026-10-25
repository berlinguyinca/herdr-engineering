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


def case_report(*, status='passed', seconds=1, **values):
    detail = '<failure message="token=synthetic-private">bounded stack</failure>' if status == 'failed' else '<skipped/>' if status == 'skipped' else ''
    return normalize_report({**pipeline(), 'xml':
        f'<testsuite name="unit"><testcase classname="Checks" name="works" time="{seconds}">{detail}</testcase></testsuite>',
        **values}, STAMP)


def test_test_history_counts_timestamps_quantiles_and_bounded_evidence(store):
    for number in range(1, 7):
        store.record_report(case_report(run_number=number, revision=str(number), seconds=number,
            status='failed' if number in (1, 3) else 'passed',
            reported_at=f'2026-10-{18 + number:02d}T12:00:00+00:00'))
    view = store.test_summary(repository='org/app', branch='main', days=7)
    row = view['tests'][0]
    assert row['passed'] == 4 and row['failed'] == 2 and row['completed_count'] == 6
    assert row['failure_rate'] == pytest.approx(2 / 6)
    assert row['first_failure'] == '2026-10-19T12:00:00+00:00'
    assert row['last_pass'] == '2026-10-24T12:00:00+00:00'
    assert row['last_duration_ms'] == 6000
    assert row['duration_ms']['sample_count'] == 6
    assert row['duration_ms']['p50'] == 3000 and row['duration_ms']['p95'] == 6000
    assert row['classification'] == 'inconsistent_outcomes'
    assert row['evidence_availability'] == 'reported'
    assert row['failure_evidence']['message'] == '[REDACTED]'
    assert row['failure_evidence']['trace'] == 'bounded stack'
    assert {ref['run_number'] for ref in row['representative_runs']} == {1, 3, 6}
    assert 'synthetic-private' not in json.dumps(view)
    row['representative_runs'].clear()
    assert store.test_summary()['tests'][0]['representative_runs']


def test_retry_evidence_is_deduplicated_and_requires_same_revision(store):
    failed = case_report(status='failed')
    passed = case_report()
    store.record_report(failed)
    store.record_report(failed)
    store.record_report(passed)
    store.record_report(passed)
    row = store.test_summary()['tests'][0]
    assert row['classification'] == 'proven_flaky'
    assert row['passed'] == row['failed'] == row['inconsistent_count'] == 1
    assert row['same_revision_retries'][0]['revision'] == 'abc'
    assert len(row['same_revision_retries'][0]['runs']) == 2
    assert row['duration_ms']['p95'] is None
    assert row['duration_ms']['sample_count'] == 2


def test_skipped_reports_and_absent_reports_are_not_success_or_failure(store):
    store.record_pipeline(pipeline())
    missing = store.test_summary()
    assert missing['tests'] == []
    assert missing['scope']['not_reported_run_count'] == 1
    assert missing['scope']['evidence_availability'] == 'not_reported'
    store.record_report(case_report(status='skipped'))
    row = store.test_summary()['tests'][0]
    assert row['completed_count'] == 0 and row['failure_rate'] is None
    assert row['classification'] == 'stable'
    assert row['first_failure'] is None and row['last_pass'] is None
    assert row['duration_ms']['sample_count'] == 0


def test_test_history_keeps_providers_and_branches_separate(store):
    store.record_report(case_report(status='failed'))
    store.record_report(case_report(provider_url='https://other.example'))
    store.record_report(case_report(provider_repo_id=18))
    store.record_report(case_report(branch='topic'))
    rows = store.test_summary()['tests']
    assert len(rows) == 4
    assert {row['classification'] for row in rows} == {'stable'}
    assert len(store.test_summary(branch='main')['tests']) == 3
    assert store.test_summary(test='nonexistent')['tests'] == []
    assert len(store.test_summary(test=rows[0]['id'])['tests']) == 4


def test_test_history_window_is_utc_inclusive_and_excludes_future_and_unknown_times(store):
    for number, stamp in enumerate(('2026-10-18T05:00:00-07:00',
                                   '2026-10-18T11:59:59+00:00', STAMP,
                                   '2026-10-25T12:00:01+00:00'), 1):
        store.record_report(case_report(run_number=number, reported_at=stamp))
    store.record_report({**case_report(run_number=5), 'reported_at': None})
    row = store.test_summary(days=7)['tests'][0]
    assert row['passed'] == 2
    assert store.test_summary(days=7)['scope']['unknown_time_report_count'] == 1


@pytest.mark.parametrize('filters', [{'days': True}, {'days': 0}, {'days': 31},
                                     {'test': ''}, {'branch': []}, {'repository': 'x' * 501}])
def test_test_history_rejects_invalid_filters(store, filters):
    with pytest.raises(ValueError):
        store.test_summary(**filters)


def test_test_cases_consume_the_existing_shared_retention_budget():
    from herdr_engineering.control_plane.ci_observability import CIObservationStore
    bounded = CIObservationStore(report_limit=2, byte_limit=4096,
                                 wall_now=lambda: datetime.fromisoformat(STAMP))
    for number in range(1, 10):
        bounded.record_report(case_report(run_number=number))
    view = bounded.test_summary()
    assert view['tests'][0]['passed'] == 2
    assert view['scope']['truncated'] is True
    assert bounded._bytes <= 4096


def test_test_history_does_not_echo_secret_filter_values(store):
    with pytest.raises(ValueError):
        store.test_summary(repository='token=synthetic-private')


def test_invalid_case_projection_cannot_become_test_history(store):
    invalid = case_report()
    invalid['tests'][0]['status'] = 'failed'
    store.record_pipeline(pipeline())
    store.record_report(invalid)
    view = store.test_summary()
    assert view['tests'] == []
    assert view['scope']['evidence_availability'] == 'not_reported'
    assert view['scope']['not_reported_run_count'] == 1


def test_replayed_equivalent_reports_do_not_erase_first_failure_time(store):
    first = case_report(status='failed', reported_at='2026-10-20T00:00:00Z')
    store.record_report(first)
    store.record_report({**first, 'reported_at': STAMP, 'ingested_at': STAMP})
    row = store.test_summary()['tests'][0]
    assert row['failed'] == 1
    assert row['first_failure'] == '2026-10-20T00:00:00+00:00'


def test_latest_run_summary_preserves_retry_outcome_when_old_upload_is_replayed(store):
    store.record_pipeline(pipeline())
    first = case_report(status='failed')
    store.record_report(first)
    store.record_report(case_report())
    store.record_report(first)
    assert store.summary()['pipelines'][0]['test_summary']['passed'] == 1
