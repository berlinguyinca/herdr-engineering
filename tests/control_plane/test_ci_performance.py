import hashlib
import json
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1800000000
DAY = 86400


def cache(number=1, **overrides):
    row = dict(schema_version='cache-operation-v1', operation_id=f'{number:064x}',
                repo='org/app', pipeline='1', phase='build', kind='go', operation='restore',
                outcome='hit', wall_seconds=2)
    return {**row, **overrides}


def timing(number=1, *, finished=NOW, wall=10):
    row = dict(schema=1, phase='build', metadata={'repo': 'org/app', 'pipeline': str(number)},
               started_at=finished - wall, finished_at=finished,
               wall_seconds=wall, cpu_seconds=wall / 2, exit_status=0)
    digest = hashlib.sha256(b'timing-v1\0' + json.dumps(
        row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(row, record_type='timing-v1', operation_id=digest)


def validate(records):
    from herdr_engineering.control_plane.ci_performance import validate_ci_telemetry
    return validate_ci_telemetry({'schema_version': 1, 'records': records})


def ingest(repo, *records):
    return repo.record_ci_telemetry(validate(list(records)))


def test_schema_accepts_real_relay_shapes_without_filling_missing_metrics():
    missing = cache()
    del missing['wall_seconds']
    rows = validate([missing, timing()])
    assert 'wall_seconds' not in rows[0]
    assert rows[0]['repository'] == 'org/app'
    assert rows[0]['branch'] is None
    assert rows[1]['wall_seconds'] == 10
    assert rows[1]['record_type'] == 'timing-v1'


@pytest.mark.parametrize('change', [
    {'schema_version': 2}, {'wall_seconds': float('nan')}, {'wall_seconds': True},
    {'wall_seconds': -1}, {'wall_seconds': 31536001}, {'compressed_bytes': 2**53},
    {'expanded_bytes': 1.2}, {'entry_count': -1}, {'operation_id': 'secret'},
    {'kind': []}, {'outcome': 'saved'}, {'repo': '/private/file'},
    {'phase': '../secrets'}, {'environment': {'TOKEN': 'secret'}}, {'wall_seconds': 10**400},
])
def test_rejects_whole_invalid_bundle(change):
    repo = ControlPlaneRepo(now=lambda: NOW)
    with pytest.raises(ValueError):
        ingest(repo, cache(1), cache(2, **change))
    assert not repo._events


@pytest.mark.parametrize('change', [
    {'schema': True}, {'finished_at': -1}, {'finished_at': 253402300800},
    {'started_at': NOW + 1}, {'cpu_seconds': float('inf')}, {'exit_status': 256},
    {'metadata': {'repo': 'org/app', 'token': 'secret'}}, {'record_type': 'other'},
])
def test_rejects_invalid_timing_records(change):
    with pytest.raises(ValueError):
        validate([{**timing(), **change}])


def test_duplicates_restore_and_scope_are_retained():
    repo = ControlPlaneRepo(now=lambda: NOW)
    result = ingest(repo, cache(), cache(), timing())
    assert (result['accepted'], result['duplicates']) == (2, 1)
    assert result['scope']['memory_only'] is True
    assert result['scope']['partial'] is True
    restored = ControlPlaneRepo(now=lambda: NOW)
    restored.import_state(repo.export_state())
    assert ingest(restored, cache(), timing())['accepted'] == 0
    assert restored.ci_performance()['scope']['record_count'] == 2
    assert len(restored.stream('ci_telemetry', cache()['operation_id'])) == 1


def test_conflicting_duplicate_does_not_write_partial_bundle():
    repo = ControlPlaneRepo(now=lambda: NOW)
    ingest(repo, cache())
    with pytest.raises(ValueError):
        ingest(repo, cache(2), cache(1, wall_seconds=99))
    assert repo.ci_performance()['scope']['record_count'] == 1


def test_nearest_rank_missing_data_hit_rate_and_budget_alerts():
    repo = ControlPlaneRepo(now=lambda: NOW)
    for number, seconds in enumerate([1, 2, 3, 4, 81, 6, 7, 8, 9, 90], 1):
        ingest(repo, cache(number, wall_seconds=seconds, outcome='hit' if number < 5 else 'miss'))
    for number in range(11, 16):
        ingest(repo, cache(number, operation='save', outcome='saved', wall_seconds=81))
    view = repo.ci_performance()
    group = view['cache_groups'][0]
    assert group['restore'] == dict(sample_count=10, p50=6, p95=90, mean=21.1,
                                    insufficient_data=False)
    assert group['save']['p95'] == 81
    assert group['hit_rate'] == 0.4
    assert group['restore_count'] == 10
    assert {(a['rule'], a['metric']) for a in view['alerts']} == {
        ('cache_budget', 'restore_p95'), ('cache_budget', 'save_p95'), ('low_hit_rate', 'hit_rate')}
    assert all(a['window'] == view['window'] for a in view['alerts'])


def test_small_groups_do_not_invent_percentiles_or_alerts():
    repo = ControlPlaneRepo(now=lambda: NOW)
    for number in range(4):
        ingest(repo, cache(number, wall_seconds=89, outcome='miss'))
    missing = cache(10)
    del missing['wall_seconds']
    ingest(repo, missing)
    view = repo.ci_performance()
    group = view['cache_groups'][0]
    assert group['sample_count'] == 5
    assert group['restore']['sample_count'] == 4
    assert group['restore']['p50'] is None
    assert group['restore']['p95'] is None
    assert group['save']['mean'] is None
    assert group['insufficient_data']
    assert view['alerts'] == []


def test_windows_are_half_open_baselines_comparable_and_stale_explicit():
    clock = [NOW - 8 * DAY]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    ingest(repo, cache())
    clock[0] = NOW
    for number in range(5):
        ingest(repo, timing(number, finished=NOW - 8 * DAY, wall=10),
               timing(number + 10, finished=NOW - DAY, wall=20))
    ingest(repo, timing(40, finished=NOW - 14 * DAY - 1, wall=1000),
           timing(41, finished=NOW - 7 * DAY, wall=20),
           timing(42, finished=NOW + 1, wall=999))
    week = repo.ci_performance(days=7)
    trend = week['execution_trends'][0]
    assert not week['cache_groups']
    assert trend['sample_count'] == 6
    assert trend['baseline']['sample_count'] == 5
    assert trend['p95'] == 20
    assert week['alerts'][0]['rule'] == 'execution_regression'
    assert week['alerts'][0]['baseline_value'] == 10
    fortnight = repo.ci_performance(days=14)
    assert len(fortnight['cache_groups']) == 1
    assert fortnight['execution_trends'][0]['sample_count'] == 11
    assert not fortnight['alerts']
    assert fortnight['scope']['stale'] is True
    assert repo.ci_performance(repository='other/repo')['execution_trends'] == []
    assert repo.ci_performance(branch='main')['cache_groups'] == []
    with pytest.raises(ValueError):
        repo.ci_performance(days=30)


def test_provider_execution_excludes_queue_and_remains_separate_from_phase_timing():
    repo = ControlPlaneRepo(now=lambda: NOW)
    def iso(seconds):
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    for number in range(5):
        repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=17,
            run_number=number, branch='main', created_at=iso(NOW - 1000),
            started_at=iso(NOW - 100), finished_at=iso(NOW - 20), state='success'))
    ingest(repo, timing(wall=5))
    trend = repo.ci_performance(branch='main')['execution_trends'][0]
    assert trend['p95'] == 80
    assert trend['measurement_source'] == 'provider_execution'
    assert trend['sample_count'] == 5
    assert len(repo.ci_performance()['execution_trends']) == 2


def test_retention_bounds_replay_and_discloses_missing_event_times(monkeypatch):
    from herdr_engineering.control_plane import ci_performance
    monkeypatch.setattr(ci_performance, 'RECORD_LIMIT', 2)
    repo = ControlPlaneRepo(now=lambda: NOW)
    ingest(repo, cache(1), cache(2), cache(3))
    state = repo.export_state()
    restored = ControlPlaneRepo(now=lambda: NOW)
    restored.import_state(state)
    view = restored.ci_performance()
    assert view['scope']['record_count'] == 2
    assert view['scope']['record_limit'] == 2
    assert view['scope']['complete'] is False
    assert view['coverage']['cache_time_source'] == 'ingestion_time'
    assert view['cache_groups'][0]['branch'] is None


def test_durable_failure_cannot_acknowledge_or_leave_a_false_duplicate(tmp_path):
    class Journal:
        fail = True

        def record_required(self, event):
            if self.fail:
                raise RuntimeError('private error')
            with (tmp_path / 'events').open('a') as stream:
                stream.write(json.dumps(event) + '\n')

    journal = Journal()
    repo = ControlPlaneRepo(now=lambda: NOW, sink=journal)
    with pytest.raises(RuntimeError):
        ingest(repo, cache())
    journal.fail = False
    receipt = ingest(repo, cache())
    assert receipt['accepted'] == 1
    assert receipt['scope']['persistence'] == 'durable_sink'
    assert receipt['scope']['memory_only'] is False
    assert len((tmp_path / 'events').read_text().splitlines()) == 1


def test_durable_replay_decodes_payloads_and_preserves_idempotency(tmp_path):
    from herdr_engineering.control_plane.bridge import load_into

    class Journal:
        def record_required(self, event):
            with (tmp_path / 'events').open('a') as stream:
                stream.write(json.dumps({**event, 'payload': json.dumps(event['payload'])}) + '\n')

        def events(self):
            return [json.loads(line) for line in (tmp_path / 'events').read_text().splitlines()]

        def missions(self):
            return []

        sessions = services = hosts = missions

    journal = Journal()
    repo = ControlPlaneRepo(now=lambda: NOW, sink=journal)
    ingest(repo, cache(), timing())
    restored = ControlPlaneRepo(now=lambda: NOW, sink=journal)
    load_into(restored, journal)
    assert ingest(restored, cache(), timing())['duplicates'] == 2
    assert restored.ci_performance()['cache_groups'][0]['hit_rate'] == 1
    assert len(journal.events()) == 2


@pytest.mark.parametrize('current_count,baseline_count', [(4, 5), (5, 4)])
def test_execution_alert_requires_five_samples_in_each_window(current_count, baseline_count):
    repo = ControlPlaneRepo(now=lambda: NOW)
    for number in range(current_count):
        ingest(repo, timing(number, wall=100))
    for number in range(baseline_count):
        ingest(repo, timing(number + 10, finished=NOW - 8 * DAY, wall=10))
    assert repo.ci_performance(days=7)['alerts'] == []


def test_hit_rate_exact_threshold_and_nine_misses_do_not_alert():
    repo = ControlPlaneRepo(now=lambda: NOW)
    ingest(repo, *(cache(n, outcome='hit' if n < 5 else 'miss') for n in range(10)))
    ingest(repo, *(cache(n + 10, kind='cargo', outcome='miss') for n in range(9)))
    assert repo.ci_performance()['alerts'] == []


def test_retained_provider_identity_is_not_conflated_across_provider_repos():
    repo = ControlPlaneRepo(now=lambda: NOW)
    for provider in (17, 18):
        repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=provider,
                                      run_number=1, branch='main', project_id=f'project-{provider}'))
    ingest(repo, cache())
    group = repo.ci_performance()['cache_groups'][0]
    assert group['provider_repo_id'] is None
    assert group['project_id'] is None
    assert group['branch'] is None
