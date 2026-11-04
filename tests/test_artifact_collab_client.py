"""`herdr-eng artifacts collab ...` and `artifacts retract`: exact method/path/body against a real local HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering import artifact_collab_client as cc

SECRET = "fictional-collab-api-key"


@pytest.fixture
def endpoint():
    captured, state = [], {"reply": None}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _handle(self, method):
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            captured.append((method, self.path, dict(self.headers), json.loads(raw) if raw else None))
            status, obj = state["reply"] or (200, {"ok": True, "path": self.path})
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", captured, state
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TEST_COLLAB_KEY", SECRET)


def run(url, *argv):
    return cc.main([*argv, "--control-plane", url, "--api-key-env", "TEST_COLLAB_KEY"])


def test_list(endpoint, capsys):
    url, reqs, _ = endpoint
    assert run(url, "collab", "list", "a1") == 0
    assert [(m, p, b) for m, p, _, b in reqs] == [("GET", "/api/v1/artifacts/a1/collaborators", None)]
    assert {k.lower(): v for k, v in reqs[0][2].items()}["x-api-key"] == SECRET
    out = capsys.readouterr()
    assert json.loads(out.out) == {"ok": True, "path": "/api/v1/artifacts/a1/collaborators"}
    assert SECRET not in out.out + out.err


def test_grant_default_read_30(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "collab", "grant", "a1", "agent-x") == 0
    assert [(m, p, b) for m, p, _, b in reqs] == [
        ("POST", "/api/v1/artifacts/a1/collaborators",
         {"agent_id": "agent-x", "permission": "read", "expires_in_days": 30})]


def test_grant_write_days(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "collab", "grant", "a1", "agent-x", "--write", "--days", "90") == 0
    assert reqs[0][3] == {"agent_id": "agent-x", "permission": "write", "expires_in_days": 90}
    assert run(url, "collab", "grant", "a1", "agent-x", "--days", "1") == 0
    assert reqs[1][3] == {"agent_id": "agent-x", "permission": "read", "expires_in_days": 1}


@pytest.mark.parametrize("days", ["0", "91", "-1", "abc", "1.5", ""])
def test_grant_days_refused_locally(endpoint, capsys, days):
    url, reqs, _ = endpoint
    assert run(url, "collab", "grant", "a1", "agent-x", "--days", days) != 0
    assert reqs == []
    assert "--days" in capsys.readouterr().err


def test_revoke(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "collab", "revoke", "a1", "agent-x") == 0
    assert [(m, p, b) for m, p, _, b in reqs] == [("POST", "/api/v1/artifacts/a1/collaborators/agent-x/revoke", {})]


def test_restrict(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "collab", "restrict", "a1") == 0
    assert [(m, p, b) for m, p, _, b in reqs] == [("POST", "/api/v1/artifacts/a1/collab-restrict", {})]


def test_retract_bare(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "retract", "a1") == 0
    assert [(m, p, b) for m, p, _, b in reqs] == [("POST", "/api/v1/artifacts/a1/retract", {})]


def test_retract_with_options(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "retract", "a1", "--version-id", "v2", "--reason", "leaked key") == 0
    assert reqs[0][3] == {"version_id": "v2", "reason": "leaked key"}
    assert run(url, "retract", "a1", "--reason", "r", "--restrict-all") == 0
    assert reqs[1][3] == {"reason": "r", "restrict_all": True}
    assert run(url, "retract", "a1", "--restrict-all") == 0
    assert reqs[2][3] == {"restrict_all": True}


def test_retract_500_prints_body_and_fails(endpoint, capsys):
    url, reqs, state = endpoint
    state["reply"] = (500, {"error": "not persisted", "restrict_all_offered": True, "collab_write_revoked": False})
    assert run(url, "retract", "a1") != 0
    out = capsys.readouterr()
    assert json.loads(out.out)["restrict_all_offered"] is True
    assert SECRET not in out.out + out.err


def test_error_status_non_json_body(endpoint, capsys):
    url, _, state = endpoint
    state["reply"] = (404, "plain")
    assert run(url, "collab", "list", "a1") != 0
    assert capsys.readouterr().err


def test_hostile_ids_are_quoted(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "collab", "revoke", "a/../b?x=1#f", "ag/ent%2F") == 0
    assert reqs[0][1] == "/api/v1/artifacts/a%2F..%2Fb%3Fx%3D1%23f/collaborators/ag%2Fent%252F/revoke"
    assert run(url, "retract", "a b/c") == 0
    assert reqs[1][1] == "/api/v1/artifacts/a%20b%2Fc/retract"


def test_unreachable_is_nonzero(capsys):
    assert run("http://127.0.0.1:1", "collab", "list", "a1") != 0
    assert SECRET not in capsys.readouterr().err


def test_registered_under_artifacts_cli():
    from herdr_engineering.cli import _build_parser as build_parser
    args = build_parser().parse_args(["artifacts", "collab", "grant", "a1", "ag", "--write"])
    assert args.func is cc.run_command and args.write is True
    assert build_parser().parse_args(["artifacts", "retract", "a1", "--restrict-all"]).restrict_all is True
