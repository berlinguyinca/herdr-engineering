"""`herdr-eng artifacts members ...`: exact method/path/body against a real local HTTP server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering import artifact_members_client as mc

SECRET = "fictional-members-api-key"


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
    monkeypatch.setenv("TEST_MEMBERS_KEY", SECRET)


def run(url, *argv):
    return mc.main(["members", *argv, "--control-plane", url, "--api-key-env", "TEST_MEMBERS_KEY"])


def sent(reqs):
    return [(m, p, b) for m, p, _, b in reqs]


def test_read_commands_use_get_and_print_json(endpoint, capsys):
    url, reqs, _ = endpoint
    assert run(url, "list") == 0
    assert run(url, "show", "alice") == 0
    assert run(url, "links", "--audience", "public", "--state", "live") == 0
    assert sent(reqs) == [("GET", "/api/v1/artifact-members", None), ("GET", "/api/v1/artifact-members/alice", None),
                          ("GET", "/api/v1/artifact-members/links?audience=public&state=live", None)]
    assert {k.lower(): v for k, v in reqs[0][2].items()}["x-api-key"] == SECRET
    out = capsys.readouterr()
    assert [json.loads(line)["ok"] for line in out.out.splitlines()] == [True, True, True]
    assert SECRET not in out.out + out.err


def test_member_segment_is_percent_encoded(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "show", "eu:r+x/../y") == 0
    assert reqs[0][1] == "/api/v1/artifact-members/eu%3Ar%2Bx%2F..%2Fy"


def test_limits_send_only_the_given_fields(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "limits", "alice", "--max-artifacts", "5", "--max-member-bytes", "none") == 0
    assert sent(reqs) == [("POST", "/api/v1/artifact-members/alice/limits",
                           {"max_artifacts": 5, "max_member_bytes": None})]


@pytest.mark.parametrize("bad", [["0"], ["-3"], ["1.5"], ["abc"]])
def test_limits_reject_bad_values_before_any_request(endpoint, bad, capsys):
    url, reqs, _ = endpoint
    assert run(url, "limits", "alice", "--max-artifacts", *bad) == 2
    assert reqs == []


def test_limits_need_a_field(endpoint, capsys):
    url, reqs, _ = endpoint
    assert run(url, "limits", "alice") == 2 and reqs == []


def test_sharing_and_revoke(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "disable-sharing", "alice") == 0
    assert run(url, "enable-sharing", "alice") == 0
    assert run(url, "revoke-share", "alice", "share_1") == 0
    assert sent(reqs) == [("POST", "/api/v1/artifact-members/alice/sharing", {"enabled": False}),
                          ("POST", "/api/v1/artifact-members/alice/sharing", {"enabled": True}),
                          ("POST", "/api/v1/artifact-members/alice/shares/share_1/revoke", {})]


def test_purge_requires_the_matching_confirm(endpoint, capsys):
    url, reqs, _ = endpoint
    assert run(url, "purge", "alice", "a1") == 2                     # argparse: --confirm is required
    assert run(url, "purge", "alice", "a1", "--confirm", "a2") == 2
    assert reqs == []
    assert run(url, "purge", "alice", "a1", "--confirm", "a1") == 0
    assert sent(reqs) == [("POST", "/api/v1/artifact-members/alice/artifacts/a1/purge", {"confirm": "a1"})]


def test_http_errors_exit_nonzero_and_never_echo_the_key(endpoint, capsys):
    url, reqs, state = endpoint
    state["reply"] = (409, {"error": f"cannot purge {SECRET}"})
    assert run(url, "purge", "alice", "a1", "--confirm", "a1") == 1
    err = capsys.readouterr()
    assert "HTTP 409" in err.err and SECRET not in err.err


def test_unreachable_control_plane_fails_cleanly(capsys):
    assert run("http://127.0.0.1:1", "list") == 1
    assert SECRET not in capsys.readouterr().err
