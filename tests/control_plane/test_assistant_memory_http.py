"""The native provider alias stays authenticated and private-only."""
import json
import hashlib
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


PREFIX = '/api/v1/assistant-memory/hindsight'


class Boundary:
    def __init__(self):
        self.requests = []

    def __call__(self, method, *args):
        from herdr_engineering.control_plane.assistant_memory_gateway import GatewayError
        if method == 'authorize':
            if args[0] != 'Bearer scoped-memory-key':
                raise GatewayError(401, 'invalid bearer credential')
            return 'authenticated-actor'
        assert method == 'native'
        verb, path, body, actor = args
        assert actor == 'authenticated-actor'
        self.requests.append((verb, path, body))
        return 200, {'success': True, 'path': path}


@pytest.fixture
def memory_server():
    boundary = Boundary()
    server = ControlPlaneServer(ControlPlaneRepo(), memory_gateway=boundary,
                                api_keys={'base-operator-key': 'operator'})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        yield server, boundary
    finally:
        server.shutdown()


def request(server, path, *, method='GET', token=None, body=None, extra_headers=None):
    headers = {'Content-Type': 'application/json'}
    headers.update(extra_headers or {})
    if token:
        headers['Authorization'] = 'Bearer ' + token
    call = urllib.request.Request(f'http://127.0.0.1:{server.port}' + path,
        method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(call, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_memory_bearer_is_checked_before_native_dispatch(memory_server):
    server, boundary = memory_server
    path = PREFIX + '/v1/default/banks/project-summaries/memories'
    assert request(server, path, method='POST', body={'items': []})[0] == 401
    assert request(server, path, method='POST', token='wrong', body={'items': []})[0] == 401
    assert boundary.requests == []
    status, response = request(server, path, method='POST', token='scoped-memory-key',
                               body={'items': [{'content': 'A bounded synopsis.'}]})
    assert status == 200 and response['success']
    assert boundary.requests == [('POST', '/v1/default/banks/project-summaries/memories',
                                 {'items': [{'content': 'A bounded synopsis.'}]})]


def test_memory_key_does_not_authenticate_other_control_plane_routes(memory_server):
    server, _ = memory_server
    assert request(server, '/api/v1/hosts', token='scoped-memory-key')[0] == 401


def test_native_bearer_does_not_borrow_browser_operator_authority(memory_server):
    server, boundary = memory_server
    assert request(server, PREFIX + '/v1/default/banks', token='scoped-memory-key',
        extra_headers={'Host': 'external-agent.example',
                       'Origin': 'https://external-agent.example'})[0] == 200
    assert boundary.requests == [('GET', '/v1/default/banks', None)]


def test_native_delete_reaches_the_same_authorized_boundary(memory_server):
    server, boundary = memory_server
    path = '/v1/default/banks/project-facts/documents/note-1'
    assert request(server, PREFIX + path, method='DELETE', token='scoped-memory-key')[0] == 200
    assert boundary.requests == [('DELETE', path, None)]


def test_public_listener_does_not_expose_native_memory():
    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    boundary = Boundary()
    collab = CollabService(StaticTokenAuthenticator(
        {'test-user': hashlib.sha256(b'collab-test-token').hexdigest()}))
    server = ControlPlaneServer(ControlPlaneRepo(), memory_gateway=boundary,
                                public_port=0, public_host='127.0.0.1', collab=collab)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        call = urllib.request.Request(f'http://127.0.0.1:{server.public_port}' +
            PREFIX + '/v1/default/banks',
            headers={'Authorization': 'Bearer scoped-memory-key'})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(call, timeout=3)
        assert error.value.code == 404
        assert boundary.requests == []
    finally:
        server.shutdown()


def test_keyless_operator_memory_rejects_rebinding_cross_origin_and_simple_posts():
    class OperatorBoundary(Boundary):
        def __init__(self):
            super().__init__()
            self.grants = 0

        def __call__(self, method, *args):
            if method == 'authorize_operator':
                self.grants += 1
                return 'authenticated-actor'
            return super().__call__(method, *args)

    boundary = OperatorBoundary()
    server = ControlPlaneServer(ControlPlaneRepo(), memory_gateway=boundary)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    path = '/api/v1/assistant-memory/operator/v1/default/banks/project-facts/memories'
    try:
        assert request(server, path, extra_headers={'Host': 'attacker.example'})[0] == 404
        assert request(server, path, method='POST', body={}, extra_headers={
            'Origin': 'https://attacker.example'})[0] == 403
        assert request(server, path, method='POST', body={}, extra_headers={
            'Content-Type': 'text/plain'})[0] == 415
        assert request(server, path, method='DELETE', extra_headers={
            'Origin': 'https://attacker.example'})[0] == 403
        assert request(server, path, method='POST', body={}, extra_headers={
            'Origin': f'https://127.0.0.1:{server.port}'})[0] == 403
        assert request(server, path, extra_headers={'Host': '127.0.0.1:NOTAPORT'})[0] == 404
        assert boundary.grants == 0 and boundary.requests == []
        assert request(server, path, method='POST', body={}, extra_headers={
            'Origin': f'http://127.0.0.1:{server.port}'})[0] == 200
        assert boundary.grants == 1
    finally:
        server.shutdown()


def test_operator_tls_origin_requires_explicit_configuration():
    class OperatorBoundary(Boundary):
        def __call__(self, method, *args):
            if method == 'authorize_operator':
                return 'authenticated-actor'
            return super().__call__(method, *args)

    boundary = OperatorBoundary()
    server = ControlPlaneServer(ControlPlaneRepo(), memory_gateway=boundary,
        collab_dashboard_hosts=['dashboard.internal:443'],
        memory_dashboard_origins=['https://dashboard.internal:443'])
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    path = '/api/v1/assistant-memory/operator/v1/default/banks/project-facts/memories'
    try:
        assert request(server, path, method='POST', body={}, extra_headers={
            'Host': 'dashboard.internal:443', 'Origin': 'https://dashboard.internal:443'})[0] == 200
        assert request(server, path, method='POST', body={}, extra_headers={
            'Host': 'dashboard.internal:443', 'Origin': 'http://dashboard.internal:443'})[0] == 403
        assert len(boundary.requests) == 1
    finally:
        server.shutdown()


@pytest.mark.parametrize('origins', [['https://user:pass@dashboard.internal'],
    ['https://dashboard.internal/path'], ['http://localhost:bad'], ['null'], 'https://localhost'])
def test_invalid_operator_origin_configuration_is_rejected(origins):
    with pytest.raises(ValueError, match='memory dashboard origins'):
        ControlPlaneServer(ControlPlaneRepo(), memory_dashboard_origins=origins)
