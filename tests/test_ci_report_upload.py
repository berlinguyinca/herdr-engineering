"""Uploader contract exercised against real local HTTP servers."""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.ci_report_upload import main
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_reports import MAX_XML_BYTES
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.fixture
def upload_server():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={'private-operator': 'operator', 'viewer': 'viewer'})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    yield server
    server.shutdown()


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    report = tmp_path / 'junit.xml'
    report.write_text('<testsuite><testcase name="passes"/><testcase name="fails"><failure message="bad"/></testcase></testsuite>')
    for key, value in {'CI_REPO': 'org/app', 'CI_REPO_ID': '17', 'CI_PIPELINE_NUMBER': '3',
                       'CI_COMMIT_BRANCH': 'main', 'CI_COMMIT_SHA': 'abcdef',
                       'HERDR_ENGINEERING_API_KEY': 'private-operator'}.items():
        monkeypatch.setenv(key, value)
    return report


def test_upload_from_woodpecker_environment(upload_server, report_env, monkeypatch, capsys):
    url = f'http://127.0.0.1:{upload_server.port}'
    monkeypatch.setenv('HERDR_ENGINEERING_CONTROL_PLANE_URL', url)
    assert main([str(report_env)]) == 0
    output = capsys.readouterr()
    assert '2 tests' in output.out
    assert 'private-operator' not in output.out + output.err
    request = urllib.request.Request(url + '/api/v1/ci/reports', headers={'X-API-Key': 'viewer'})
    with urllib.request.urlopen(request, timeout=3) as response:
        result = json.load(response)['reports'][0]
    assert (result['repository'], result['provider_repo_id'], result['run_number']) == ('org/app', '17', 3)
    assert result['summary']['failed'] == 1


@pytest.mark.parametrize('key', ['wrong-private-key', 'viewer'])
def test_upload_authorization_failure_is_nonzero_and_private(upload_server, report_env, monkeypatch, capsys, key):
    monkeypatch.setenv('HERDR_ENGINEERING_API_KEY', key)
    assert main([str(report_env), '--control-plane', f'http://127.0.0.1:{upload_server.port}']) == 1
    output = capsys.readouterr()
    assert 'HTTP' in output.err
    assert key not in output.out + output.err


@pytest.mark.parametrize('url', ['file:///tmp/secret', 'https://user:password@example.org', 'http://example.org/?key=secret'])
def test_invalid_destinations_are_rejected_without_disclosing_url(report_env, capsys, url):
    assert main([str(report_env), '--control-plane', url]) == 1
    output = capsys.readouterr()
    assert url not in output.out + output.err
    assert 'password' not in output.err


def test_invalid_xml_large_file_and_missing_metadata_fail_before_upload(report_env, monkeypatch, capsys):
    args = [str(report_env), '--control-plane', 'http://127.0.0.1:1']
    report_env.write_text('<broken')
    assert main(args) == 1
    assert 'JUnit' in capsys.readouterr().err
    report_env.write_bytes(b'x' * (MAX_XML_BYTES + 1))
    assert main(args) == 1
    assert '1 MiB' in capsys.readouterr().err
    report_env.write_text('<testsuite/>')
    monkeypatch.delenv('CI_COMMIT_SHA')
    assert main(args) == 1
    assert 'revision' in capsys.readouterr().err


def test_redirect_is_not_followed_and_response_is_bounded(report_env, capsys):
    visits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            visits.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(302 if self.path.startswith('/redirect') else 200)
            if self.path.startswith('/redirect'):
                self.send_header('Location', '/leak')
            self.end_headers()
            if not self.path.startswith('/redirect'):
                try:
                    self.wfile.write(b'x' * (3 * 1024 * 1024))
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_GET(self):
            visits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        assert main([str(report_env), '--control-plane', base + '/redirect']) == 1
        assert visits == ['/redirect/api/v1/ci/reports']
        assert 'HTTP 302' in capsys.readouterr().err
        assert main([str(report_env), '--control-plane', base]) == 1
        assert 'response' in capsys.readouterr().err.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_invalid_private_key_is_never_echoed(report_env, monkeypatch, capsys):
    monkeypatch.setenv('HERDR_ENGINEERING_API_KEY', 'private-key-☃')
    assert main([str(report_env), '--control-plane', 'http://127.0.0.1:1']) == 1
    output = capsys.readouterr()
    assert 'private-key' not in output.err
    assert 'operator key' in output.err


def test_explicit_metadata_overrides_environment(upload_server, report_env):
    assert main([str(report_env), '--control-plane', f'http://127.0.0.1:{upload_server.port}',
                 '--repository', 'org/other', '--run-number', '9', '--branch', 'dev',
                 '--revision', 'fedcba', '--provider-repo-id', '42']) == 0
    result = upload_server.repo.ci_reports()['reports'][0]
    assert (result['repository'], result['branch'], result['revision'], result['run_number'],
            result['provider_repo_id']) == ('org/other', 'dev', 'fedcba', 9, '42')
