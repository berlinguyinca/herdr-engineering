"""Exact provider binding survives reconfiguration and repository/run ID collisions."""
import base64
import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, build_opener

import pytest

from herdr_engineering.ci import WoodpeckerProvider
from herdr_engineering.control_plane.ci_monitor import CIMonitor
from tests.control_plane.test_ci_api import request, server


@pytest.fixture
def providers(tmp_path):
    requests = []
    state = {'provider': 'a', 'status': 'failure'}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            prefix, _, path = self.path[1:].partition('/')
            if path.startswith('api/repos/17/logs/'):
                body = [{'data': base64.b64encode(f'log from {prefix}'.encode()).decode()}]
            elif path == 'api/repos/17/permissions':
                body = {'push': True, 'pull': True}
            elif path == 'api/user/repos':
                body = [{'id': 17, 'full_name': 'org/app'}]
            elif path == 'api/repos/17/pipelines':
                body = [{'number': 12, 'status': state['status']}]
            else:
                body = {'number': 12, 'status': state['status'], 'commit': prefix * 40}
            value = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(value)))
            self.end_headers()
            self.wfile.write(value)

        def do_POST(self):
            self.do_GET()

        def log_message(self, *args):
            pass

    certificate, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(certificate)], check=True, capture_output=True)
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    upstream.socket = tls.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    origin = f'https://127.0.0.1:{upstream.server_port}'
    client = build_opener(HTTPSHandler(context=ssl.create_default_context(cafile=str(certificate))))
    def factory():
        return WoodpeckerProvider(f'{origin}/{state["provider"]}', 'local-test-token', client=client)
    try:
        yield factory, state, requests, origin
    finally:
        upstream.shutdown()
        upstream.server_close()


@pytest.mark.parametrize('operation', ['pipeline', 'log', 'retry', 'cancel'])
def test_expected_provider_rejects_reconfigured_colliding_run_before_upstream_io(server, providers, operation):
    factory, state, requests, origin = providers
    server.ci_monitor.close()
    server.ci_monitor = CIMonitor(provider_factory=factory, actions_enabled=True)
    query = {'repository': '17', 'number': 12, 'provider_url': f'{origin}/a'}
    if operation == 'log':
        query['step'] = '4'
    state['provider'] = 'b'
    if operation == 'cancel':
        state['status'] = 'running'
    if operation in ('retry', 'cancel'):
        result = request(server, 'ci/actions', {**query, 'action': operation}, key='write')
    else:
        result = request(server, f'ci/{operation}?{urlencode(query)}')
    assert result[0] in (400, 409), result
    assert requests == [], 'Mismatched retained identity must never reach the newly configured provider'
    query['provider_url'] = f'{origin}/b/'
    if operation in ('retry', 'cancel'):
        status, body = request(server, 'ci/actions', {**query, 'action': operation}, key='write')
        assert status == 200, body
        assert any(method == 'POST' and path.startswith('/b/') for method, path in requests)
    else:
        status, body = request(server, f'ci/{operation}?{urlencode(query)}')
        assert status == 200, body
        assert body['provider_url'] == f'{origin}/b'
        if operation == 'pipeline':
            assert body['pipeline']['provider_url'] == f'{origin}/b'
            assert body['pipeline']['provider_repo_id'] == '17'
        else:
            assert body['log'] == 'log from b'
    assert all(path.startswith('/b/') for _, path in requests)
