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


def test_provider_bounded_preview_keeps_truncation_flag():
    class Provider:
        token = None

        def step_log(self, *args):
            return 'bounded output\n[log truncated; open CI for the full log]'

    assert CIMonitor(provider_factory=Provider).log('org/app', 1, '2')['truncated'] is True
