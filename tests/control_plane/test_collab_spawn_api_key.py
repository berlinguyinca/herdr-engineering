"""collab-spawn sends X-API-Key only when given a key file (mode 0600): ingest report and scheduler calls."""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("collab_spawn_key", ROOT / "deploy/control-plane/collab-spawn.py")
spawn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spawn)

ANSWER = {"state": "reserved", "host_uid": "uid1", "reservation_id": "res1",
          "hosts": [{"host_uid": "uid1", "aliases": ["h"]}]}


@pytest.fixture
def endpoint():
    seen: list[tuple[str, str | None]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            seen.append((self.path, self.headers.get("X-API-Key")))
            body = json.dumps(ANSWER).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def make(url, tmp_path, key=None):
    worker = tmp_path / "worker.sh"
    worker.write_text("#!/bin/sh\n")
    return spawn.Runner(url, tmp_path / "state", "herdr", "ssh", worker, "h", say=lambda m: None, api_key=key)


def test_ingest_report_and_every_scheduler_call_send_the_key(endpoint, tmp_path):
    url, seen = endpoint
    r = make(url, tmp_path, "the-key")
    r.report_session("h", "w1:p1", "codex", "n")
    host, rid = r.scheduler_host(spawn.PlacementSpec("req1", "codex", None, None), ["h"])
    r.scheduler_release(rid)
    assert (host, rid) == ("h", "res1")
    paths = [p for p, _ in seen]
    assert paths == ["/api/v1/ingest", "/api/v1/scheduler/reservations", "/api/v1/scheduler",
                     "/api/v1/scheduler/reservations/commit", "/api/v1/scheduler/reservations/release"]
    assert all(k == "the-key" for _, k in seen)


def test_no_key_file_means_no_header(endpoint, tmp_path):
    url, seen = endpoint
    r = make(url, tmp_path)
    r.report_session("h", "w1:p1", "codex", "n")
    r.scheduler_host(spawn.PlacementSpec("req1", "codex", None, None), ["h"])
    assert seen and all(k is None for _, k in seen)


def test_key_file_must_be_0600(tmp_path):
    p = tmp_path / "k"
    p.write_text("sekret\n")
    p.chmod(0o640)
    with pytest.raises(ValueError) as exc:
        spawn.read_key_file(p)
    assert "0600" in str(exc.value) and "sekret" not in str(exc.value)
    p.chmod(0o600)
    assert spawn.read_key_file(p) == "sekret"
