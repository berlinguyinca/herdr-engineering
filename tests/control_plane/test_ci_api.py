import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_monitor import CIMonitor
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.fixture
def server():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={'read': 'viewer', 'write': 'operator'})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield server
    server.shutdown()


def request(server, path, body=None, key='read'):
    req = urllib.request.Request(f'http://127.0.0.1:{server.port}/api/v1/{path}',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def test_summary_uses_single_shared_monitor(server):
    server.ci_monitor.snapshot = lambda: {'available': True, 'repos': [{'repository': 'org/app'}]}
    status, data = request(server, 'ci')
    assert status == 200
    assert data['repos'][0]['repository'] == 'org/app'


def test_telemetry_auth_limits_invalid_bundles_and_performance(server):
    from tests.control_plane.test_ci_performance import cache, timing

    body = {'schema_version': 1, 'records': [cache(), timing()]}
    assert request(server, 'ci/telemetry', body, key='invalid')[0] == 401
    assert request(server, 'ci/telemetry', body)[0] == 403
    status, receipt = request(server, 'ci/telemetry', body, key='write')
    assert status == 200
    assert receipt['accepted'] == 2
    assert receipt['scope']['partial'] is True
    assert request(server, 'ci/telemetry', body, key='write')[1]['duplicates'] == 2
    for invalid in [[], {'schema_version': True, 'records': []},
                    {'schema_version': 1, 'records': [cache()] * 201},
                    {'schema_version': 1, 'records': [cache(2), {'token': 'secret'}]}]:
        assert request(server, 'ci/telemetry', invalid, key='write')[0] == 400
    # The server rejects oversized Content-Length before reading the body.
    # Sending a megabyte concurrently with that early close races into EPIPE
    # in urllib, hiding the response this assertion is intended to verify.
    from http.client import HTTPConnection
    connection = HTTPConnection('127.0.0.1', server.port, timeout=3)
    try:
        connection.request('POST', '/api/v1/ci/telemetry', headers={
            'X-API-Key': 'write', 'Content-Type': 'application/json',
            'Content-Length': str(1048577),
        })
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()
    assert request(server, 'ci/performance?days=30')[0] == 400
    assert request(server, 'ci/performance?days=garbage')[0] == 400
    assert request(server, 'ci/performance', key='invalid')[0] == 401
    status, view = request(server, 'ci/performance?days=7&repository=org%2Fapp')
    assert status == 200
    assert view['scope']['record_count'] == 2
    assert view['scope']['memory_only'] is True
    assert view['cache_groups'][0]['kind'] == 'go'


def test_performance_uses_cached_identity_without_provider_calls(server):
    from tests.control_plane.test_ci_performance import cache

    ingest = {'schema_version': 1, 'records': [cache()]}
    assert request(server, 'ci/telemetry', ingest, key='write')[0] == 200
    server.ci_monitor.snapshot = lambda **kwargs: {'repos': [{'id': 17, 'repository': 'org/app',
        'forge_url': 'https://github.com/org/app'}], 'stale': True}
    server.github_monitor.repository_languages = lambda **kwargs: {'github.com/org/app': 'Go'}
    before = server.repo.export_state()
    status, view = request(server, 'ci/performance')
    assert status == 200
    assert view['cache_groups'][0]['project_id'] == 'github.com/org/app'
    assert view['cache_groups'][0]['language'] == 'Go'
    assert view['scope']['stale'] is True
    assert server.repo.export_state() == before


def test_performance_does_not_start_provider_polling(server):
    started = []
    def forbidden():
        raise AssertionError('Request handler started provider polling')

    server.ci_monitor.start = forbidden
    server.github_monitor.start = lambda: started.append('github')
    status, view = request(server, 'ci/performance')
    assert status == 200
    assert view['scope']['partial'] is True
    assert started == []


def test_performance_resolves_inventory_before_grouping_cache_samples(server):
    from tests.control_plane.test_ci_performance import cache

    server.repo.observe_ci_pipeline({'repository': 'org/app', 'provider_repo_id': 17,
                                    'run_number': 1, 'state': 'success'})
    server.ci_monitor.snapshot = lambda **kwargs: {'repos': [
        {'id': 17, 'repository': 'org/app', 'forge_url': 'https://github.com/org/app'}]}
    records = [cache(number, pipeline=str(number), wall_seconds=85) for number in range(1, 6)]
    records.append(cache(10, repo='org/unresolved', wall_seconds=2))
    assert request(server, 'ci/telemetry', {'schema_version': 1, 'records': records}, key='write')[0] == 200
    before = server.repo.export_state()
    status, view = request(server, 'ci/performance')
    assert status == 200
    resolved = [group for group in view['cache_groups'] if group['repository'] == 'org/app']
    assert len(resolved) == 1
    assert resolved[0]['sample_count'] == 5
    assert resolved[0]['restore']['p95'] == 85
    assert resolved[0]['project_id'] == 'github.com/org/app'
    assert resolved[0]['provider_repo_id'] == '17'
    assert resolved[0]['branch'] is None
    assert len(view['alerts']) == 1
    assert view['alerts'][0]['rule'] == 'cache_budget'
    assert view['alerts'][0]['current_value'] == 85
    unresolved = [group for group in view['cache_groups'] if group['repository'] == 'org/unresolved'][0]
    assert unresolved['project_id'] is None
    assert unresolved['provider_repo_id'] is None
    assert server.repo.export_state() == before


def test_telemetry_persistence_error_is_sanitized_and_not_acknowledged(server):
    from tests.control_plane.test_ci_performance import cache

    class UnavailableSink:
        def record_required(self, event):
            raise OSError('database-password-private')

    server.repo._sink = UnavailableSink()
    status, result = request(server, 'ci/telemetry',
        {'schema_version': 1, 'records': [cache()]}, key='write')
    assert status == 503
    assert 'database-password-private' not in json.dumps(result)
    assert server.repo.ci_performance()['scope']['record_count'] == 0


def test_pipeline_and_log_require_valid_query(server):
    assert request(server, 'ci/pipeline')[0] == 400
    assert request(server, 'ci/log?repository=org%2Fapp&number=-1&step=2')[0] == 400
    server.ci_monitor.detail = lambda repo, number: {'pipeline': {'repository': repo, 'run_number': int(number)}}
    assert request(server, 'ci/pipeline?repository=org%2Fapp&number=2')[1]['pipeline']['repository'] == 'org/app'


def test_viewer_cannot_execute_and_operator_needs_deployment_opt_in(server):
    body = {'repository': 'org/app', 'number': 2, 'action': 'cancel'}
    assert request(server, 'ci/actions', body)[0] == 403
    assert request(server, 'ci/actions', body, key='write')[0] == 403


def test_log_redacts_exact_token_and_bounds_text():
    class Provider:
        token = 'very-private-value'

        def step_log(self, *args):
            return self.token + 'x' * 300000

    view = CIMonitor(provider_factory=Provider).log('org/app', '1', '2')
    assert view['available'] and view['truncated']
    assert 'very-private-value' not in view['log']
    assert len(view['log']) <= 262144


def test_cross_origin_mutation_is_refused_even_with_operator_key(server):
    req = urllib.request.Request(f'http://127.0.0.1:{server.port}/api/v1/ci/actions',
                                 data=b'{}', headers={'X-API-Key': 'write',
                                 'Content-Type': 'application/json', 'Origin': 'https://untrusted.example'})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 403


def test_debug_route_requires_operator_and_reports_delivery(server, monkeypatch):
    from herdr_engineering.control_plane import ci_debug
    calls = []

    def dispatch(*args):
        calls.append(args)
        return {'ok': True, 'status': 'delivered'}

    monkeypatch.setattr(ci_debug, 'debug_ci', dispatch)
    body = {'repository': '17', 'number': 2, 'session_id': 'pi:session', 'operation_id': 'op'}
    assert request(server, 'ci/debug', body)[0] == 403
    assert not calls
    assert request(server, 'ci/debug', body, key='write') == (200, {'ok': True, 'status': 'delivered'})
    assert calls[0][3:] == ('17', 2, 'pi:session', 'op')


def test_debug_route_passes_the_fix_mode_only_when_sent_and_needs_operator_access(server, monkeypatch):
    from herdr_engineering.control_plane import ci_debug
    calls = []

    def dispatch(*args, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'status': 'delivered', 'handoff_type': 'fix-with-pi'}

    monkeypatch.setattr(ci_debug, 'debug_ci', dispatch)
    body = {'repository': '17', 'number': 2, 'session_id': 'pi:session', 'operation_id': 'op', 'mode': 'fix'}
    assert request(server, 'ci/debug', body)[0] == 403
    assert request(server, 'ci/debug', body, key='write')[0] == 200
    assert calls == [{'mode': 'fix'}]
    del body['mode']
    request(server, 'ci/debug', body, key='write')
    assert calls[1] == {}


def test_provider_bounded_preview_keeps_truncation_flag():
    class Provider:
        token = None

        def step_log(self, *args):
            return 'bounded output\n[log truncated; open CI for the full log]'

    assert CIMonitor(provider_factory=Provider).log('org/app', 1, '2')['truncated'] is True


def test_report_upload_requires_operator_and_is_readable_by_viewer(server):
    body = {'repository': 'org/app', 'provider_repo_id': 17, 'run_number': 3,
            'branch': 'main', 'revision': 'abc',
            'xml': '<testsuite><testcase name="test_add"/></testsuite>'}
    assert request(server, 'ci/reports', body)[0] == 403
    assert request(server, 'ci/reports', body, key='write')[0] == 200
    status, data = request(server, 'ci/reports')
    assert status == 200
    assert data['reports'][0]['summary']['passed'] == 1
    assert data['scope']['complete'] is False
    assert request(server, 'ci/reports', {**body, 'xml': '<broken'}, key='write')[0] == 400
    assert request(server, 'ci/reports', ['bad'], key='write')[0] == 400


def test_history_is_viewer_readable_flat_and_memory_only(server):
    from tests.control_plane.test_ci_history import MemoryProvider, runs

    server.ci_monitor.close()
    server.ci_monitor = CIMonitor(provider_factory=lambda: MemoryProvider(runs(3)))
    server.ci_monitor._history.refresh()
    status, data = request(server, 'ci/history')
    assert status == 200
    assert isinstance(data['pipelines'], list)
    assert data['scope']['memory_only'] is True
    assert data['repos'][0]['provider_url'] == 'https://ci.example'
    assert data['coverage']['17']['exhausted'] is True
    assert request(server, 'ci/history', key='invalid')[0] == 401


@pytest.mark.parametrize('path', ['ci', 'ci/history'])
def test_ci_routes_use_cached_language_metadata_and_preserve_history_scope(server, path):
    from tests.control_plane.test_project_links import snapshot

    view = snapshot()
    view['scope'] = {'memory_only': True}
    if path.endswith('history'):
        view['pipelines'] = [row for rows in view['pipelines'].values() for row in rows]
    server.ci_monitor.snapshot = lambda: view
    server.ci_monitor.history = lambda: view
    server.github_monitor.repository_languages = lambda: {'github.com/org/frontend': 'Rust'}
    status, data = request(server, path)
    assert status == 200
    row = data['pipelines'][0] if path.endswith('history') else data['pipelines']['org/frontend'][0]
    assert row['language'] == 'Rust'
    assert row['language_source'] == 'github_primary_language'
    assert data['scope'] == view['scope']


@pytest.mark.parametrize('path', ['ci', 'ci/history'])
def test_ci_remains_available_when_language_monitor_is_unavailable(server, path):
    from types import SimpleNamespace

    from tests.control_plane.test_project_links import snapshot

    server.github_monitor.close()
    view = snapshot()
    if path.endswith('history'):
        view['pipelines'] = [row for rows in view['pipelines'].values() for row in rows]
    server.ci_monitor.snapshot = lambda: view
    server.ci_monitor.history = lambda: view
    server.github_monitor = SimpleNamespace(close=lambda: None)
    assert request(server, path)[1]['repos'][0]['language'] is None

    def unavailable():
        raise OSError('private-token-do-not-expose')

    server.github_monitor.repository_languages = unavailable
    status, data = request(server, path)
    assert status == 200
    assert data['repos'][0]['language'] is None
    assert 'private-token-do-not-expose' not in json.dumps(data)


def test_retained_report_runs_receive_current_repository_language_without_recent_runs(server):
    from tests.control_plane.test_project_links import snapshot

    view = snapshot()
    view['pipelines'] = {}
    server.ci_monitor.snapshot = lambda: view
    server.github_monitor.repository_languages = lambda: {'github.com/org/frontend': 'Rust'}
    server.repo.observe_ci_pipeline({'repository': 'org/frontend', 'provider_repo_id': 17,
                                    'run_number': 100, 'state': 'success'})
    before = server.repo.ci_reports()
    status, data = request(server, 'ci/reports')
    assert status == 200
    assert data['pipelines'][0]['language'] == 'Rust'
    assert data['pipelines'][0]['language_source'] == 'github_primary_language'
    assert data['pipelines'][0]['project_id'] == 'github.com/org/frontend'
    assert data['reports'] == before['reports']
    assert data['scope'] == before['scope']
    assert server.repo.ci_reports() == before


@pytest.mark.parametrize('inventory', [[], [{'id': 99, 'repository': 'org/other',
                                            'forge_url': 'https://github.com/org/other'}]])
@pytest.mark.parametrize('metadata', [{}, {'repository_id': 'github.com/org/app',
                                          'forge_host': 'github.com', 'project_id': 'shared-project'}])
def test_retained_report_identity_survives_missing_current_repository(server, inventory, metadata):
    stored = {'provider_repo_id': '17', 'repository': 'org/app', 'run_number': 1,
              'provider_url': 'https://ci.example', **metadata}
    server.repo.observe_ci_pipeline(stored)
    server.ci_monitor.snapshot = lambda: {'repos': inventory, 'pipelines': {}}
    server.github_monitor.repository_languages = lambda: {}
    status, data = request(server, 'ci/reports')
    assert status == 200
    row = data['pipelines'][0]
    assert {key: row[key] for key in stored} == stored
    assert row['language'] is None
    assert row['language_source'] == 'unknown'
    assert server.repo.ci_reports()['pipelines'][0] == stored


def test_ci_compatibility_response_omits_private_measurements(server):
    from tests.control_plane.test_ci_monitor import Provider

    server.ci_monitor.close()
    monitor = CIMonitor(provider_factory=Provider)
    server.ci_monitor = monitor
    monitor.refresh()
    # Freeze background work so the HTTP contract is deterministic.
    monitor.close()
    status, view = request(server, 'ci')
    assert status == 200
    assert {'available', 'status', 'refreshing', 'stale', 'repos', 'pipelines',
            'repo_errors', 'agents', 'agents_error', 'error', 'checked_at',
            'last_success_at', 'progress'} <= view.keys()
    assert view['pipelines']['org/app'][0]['run_number'] == 2
    assert view['pipelines']['org/app'][0]['state'] == 'running'
    assert 'provider_calls' not in json.dumps(view)
    measured = monitor._measurement_snapshot()
    assert measured['provider_calls'] == 3  # repos, pipelines, attempted agents
    assert measured['summary_rows'] == 1
    assert measured['cache_hits'] == 1
    assert measured['refresh_duration_seconds'] >= 0
    assert measured['refresh_finished_at'] is not None


# Task 4: exercise the real viewer routes against retained, deterministic evidence.
CI_NOW = 1792929600  # 2026-10-25T12:00:00Z


def ci_run(number=1, **changes):
    from datetime import UTC, datetime

    stamp = datetime.fromtimestamp(CI_NOW - 60, UTC).isoformat()
    return dict(provider_url='https://ci.example', provider_repo_id=17,
                repository='org/app', run_number=number, revision='abc', branch='main',
                state='failure', event='push', created_at=stamp, started_at=stamp,
                finished_at=datetime.fromtimestamp(CI_NOW, UTC).isoformat(), **changes)


def seed_ci(server, rows, *, stale=False):
    from types import SimpleNamespace

    server.repo._now = lambda: CI_NOW
    server.ci_monitor.close()  # Freeze publication while exercising cached reads.
    history = server.ci_monitor._history
    projects = {(r['provider_url'], str(r['provider_repo_id']), r['repository']): r for r in rows}
    repos = [dict(id=r['provider_repo_id'], repository=r['repository'],
                  provider_url=r['provider_url'], default_branch='main') for r in projects.values()]
    with history._lock:
        history._view.update(available=True, status='connected', stale=stale,
                             repos=repos, pipelines=rows)
        history._summary_projects = [dict(provider_url=r['provider_url'],
            provider_repo_id=r['id'], repository=r['repository'], pipeline_count=0, outcomes={})
            for r in repos]
    def forbidden(*args, **kwargs):
        raise AssertionError('Compact reads must not scan, start polling, or load detail/logs')
    # Credential context is local configuration; this stub offers no read API.
    server.ci_monitor._factory = lambda: SimpleNamespace(token=None)
    server.ci_monitor.start = forbidden
    history.snapshot = forbidden
    server.ci_monitor.snapshot = forbidden
    return history


def upload_ci_report(server, *, number=1, xml=None, **changes):
    body = dict(repository='org/app', provider_url='https://ci.example', provider_repo_id=17,
                run_number=number, revision='abc', branch='main',
                xml=xml or '<testsuite><testcase name="broken" file="test.py" time="1"><failure message="token=secret-value">trace</failure></testcase></testsuite>')
    body.update(changes)
    assert request(server, 'ci/reports', body, key='write')[0] == 200


def test_compact_summary_filters_metrics_scope_and_exact_identity(server):
    rows = [ci_run(), {**ci_run(2), 'state': 'success'},
            {**ci_run(3), 'state': 'canceled'}, {**ci_run(4), 'state': 'running'},
            {**ci_run(5), 'branch': 'topic', 'event': 'pull_request'},
            {**ci_run(6), 'created_at': None, 'started_at': None, 'finished_at': None},
            {**ci_run(7), 'created_at': '2026-09-01T00:00:00Z',
             'finished_at': '2026-09-01T00:00:00Z'},
            {**ci_run(), 'provider_url': 'https://other.example'},
            {**ci_run(), 'provider_repo_id': 18},
            {**ci_run(), 'repository': 'org/renamed'}]
    seed_ci(server, rows, stale=True)
    status, view = request(server, 'ci/summary?days=7&repository=org%2Fapp&branch=main&scope=all')
    assert status == 200
    assert 'projects' in view, view
    assert len(view['projects']) == 3
    assert len({p['project_id'] for p in view['projects']}) == 3
    assert view['kpis']['pipeline_count'] == 6
    assert view['kpis']['completed_count'] == 4
    assert view['kpis']['pass_rate'] == .25
    assert view['kpis']['failure_count'] == 3
    assert view['kpis']['active_count'] == 1
    assert view['kpis']['duration_seconds']['sample_count'] == 4
    assert view['kpis']['duration_seconds']['p95'] is None
    assert view['scope']['unknown_time_run_count'] == 1
    assert view['scope']['partial'] and view['scope']['stale']
    assert view['scope']['complete'] is False
    assert view['scope']['denominator'] == 'successful_or_failed_runs'
    assert view['charts'][0]['completed_count'] == 4
    assert 'pipelines' not in view and 'reports' not in view
    for scope, count in [('pr', 1), ('default', 4)]:
        result = request(server, f'ci/summary?days=7&scope={scope}&provider_url=https%3A%2F%2Fci.example&provider_repo_id=17&repository=org%2Fapp')[1]
        assert result['kpis']['pipeline_count'] == count
    assert request(server, 'ci/summary', key='invalid')[0] == 401


@pytest.mark.parametrize('route', ['summary', 'tests'])
@pytest.mark.parametrize('query', ['days=0', 'days=8', 'days=garbage', 'days=7&days=14',
    'branch=%00', 'repository=' + 'x' * 501, 'repository=token%3Dsecret-value',
    'provider_url=https%3A%2F%2Fuser%3Asecret%40ci.example', 'token=secret', 'unknown=1'])
def test_compact_routes_reject_invalid_filters(server, route, query):
    assert request(server, f'ci/{route}?{query}')[0] == 400


def test_compact_routes_validate_scope_and_allow_supported_windows(server):
    seed_ci(server, [])
    assert request(server, 'ci/summary?scope=unrecognized')[0] == 400
    assert request(server, 'ci/tests?scope=unrecognized')[0] == 400
    assert request(server, 'ci/tests?scope=all')[0] == 200
    for route in ('summary', 'tests'):
        for days in (7, 14, 30):
            status, view = request(server, f'ci/{route}?days={days}')
            assert status == 200 and view['scope']['days'] == days
            assert view['scope']['partial'] is True


def test_test_history_route_is_exact_bounded_and_retains_unknown_evidence(server):
    seed_ci(server, [ci_run()], stale=True)
    upload_ci_report(server)
    upload_ci_report(server, provider_url='https://other.example')
    query = 'ci/tests?days=7&repository=org%2Fapp&branch=main&test=broken&provider_url=https%3A%2F%2Fci.example&provider_repo_id=17'
    status, view = request(server, query)
    assert status == 200
    assert len(view['tests']) == 1
    assert view['tests'][0]['failure_count'] == 1
    assert view['tests'][0]['provider_url'] == 'https://ci.example'
    assert view['scope']['stale'] is True and view['scope']['partial'] is True
    assert 'secret-value' not in json.dumps(view)
    assert request(server, 'ci/tests', key='invalid')[0] == 401
    assert request(server, 'ci/tests?test=missing')[1]['tests'] == []


def test_summary_large_cached_history_is_compact_fast_and_does_not_copy_details(server):
    import time

    class NoCopy:
        def __deepcopy__(self, memo):
            raise AssertionError('Full details were copied')

    rows = [{**ci_run(index + 1), 'provider_repo_id': index % 120,
             'repository': f'org/app-{index % 120}', 'tasks': NoCopy(),
             'log': 'PRIVATE RAW LOG', 'token': 'NEVER RETURN'} for index in range(20000)]
    seed_ci(server, rows)
    elapsed = []
    for _ in range(3):
        started = time.monotonic()
        status, view = request(server, 'ci/summary?days=30&scope=all')
        elapsed.append(time.monotonic() - started)
        assert status == 200
        assert len(view['projects']) == 120
        assert view['kpis']['pipeline_count'] == 20000
        assert view['kpis']['duration_seconds']['p95'] == 60
        payload = json.dumps(view)
        assert len(payload.encode()) < 256 * 1024
        assert 'PRIVATE RAW LOG' not in payload and 'NEVER RETURN' not in payload
    assert max(elapsed) < 2, elapsed
    print(f'compact summary: 120 projects, 20000 runs, bytes={len(payload.encode())}, seconds={elapsed}')


def diagnosis_provider(server, row):
    calls = []
    class Provider:
        available = True
        base_url = 'https://ci.example'
        token = 'provider-private-token'
        def pipeline(self, repository, number):
            calls.append(('pipeline', repository, number))
            return row
        def step_log(self, repository, number, step):
            calls.append(('log', repository, number, step))
            return 'lazy log'
    server.ci_monitor._factory = Provider
    return calls


def test_pipeline_diagnosis_joins_exact_report_without_loading_logs(server):
    seed_ci(server, [ci_run()])
    server.repo.observe_ci_pipeline(ci_run())
    upload_ci_report(server)
    calls = diagnosis_provider(server, dict(repository='17', number=1, commit='abc', branch='main',
        status='failure', tasks=[dict(id=4, name='unit', state='failure', workflow='tests',
        error='token=secret-value')], workflows=[dict(id=2, name='tests', status='failure')]))
    status, view = request(server, 'ci/pipeline?repository=17&number=1&provider_url=https%3A%2F%2Fci.example')
    assert status == 200
    diagnosis = view['diagnosis']
    assert diagnosis['evidence_availability'] == 'reported'
    assert diagnosis['failed_steps'][0]['id'] == 4
    assert diagnosis['failed_steps'][0]['dependency_evidence'] == 'unavailable'
    assert diagnosis['failed_tests'][0]['name'] == 'broken'
    assert diagnosis['failed_tests'][0]['history']['failure_count'] == 1
    assert diagnosis['failed_tests'][0]['history']['first_failure'] is not None
    assert diagnosis['step_test_association'] == 'unavailable'
    assert diagnosis['log']['availability'] == 'not_loaded'
    assert 'secret-value' not in json.dumps(view)
    assert calls == [('pipeline', '17', 1)]
    assert request(server, 'ci/log?repository=17&number=1&step=4')[1]['log'] == 'lazy log'
    assert calls[-1] == ('log', '17', 1, '4')


@pytest.mark.parametrize('changes', [dict(provider_url='https://other.example'),
    dict(provider_repo_id=18), dict(repository='org/other'), dict(branch='topic'),
    dict(revision='other')])
def test_diagnosis_does_not_join_colliding_reports(server, changes):
    seed_ci(server, [ci_run()])
    server.repo.observe_ci_pipeline(ci_run())
    upload_ci_report(server, **changes)
    diagnosis_provider(server, ci_run())
    status, view = request(server, 'ci/pipeline?repository=17&number=1')
    assert status == 200
    assert view['diagnosis']['evidence_availability'] == 'unavailable'
    assert view['diagnosis']['failed_tests'] == []
    assert view['diagnosis']['reason']


def test_diagnosis_and_tests_enforce_payload_limits(server):
    seed_ci(server, [ci_run()])
    server.repo.observe_ci_pipeline(ci_run())
    cases = ''.join(f'<testcase name="case-{i}"><failure message="failure">{"x" * 2000}</failure></testcase>' for i in range(300))
    upload_ci_report(server, xml=f'<testsuite>{cases}</testsuite>')
    diagnosis_provider(server, {**ci_run(), 'log': 'RAW LOG MUST NOT LEAK',
                               'tasks': [dict(id=i, state='failure', log='RAW LOG MUST NOT LEAK') for i in range(300)]})
    status, view = request(server, 'ci/pipeline?repository=17&number=1')
    assert status == 200
    assert view['diagnosis']['truncated'] is True
    assert len(view['diagnosis']['failed_tests']) <= 25
    assert len(view['diagnosis']['failed_steps']) <= 50
    assert len(json.dumps(view['diagnosis']).encode()) < 128 * 1024
    assert 'RAW LOG MUST NOT LEAK' not in json.dumps(view)
    status, tests = request(server, 'ci/tests')
    assert status == 200
    assert tests['scope']['truncated'] and tests['scope']['test_count'] == 300
    assert len(tests['tests']) <= 200
    assert len(json.dumps(tests).encode()) < 256 * 1024


def test_summary_retains_empty_inventory_and_stable_revisions(server):
    history = seed_ci(server, [ci_run()])
    with history._lock:
        history._view['pipelines'] = []
    status, empty = request(server, 'ci/summary?scope=default')
    assert status == 200
    assert empty['kpis']['project_count'] == 1
    assert empty['projects'][0]['completed_count'] == 0
    assert empty['projects'][0]['pass_rate'] is None
    assert empty['projects'][0]['coverage']['exhausted'] is False
    assert empty['revision'] == request(server, 'ci/summary?scope=default')[1]['revision']
    server.repo.observe_ci_pipeline(ci_run())
    observed = request(server, 'ci/summary?scope=default')[1]
    assert observed['kpis']['failure_count'] == 1
    assert observed['revision'] != empty['revision']
    with history._lock:
        history._view['pipelines'] = [{**ci_run(), 'state': 'success'}]
    latest = request(server, 'ci/summary?scope=default')[1]
    assert latest['kpis']['completed_count'] == 1 and latest['kpis']['success_count'] == 1


def test_diagnosis_enforces_byte_bound_and_redacts_exact_provider_token(server):
    seed_ci(server, [ci_run()])
    tasks = [dict(id=i, state='failure', name='n' * 500, workflow='w' * 500,
                  error='provider-private-token', depends_on=['d' * 200] * 50) for i in range(50)]
    diagnosis_provider(server, {**ci_run(), 'message': 'provider-private-token', 'tasks': tasks})
    status, view = request(server, 'ci/pipeline?repository=17&number=1')
    assert status == 200
    assert json.dumps(view).find('provider-private-token') == -1
    assert len(json.dumps(view['diagnosis']).encode()) < 128 * 1024
    assert view['diagnosis']['truncated'] is True


def test_diagnosis_resolves_numeric_detail_identity_from_cached_inventory(server):
    seed_ci(server, [ci_run()])
    upload_ci_report(server)
    diagnosis_provider(server, dict(repository='17', number=1, commit='abc', branch='main', status='failure'))
    status, view = request(server, 'ci/pipeline?repository=17&number=1')
    assert status == 200
    assert view['diagnosis']['evidence_availability'] == 'reported'
    assert view['diagnosis']['failed_tests'][0]['name'] == 'broken'


def test_cold_summary_bootstraps_background_history_without_waiting_for_provider(server):
    import time

    entered, release = threading.Event(), threading.Event()
    request_threads = []
    class Provider:
        available = True
        base_url = 'https://ci.example'
        def repos(self, **kwargs):
            request_threads.append(threading.current_thread().name)
            entered.set()
            release.wait(3)
            return []
    server.ci_monitor.close()
    server.ci_monitor = CIMonitor(provider_factory=Provider)
    try:
        started = time.monotonic()
        status, view = request(server, 'ci/summary')
        assert status == 200 and time.monotonic() - started < 1
        assert view['scope']['partial'] is True
        assert entered.wait(1), 'Summary must bootstrap the existing background collector'
        assert all('process_request' not in name for name in request_threads)
    finally:
        release.set()


@pytest.mark.parametrize('route', ['tests', 'pipeline'])
@pytest.mark.parametrize('field', ['message', 'trace', 'labels'])
def test_report_output_redacts_opaque_provider_credential_before_limits(server, route, field):
    from herdr_engineering.ci import WoodpeckerProvider
    from tests.test_ci import FakeCI

    seed_ci(server, [ci_run()])
    server.repo.observe_ci_pipeline(ci_run())
    credential = 'Z9q7V2m4R8t6N3p5L1w0'
    # Cross diagnosis's 500/1000-character boundaries to catch prefix leaks.
    message = 'm' * 490 + credential if field == 'message' else 'failure'
    trace = 't' * 990 + credential if field == 'trace' else 'trace'
    label = credential if field == 'labels' else 'case'
    xml = (f'<testsuite name="{label}"><testcase name="{label}" classname="{label}" file="{label}">'
           f'<failure message="{message}">{trace}</failure></testcase></testsuite>')
    upload_ci_report(server, xml=xml)
    transport = FakeCI({'/api/repos/17/pipelines/1': {
        'number': 1, 'status': 'failure', 'branch': 'main', 'commit': 'abc'},
        '/api/repos/17/permissions': {'push': False}})
    provider = WoodpeckerProvider('https://ci.example', credential, client=transport)
    server.ci_monitor._factory = lambda: provider
    path = 'ci/tests' if route == 'tests' else 'ci/pipeline?repository=17&number=1'
    status, view = request(server, path)
    assert status == 200
    payload = json.dumps(view)
    assert payload.find(credential[:8]) == -1
    assert '[REDACTED]' in payload
    if route == 'tests':
        assert len(view['tests']) == 1
        assert transport.requests == [], 'Credential access must not issue provider reads'
        assert len(payload.encode()) < 256 * 1024
    else:
        assert view['diagnosis']['evidence_availability'] == 'reported'
        assert len(view['diagnosis']['failed_tests']) == 1
        assert len(json.dumps(view['diagnosis']).encode()) < 128 * 1024
        assert all('/pipelines/1' in url or '/permissions' in url for url in transport.requests)


def test_named_detail_uses_requested_provider_id_not_stale_recreated_repo_inventory(server):
    from herdr_engineering.ci import WoodpeckerProvider
    from tests.test_ci import FakeCI

    seed_ci(server, [ci_run()])  # Old org/app was ID 17.
    server.repo.observe_ci_pipeline(ci_run())
    upload_ci_report(server)
    transport = FakeCI({'/api/user/repos': [{'id': 18, 'full_name': 'org/app'}],
        '/api/repos/18/pipelines/1': {'number': 1, 'commit': 'abc', 'branch': 'main', 'status': 'failure'},
        '/api/repos/18/permissions': {'push': False}})
    provider = WoodpeckerProvider('https://ci.example', 'opaque-value', client=transport)
    server.ci_monitor._factory = lambda: provider
    status, view = request(server, 'ci/pipeline?repository=org%2Fapp&number=1')
    assert status == 200
    assert 'https://ci.example/api/repos/18/pipelines/1' in transport.requests
    assert str(view['pipeline']['provider_repo_id']) == '18'
    assert view['diagnosis']['evidence_availability'] == 'unavailable'
    assert view['diagnosis']['failed_tests'] == []
    upload_ci_report(server, provider_repo_id=18)
    status, view = request(server, 'ci/pipeline?repository=org%2Fapp&number=1')
    assert status == 200
    assert view['diagnosis']['evidence_availability'] == 'reported'
    assert view['diagnosis']['history_scope']['provider_repo_id'] == '18'


@pytest.mark.parametrize('bound', ['', '&provider_url=https%3A%2F%2Fci.example'])
def test_named_detail_without_provider_id_cannot_use_cached_name_as_proof(server, bound):
    seed_ci(server, [ci_run()])
    server.repo.observe_ci_pipeline(ci_run())
    upload_ci_report(server)
    diagnosis_provider(server, dict(repository='org/app', number=1, commit='abc', branch='main', status='failure'))
    status, view = request(server, 'ci/pipeline?repository=org%2Fapp&number=1' + bound)
    assert status == 200
    assert view['diagnosis']['evidence_availability'] == 'unavailable'
    assert view['diagnosis']['failed_tests'] == []
    assert view['diagnosis']['reason']


@pytest.mark.parametrize('legacy', [False, True], ids=['new-upload', 'retained'])
@pytest.mark.parametrize('prefix_length', [1, 7])
@pytest.mark.parametrize('field,limit', [('name', 500), ('suite', 500), ('classname', 500),
                                       ('file', 500), ('message', 2000), ('trace', 4000)])
def test_parser_boundary_credentials_are_not_exposed(server, legacy, prefix_length, field, limit):
    from herdr_engineering.ci import WoodpeckerProvider
    from herdr_engineering.control_plane.ci_reports import parse_junit
    from tests.test_ci import FakeCI

    seed_ci(server, [ci_run()])
    credential = 'Ω9q7V2m4R8t6N3p5L1w0'
    values = dict(name='case', suite='suite', classname='class', file='test.py', message='failure', trace='trace')
    values[field] = 'x' * (limit - prefix_length) + credential + ' trailing text'
    xml = (f'<testsuite name="{values["suite"]}"><testcase name="{values["name"]}" '
           f'classname="{values["classname"]}" file="{values["file"]}">'
           f'<failure message="{values["message"]}">{values["trace"]}</failure></testcase></testsuite>')
    original_id = parse_junit(xml)['tests'][0]['id']
    body = dict(repository='org/app', provider_url='https://ci.example', provider_repo_id=17,
                run_number=1, revision='abc', branch='main', xml=xml)
    transport = FakeCI({'/api/repos/17/pipelines/1': {
        'number': 1, 'status': 'failure', 'branch': 'main', 'commit': 'abc'},
        '/api/repos/17/permissions': {'push': False}})
    provider = WoodpeckerProvider('https://ci.example', credential, client=transport)
    server.ci_monitor._factory = lambda: provider
    if legacy:
        # Exercise existing journal/backup evidence created without credential context.
        server.repo.ingest_ci_report(body)
        restored = ControlPlaneRepo(now=lambda: CI_NOW)
        restored.import_state(server.repo.export_state())
        server.repo = restored
        assert server.repo.ci_reports()['reports'][0]['tests'][0][field].endswith(credential[:prefix_length])
    else:
        status, receipt = request(server, 'ci/reports', body, key='write')
        assert status == 200
        assert receipt['tests'][0]['id'] == original_id
        assert receipt['tests'][0][field].find('Ω') == -1
        assert len(receipt['tests'][0][field]) <= limit
        assert json.dumps(server.repo.export_state(), ensure_ascii=False).find('Ω') == -1
    before = server.repo.export_state()
    for route in ('ci/tests', 'ci/pipeline?repository=17&number=1'):
        status, view = request(server, route)
        assert status == 200
        assert json.dumps(view, ensure_ascii=False).find('Ω') == -1
        if route == 'ci/tests':
            assert transport.requests == []
            cases = view['tests']
            assert cases[0]['failure_count'] == 1
        else:
            assert view['diagnosis']['evidence_availability'] == 'reported'
            cases = view['diagnosis']['failed_tests']
        assert cases[0]['id'] == original_id
        assert len(json.dumps(view).encode()) < 256 * 1024
    assert server.repo.export_state() == before, 'Read-time protection must not rewrite retained evidence'


@pytest.mark.parametrize('url', ['https://opaque-provider-credential.example',
                                 'https://ci.example/opaque-provider-credential'])
def test_report_upload_rejects_exact_provider_url_credentials(server, url):
    from herdr_engineering.ci import WoodpeckerProvider
    from tests.test_ci import FakeCI

    transport = FakeCI({})
    provider = WoodpeckerProvider('https://ci.example', 'opaque-provider-credential', client=transport)
    server.ci_monitor._factory = lambda: provider
    body = dict(repository='org/app', provider_url=url, provider_repo_id=17,
                run_number=1, revision='abc', branch='main', xml='<testsuite/>')
    status, view = request(server, 'ci/reports', body, key='write')
    assert status == 400
    assert 'opaque-provider-credential' not in json.dumps(view)
    assert server.repo.ci_reports()['reports'] == []
    assert transport.requests == []


def test_compatibility_reports_redact_retained_provider_urls_without_mutation(server):
    from herdr_engineering.ci import WoodpeckerProvider
    from tests.test_ci import FakeCI

    credential = 'opaque-provider-credential'
    provider_url = f'https://ci.example/{credential}'
    body = dict(repository='org/app', provider_url=provider_url, provider_repo_id=17,
                run_number=1, revision='abc', branch='main',
                xml=f'<testsuite><testcase name="{credential}"/></testsuite>')
    receipt = server.repo.ingest_ci_report(body)
    server.repo.observe_ci_pipeline({key: value for key, value in body.items() if key != 'xml'})
    restored = ControlPlaneRepo()
    restored.import_state(server.repo.export_state())
    server.repo = restored
    before = server.repo.export_state()
    transport = FakeCI({})
    provider = WoodpeckerProvider('https://ci.example', credential, client=transport)
    server.ci_monitor._factory = lambda: provider
    server.ci_monitor.snapshot = lambda **kwargs: {'repos': []}
    status, view = request(server, 'ci/reports')
    assert status == 200
    assert credential not in json.dumps(view)
    assert view['reports'][0]['report_id'] == receipt['report_id']
    assert view['reports'][0]['tests'][0]['id'] == receipt['tests'][0]['id']
    assert view['reports'][0]['summary']['passed'] == 1
    assert server.repo.export_state() == before
    assert transport.requests == []
