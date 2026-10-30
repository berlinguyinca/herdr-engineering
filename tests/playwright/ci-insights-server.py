"""Real local control plane plus TLS Woodpecker protocol server for UI tests."""
import base64
import json
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import HTTPSHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from herdr_engineering.ci import WoodpeckerProvider  # noqa: E402
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.ci_monitor import CIMonitor  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

NOW = int(time.time())
RUNS = [dict(id=i, number=i, status=status, branch=branch, commit='a' * 40,
             created=NOW - (4-i)*86400, started=NOW-(4-i)*86400,
             finished=NOW-(4-i)*86400+60) for i, status, branch in
        [(1, 'success', 'main'), (2, 'failure', 'main'), (3, 'failure', 'main'),
         (4, 'success', 'feature')]]

if '--selection-contract' in sys.argv:
    for row in RUNS:
        row['event'] = 'push' if row['number'] == 2 else 'pull_request'
        if row['number'] == 1:
            row['status'] = 'failure'

if '--old-selection-run' in sys.argv:
    for row in RUNS:
        if row['number'] in (2, 3):
            for key in ('created', 'started', 'finished'):
                row[key] -= 60 * 86400


# --log-tests: the unit-test step prints go -v output and the browser step only karma totals, like real projects do.
LOGS = {12: '\n'.join(['go: downloading modules', '=== RUN   TestAlpha', '--- PASS: TestAlpha (0.01s)', '=== RUN   TestBeta',
                       '    beta_test.go:12: expected 200, got 500', '--- FAIL: TestBeta (0.20s)', 'FAIL',
                       'FAIL\texample.org/app/pkg\t0.3s', '']),
        13: 'Chrome Headless 1.0 (Linux): Executed 5 of 5 SUCCESS (0.1 secs / 0.1 secs)\nTOTAL: 5 SUCCESS\n'}


class ProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if '--log-tests' in sys.argv and path.startswith('/api/repos/17/logs/') and path.endswith('/download'):
            text = LOGS.get(int(path.split('/')[-2]), 'compiling\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(text)))
            self.end_headers()
            self.wfile.write(text)
            return
        if path == '/api/user/repos':
            body = [dict(id=17, full_name='org/app', owner='org', name='app')]
        elif path == '/api/agents':
            body = []
        elif path == '/api/repos/17/permissions':
            body = dict(pull=True, push=False)
        elif path == '/api/repos/17/pipelines':
            body = RUNS
        elif path.startswith('/api/repos/17/pipelines/'):
            number = int(path.rsplit('/', 1)[-1])
            body = {**next(r for r in RUNS if r['number'] == number),
                    'workflows': [dict(id=1, name='Build and deploy', state='failure', children=[
                        dict(id=11, name='Build', state='success', depends_on=[], started=NOW-90, finished=NOW-80),
                        dict(id=12, name='Unit tests', state='failure', depends_on=['Build'], started=NOW-80, finished=NOW-30),
                        dict(id=13, name='Browser tests', state='success', depends_on=['Build'], started=NOW-80, finished=NOW-40),
                        dict(id=14, name='Deploy production', state='blocked', depends_on=['Unit tests', 'Browser tests'])])]}
        elif path.startswith('/api/repos/17/logs/'):
            body = [dict(data=base64.b64encode(b'AssertionError: expected 200, got 500').decode())]
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory() as tmp:
    cert, key = Path(tmp)/'cert.pem', Path(tmp)/'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), ProviderHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    upstream.socket = context.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    client_context = ssl.create_default_context(cafile=str(cert))
    def factory():
        return WoodpeckerProvider(f'https://127.0.0.1:{upstream.server_port}', 'local-test-token',
                                  client=build_opener(HTTPSHandler(context=client_context)))
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, web_root=ROOT/'packages')
    server.ci_monitor.close()
    logs = dict(on_log_report=repo.ingest_ci_log_report, log_known=repo.ci_log_analysis) if '--log-tests' in sys.argv else {}
    server.ci_monitor = CIMonitor(provider_factory=factory, on_observation=repo.observe_ci_pipeline, **logs)
    server.ci_monitor.refresh()
    server.ci_monitor._analyse_logs()      # the log reader has its own thread once started; this harness never starts the monitor
    # Opt-in scale fixture: retained observations only, never extra provider I/O.
    if '--117-projects' in sys.argv:
        for project in range(116):
            for number in range(1, 8):
                started = NOW - number * 86400
                repo.observe_ci_pipeline(dict(
                    provider_url=f'https://127.0.0.1:{upstream.server_port}',
                    provider_repo_id=1000 + project,
                    repository=f'synthetic/project-{project:03d}', run_number=number,
                    revision='b' * 40, branch='main', event='push',
                    state='failure' if number % 3 == 0 else 'success',
                    created_at=datetime.fromtimestamp(started, UTC).isoformat(),
                    started_at=datetime.fromtimestamp(started, UTC).isoformat(),
                    finished_at=datetime.fromtimestamp(started + 60, UTC).isoformat()))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    print(f'PORT={server.port}', flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        server.shutdown()
        upstream.shutdown()
