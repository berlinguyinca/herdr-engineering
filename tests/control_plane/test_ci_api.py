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
                    {'schema_version': 1, 'records': [cache(2), {'token': 'secret'}]},
                    {'schema_version': 1, 'records': [], 'padding': 'x' * 1048576}]:
        assert request(server, 'ci/telemetry', invalid, key='write')[0] == 400
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
