"""compose.yaml: the control-plane web and worker run under an init, get time to shut down, and report health.

The health commands are run for real (against a local HTTP server and a heartbeat file), not just matched as text."""
import http.server
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())["services"]
SERVICES = ("control-plane-web", "control-plane-worker")


@pytest.mark.parametrize("name", SERVICES)
def test_service_has_init_grace_period_and_healthcheck(name):
    svc = COMPOSE[name]
    assert svc["init"] is True
    assert re.fullmatch(r"\d+s", svc["stop_grace_period"])
    assert int(svc["stop_grace_period"][:-1]) > 20          # the worker waits up to 20 s for a pass
    check = svc["healthcheck"]
    assert check["test"][0] == "CMD"
    assert {"interval", "timeout", "retries", "start_period"} <= set(check)


def test_image_ships_the_tools_the_healthchecks_use():
    dockerfile = (ROOT / "deploy/fabric-stack/Dockerfile").read_text()
    assert "FROM ${RUNTIME_BASE}" in dockerfile and "python:3.12-slim" in dockerfile
    assert "install -y --no-install-recommends curl" in dockerfile
    web = COMPOSE["control-plane-web"]["healthcheck"]["test"]
    worker = COMPOSE["control-plane-worker"]["healthcheck"]["test"]
    assert web[1] == "curl" and worker[1] == "python3"


class _Health(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b'{"status": "degraded"}')

    def log_message(self, *args):
        pass


def _run_web_check(port):
    argv = [a.replace("127.0.0.1:8080", f"127.0.0.1:{port}") for a in COMPOSE["control-plane-web"]["healthcheck"]["test"][1:]]
    assert any(str(port) in a for a in argv)
    return subprocess.run(argv, capture_output=True, timeout=15).returncode


def test_web_healthcheck_passes_while_health_answers_even_when_degraded_and_fails_when_nothing_listens():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        for status in (200, 503):
            _Health.status = status
            assert _run_web_check(port) == 0, status
    finally:
        server.shutdown()
        server.server_close()
    assert _run_web_check(port) != 0


def test_worker_healthcheck_reads_the_heartbeat_the_worker_is_told_to_write(tmp_path):
    svc = COMPOSE["control-plane-worker"]
    path = svc["environment"]["HERDR_WORKER_HEARTBEAT"]
    argv = svc["healthcheck"]["test"][1:]
    assert any(path in a for a in argv)
    beat = tmp_path / "heartbeat"
    argv = [a.replace(path, str(beat)) for a in argv]

    def check():                    # the image's python3 is the test interpreter here
        return subprocess.run([sys.executable, *argv[1:]], capture_output=True).returncode

    assert check() != 0                                   # no heartbeat yet
    beat.touch()
    assert check() == 0                                   # fresh
    old = time.time() - 3600
    os.utime(beat, (old, old))
    assert check() != 0                                   # stale: the loop is wedged
