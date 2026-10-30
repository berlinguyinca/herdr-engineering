"""`herdr-eng artifacts show|versions|for`: request paths, 404 wording, key never printed."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.cli import main

KEY = "fictional-browse-api-key"


@pytest.fixture
def endpoint(monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            status, body = (404, {"error": "not found"}) if "missing" in self.path else (200, {"ok": True, "path": self.path})
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HERDR_API_KEY", KEY)
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def _run(base, *argv):
    return main(["artifacts", *argv[:1], "--control-plane", base, *argv[1:]])


def test_show_and_version_paths(endpoint, capsys):
    base, seen = endpoint
    assert _run(base, "show", "a/1") == 0
    assert _run(base, "show", "a1", "v1") == 0
    assert [p for p, _ in seen] == ["/api/v1/artifacts/a%2F1", "/api/v1/artifacts/a1/versions/v1"]
    assert KEY not in capsys.readouterr().out


def test_versions_asks_for_summaries(endpoint):
    base, seen = endpoint
    assert _run(base, "versions", "a1") == 0
    assert seen[0][0] == "/api/v1/artifacts/a1/versions?summary=true"


def test_for_builds_context_query(endpoint):
    base, seen = endpoint
    assert _run(base, "for", "project", "p 1", "--query", "plot", "--limit", "5") == 0
    assert seen[0][0] == "/api/v1/artifacts/for/project/p%201?q=plot&limit=5"


def test_for_rejects_bad_limit(endpoint, capsys):
    base, seen = endpoint
    assert _run(base, "for", "project", "p1", "--limit", "0") == 2
    assert not seen


def test_not_found_does_not_say_which(endpoint, capsys):
    base, _ = endpoint
    assert _run(base, "show", "missing") == 1
    assert "cannot read it" in capsys.readouterr().err
