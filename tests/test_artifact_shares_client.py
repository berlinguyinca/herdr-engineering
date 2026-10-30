"""`herdr-eng artifacts share ...`: validation, approve gating, no-token output, mocked pool check."""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering import artifact_shares_client as sc

SECRET = "fictional-share-api-key"
SUMMARY = {"entry": "index.html", "file_count": 3, "total_bytes": 2048, "audience": "lab",
           "expires_at": "2026-12-01T00:00:00+00:00",
           "warnings": ["index.html references https://cdn.example.com/x.js"],
           "files": [{"name": f"f{i}.txt", "size_bytes": 10} for i in range(3)]}
SHARE = {"share_id": "s1", "artifact_id": "a1", "version_id": "v1", "audience": "lab", "state": "pending",
         "allow_emails": ["bob@example.org"], "allow_groups": [], "expires_at": "2026-12-01T00:00:00+00:00",
         "entry": None, "title": "T", "token_hash": "LEAKY-HASH"}


@pytest.fixture
def endpoint():
    captured, state = [], {"post": None, "get_share": None}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            captured.append(("GET", self.path, dict(self.headers), None))
            if self.path.endswith("/events"):
                return self._send(200, {"share_id": "s1", "events": [{"kind": f"e{i}", "token_hash": "LEAKY"} for i in range(5)]})
            if self.path.endswith("/shares"):
                return self._send(200, {"artifact_id": "a1", "shares": [SHARE]})
            self._send(*(state["get_share"] or (200, {"share": SHARE, "summary": SUMMARY})))

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            captured.append(("POST", self.path, dict(self.headers), json.loads(raw) if raw else None))
            if state["post"]:
                return self._send(*state["post"])
            if self.path.endswith("/approve"):
                return self._send(200, {"share": {**SHARE, "state": "live"}, "url": "https://x.example/s/SECRET-TOKEN", "summary": SUMMARY})
            if self.path.endswith("/revoke"):
                return self._send(200, {"share": {**SHARE, "state": "revoked"}})
            self._send(201, {"share": SHARE, "summary": SUMMARY})

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", captured, state
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TEST_SHARE_KEY", SECRET)


def run(url, *argv):
    return sc.main([*argv[:1], *argv[1:], "--control-plane", url, "--api-key-env", "TEST_SHARE_KEY"])


def tty(monkeypatch, text=None):
    stdin = io.StringIO(text or "")
    stdin.isatty = lambda: text is not None
    monkeypatch.setattr("sys.stdin", stdin)


def test_create_payload_and_summary(endpoint, capsys):
    url, reqs, _ = endpoint
    assert run(url, "create", "a1", "v1", "--audience", "lab", "--email", "bob@example.org",
               "--group", "g1", "--expires-in-days", "7", "--entry", "r.html", "--title", "T") == 0
    method, path, headers, body = reqs[0]
    assert (method, path) == ("POST", "/api/v1/artifacts/a1/shares")
    assert body == {"version_id": "v1", "audience": "lab", "allow_emails": ["bob@example.org"],
                    "allow_groups": ["g1"], "expires_in_days": 7, "entry": "r.html", "title": "T"}
    assert {k.lower(): v for k, v in headers.items()}["x-api-key"] == SECRET
    out = capsys.readouterr()
    assert "LEAKY-HASH" not in out.out and SECRET not in out.out + out.err
    assert "3 files" in out.out and "cdn.example.com" in out.out and "pending" in out.out


def test_create_public_sends_no_days_unless_given(endpoint):
    url, reqs, _ = endpoint
    assert run(url, "create", "a1", "v1", "--audience", "public") == 0
    assert reqs[0][3] == {"version_id": "v1", "audience": "public"}


@pytest.mark.parametrize("argv", [
    ["create", "a1", "v1", "--audience", "lab"],
    ["create", "a1", "v1", "--audience", "public", "--email", "a@b.org"],
    ["create", "a1", "v1", "--audience", "public", "--group", "g"],
    ["create", "a1", "v1", "--audience", "lab", "--email", "not-an-email"],
    ["create", "a1", "v1", "--audience", "public", "--expires-in-days", "0"],
    ["create", "a1", "v1", "--audience", "public", "--expires-in-days", "3651"],
    ["create", "a1", "v1", "--audience", "private"],
    ["events", "a1", "s1", "--limit", "0"],
])
def test_validation_exits_2_without_request(endpoint, argv, capsys):
    url, reqs, _ = endpoint
    assert run(url, *argv) == 2
    assert reqs == []


def test_approve_refuses_without_yes_on_non_tty(endpoint, monkeypatch, capsys):
    url, reqs, _ = endpoint
    tty(monkeypatch, None)
    assert run(url, "approve", "a1", "s1") == 1
    assert [r[0] for r in reqs] == ["GET"]
    out = capsys.readouterr()
    assert "--yes" in out.err and "SECRET-TOKEN" not in out.out
    assert "bob@example.org" in out.out  # the summary was still shown


def test_approve_yes_shows_summary_before_post_and_url_once(endpoint, monkeypatch, capsys):
    url, reqs, _ = endpoint
    tty(monkeypatch, None)
    assert run(url, "approve", "a1", "s1", "--yes") == 0
    assert [r[0] for r in reqs] == ["GET", "POST"]
    assert reqs[1][1] == "/api/v1/artifacts/a1/shares/s1/approve" and reqs[1][3] == {"confirm": True}
    out = capsys.readouterr().out
    assert out.count("https://x.example/s/SECRET-TOKEN") == 1
    assert out.index("cdn.example.com") < out.index("SECRET-TOKEN")
    assert "not be shown again" in out


def test_approve_interactive_typed_yes_and_no(endpoint, monkeypatch, capsys):
    url, reqs, _ = endpoint
    tty(monkeypatch, "no\n")
    assert run(url, "approve", "a1", "s1") == 1
    assert [r[0] for r in reqs] == ["GET"]
    tty(monkeypatch, "yes\n")
    assert run(url, "approve", "a1", "s1") == 0
    assert reqs[-1][0] == "POST"


@pytest.mark.parametrize("status,word", [(409, "already"), (410, "expired")])
def test_approve_conflict_and_gone(endpoint, status, word, capsys):
    url, reqs, state = endpoint
    state["post"] = (status, {"error": "whatever"})
    assert run(url, "approve", "a1", "s1", "--yes") == 1
    assert word in capsys.readouterr().err.lower()


def test_approve_non_pending_does_not_post(endpoint, capsys):
    url, reqs, state = endpoint
    state["get_share"] = (200, {"share": {**SHARE, "state": "live"}})
    assert run(url, "approve", "a1", "s1", "--yes") == 1
    assert [r[0] for r in reqs] == ["GET"]
    assert "live" in capsys.readouterr().err


def test_show_list_events_never_print_token_hash_in_human_output(endpoint, capsys):
    url, reqs, _ = endpoint
    assert run(url, "show", "a1", "s1") == 0
    assert "LEAKY" not in capsys.readouterr().out
    assert run(url, "list", "a1") == 0
    listed = capsys.readouterr().out
    assert "s1" in listed and "LEAKY" not in listed
    assert run(url, "events", "a1", "s1", "--limit", "2") == 0
    ev = json.loads(capsys.readouterr().out)
    assert [e["kind"] for e in ev["events"]] == ["e3", "e4"] and "LEAKY" not in json.dumps(ev)


def test_show_truncates_file_list(endpoint, capsys):
    url, _, state = endpoint
    big = {**SUMMARY, "file_count": 100, "files": [{"name": f"f{i}", "size_bytes": 1} for i in range(100)]}
    state["get_share"] = (200, {"share": SHARE, "summary": big})
    assert run(url, "show", "a1", "s1") == 0
    out = capsys.readouterr().out
    assert "f0" in out and "f99" not in out and "more" in out


def test_revoke_is_idempotent(endpoint, capsys):
    url, reqs, state = endpoint
    assert run(url, "revoke", "a1", "s1") == 0
    state["post"] = (200, {"share": {**SHARE, "state": "revoked"}})
    assert run(url, "revoke", "a1", "s1") == 0
    assert "revoked" in capsys.readouterr().out


def test_server_error_message_shown_without_html_or_key(endpoint, capsys):
    url, _, state = endpoint
    state["get_share"] = (404, {"error": "not found"})
    assert run(url, "show", "a1", "nope") == 1
    err = capsys.readouterr().err
    assert "not found" in err and SECRET not in err


def test_check_pool_warns_never_fails(endpoint, monkeypatch, capsys):
    url, reqs, _ = endpoint
    monkeypatch.setattr(sc, "_cognito_users_exist", lambda pool, emails: {e: e.startswith("known") for e in emails})
    assert run(url, "create", "a1", "v1", "--audience", "lab", "--email", "ghost@example.org",
               "--email", "known@example.org", "--check-pool", "--pool-id", "us-west-2_X") == 0
    err = capsys.readouterr().err
    assert "ghost@example.org" in err and "known@example.org" not in err
    assert len(reqs) == 1


def test_check_pool_skipped_without_credentials_or_pool(endpoint, monkeypatch, capsys):
    url, reqs, _ = endpoint
    def boom(pool, emails):
        raise sc.PoolCheckSkipped("no AWS credentials")
    monkeypatch.setattr(sc, "_cognito_users_exist", boom)
    assert run(url, "create", "a1", "v1", "--audience", "lab", "--email", "a@example.org",
               "--check-pool", "--pool-id", "us-west-2_X") == 0
    assert "skipped" in capsys.readouterr().err
    assert run(url, "create", "a1", "v1", "--audience", "lab", "--email", "a@example.org", "--check-pool") == 0
    assert "skipped" in capsys.readouterr().err
