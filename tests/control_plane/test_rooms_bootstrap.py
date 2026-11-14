"""Pure logic of deploy/control-plane/rooms-bootstrap.py (no mocks)."""
import http.server
import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import (
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import TOKENS, _digest
from tests.control_plane.test_collab_operator import call

_SPEC = importlib.util.spec_from_file_location(
    "rooms_bootstrap", Path(__file__).resolve().parents[2] / "deploy" / "control-plane" / "rooms-bootstrap.py")
rb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rb)


def test_names_are_valid_collab_handles():
    assert all(rb.valid_agent_name(n) for n in rb.AGENT_NAMES)
    assert not rb.valid_agent_name("Bad_Name") and not rb.valid_agent_name("a" * 33)
    assert len(rb.AGENT_NAMES) >= rb.GROUP_MEMBER_MIN


def test_plan_empty_then_idempotent():
    p = rb.plan([], [], None)
    assert p == {"agents": list(rb.AGENT_NAMES), "room": True, "members": list(rb.AGENT_NAMES)}
    agents = [{"name": n, "status": "active"} for n in rb.AGENT_NAMES]
    done = rb.plan(agents, [{"name": rb.ROOM_NAME}], [{"name": n} for n in rb.AGENT_NAMES])
    assert rb.is_noop(done)


def test_plan_ignores_revoked_agents_and_partial_members():
    agents = [{"name": "tui-smoke-a", "status": "revoked"}, {"name": "tui-smoke-b", "status": "active"}]
    p = rb.plan(agents, [{"name": rb.ROOM_NAME}], [{"name": "tui-smoke-b"}])
    assert p == {"agents": ["tui-smoke-a"], "room": False, "members": ["tui-smoke-a"]}


def test_redact_hides_secrets():
    out = rb.redact('cred hca_abcdefgh12345 Bearer xyz postgres://u:p@h/db "token": "s3cret"')
    assert "abcdefgh" not in out and "xyz" not in out and "u:p@h" not in out and "s3cret" not in out


# -- real-listener integration: a real CollabService behind the operator listener, nothing mocked --------------------
BASE = rb.BASE


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(tmp_path):
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}),
                        sink=FileEventLog(str(tmp_path / "log")), public_config={"public_url": "https://x/"})
    port = _free_port()
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, allow_open=True,
                             public_port=0, public_host="127.0.0.1", collab_only=True, collab_require_https=True,
                             operator_port=port, operator_user="alice", operator_email="alice@example.org",
                             operator_hosts=[f"127.0.0.1:{port}"])
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    dash = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, allow_open=True)   # answers /api/v1/health
    t2 = threading.Thread(target=dash.serve, daemon=True)
    t2.start()
    dash.wait_until_ready()
    yield type("S", (), dict(op=port, bridge=f"http://127.0.0.1:{dash.port}", url=f"http://127.0.0.1:{port}"))
    dash.shutdown()
    srv.shutdown()
    t.join(timeout=5)
    t2.join(timeout=5)


def run_main(stack, capsys, *flags):
    rc = rb.main(["--operator-url", stack.url, "--bridge-url", stack.bridge, "--wait", "5", *flags])
    return rc, capsys.readouterr().out


def created(out):
    return [ln for ln in out.splitlines() if ln.startswith("CREATED")]


def agents_and_rooms(stack):
    return (call(stack.op, "GET", f"{BASE}/agents", host=f"127.0.0.1:{stack.op}")[1]["agents"],
            call(stack.op, "GET", f"{BASE}/rooms", host=f"127.0.0.1:{stack.op}")[1]["rooms"])


def test_main_twice_second_run_creates_nothing(stack, capsys):
    rc, out = run_main(stack, capsys)
    assert rc == 0, out
    assert len(created(out)) == 5 and "FAIL" not in out        # 2 agents, room, 2 memberships
    rc, out = run_main(stack, capsys)
    assert rc == 0, out
    assert created(out) == [] and "FAIL" not in out
    assert "PASS message posted and read back" in out
    agents, rooms = agents_and_rooms(stack)
    assert sorted(a["name"] for a in agents) == sorted(rb.AGENT_NAMES) and [r["name"] for r in rooms] == [rb.ROOM_NAME]


def test_partial_state_creates_only_the_missing_membership(stack, capsys):
    host = f"127.0.0.1:{stack.op}"
    ids = {}
    for n in rb.AGENT_NAMES:
        s, info = call(stack.op, "POST", f"{BASE}/agents/connect", {"harness_type": "tui-smoke", "name": n}, host=host)
        assert s == 201
        ids[n] = info["agent_id"]
    s, proj = call(stack.op, "POST", f"{BASE}/projects", {"name": rb.PROJECT_NAME}, host=host)
    s, room = call(stack.op, "POST", f"{BASE}/projects/{proj['project_id']}/rooms", {"name": rb.ROOM_NAME}, host=host)
    s, _ = call(stack.op, "POST", f"{BASE}/rooms/{room['room_id']}/agents",
                {"agent_id": ids["tui-smoke-a"], "actions": rb.CHAT_ACTIONS}, host=host)
    assert s in (200, 201)
    rc, out = run_main(stack, capsys)
    assert rc == 0, out
    assert created(out) == ["CREATED membership tui-smoke-b -> tui-smoke"]


def test_dry_run_creates_nothing(stack, capsys):
    rc, out = run_main(stack, capsys, "--dry-run")
    assert rc == 0, out
    assert created(out) == [] and "WOULD create agent tui-smoke-a" in out
    assert agents_and_rooms(stack) == ([], [])


def test_check_only_no_post_skips_the_smoke_message(stack, capsys):
    run_main(stack, capsys)
    rc, out = run_main(stack, capsys, "--check-only", "--no-post")
    assert rc == 0, out
    assert "SKIP message posted and read back" in out and created(out) == []
    assert "nonce" not in out


def test_failures_print_fail_lines_not_tracebacks(capsys):
    rc = rb.main(["--operator-url", "http://127.0.0.1:1", "--bridge-url", "http://127.0.0.1:1", "--wait", "0.3"])
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL" in out and "Traceback" not in out
    assert "SKIP bridge health answers" in out            # never probed: not a FAIL


# -- pure rules ----------------------------------------------------------------------------------------------------
def test_expired_agents_count_as_missing():
    now = 1_000.0
    assert rb.is_active({"status": "active", "expires_at": now + 5}, now)
    assert not rb.is_active({"status": "active", "expires_at": now - 5}, now)
    assert rb.is_active({"status": "active", "expires_at": None}, now)
    agents = [{"name": "tui-smoke-a", "status": "active", "expires_at": 1.0},
              {"name": "tui-smoke-b", "status": "active", "expires_at": None}]
    assert rb.plan(agents, [{"name": rb.ROOM_NAME}], [])["agents"] == ["tui-smoke-a"]


def test_redact_hides_any_dsn_with_credentials_only():
    out = rb.redact("a mongodb+srv://user:pw0rd@host/db b redis://:x@h c")
    assert "pw0rd" not in out and "user:" not in out
    assert rb.redact("see https://example.org/path") == "see https://example.org/path"


def test_bridge_group_match_is_exact():
    assert rb.has_group({"json": [{"name": "tui-smoke"}]})
    assert not rb.has_group({"json": [{"name": "tui-smoke-old"}], "note": "tui-smoke"})


class _Serve:
    def __init__(self, handler):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_find_project_uses_get_projects_and_redirects_are_not_followed():
    seen = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    target = _Serve(Target)

    class Front(Target):
        def do_GET(self):
            if self.path.endswith("/projects"):
                body = json.dumps({"projects": [{"name": "x", "project_id": "p0"},
                                                {"name": rb.PROJECT_NAME, "project_id": "p1"}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", target.url + "/leak")
                self.end_headers()

    front = _Serve(Front)
    try:
        assert rb.find_project(rb.Http(front.url))["project_id"] == "p1"
        with pytest.raises(RuntimeError, match="HTTP 302"):
            rb.Http(front.url).call("GET", "/other")
        assert seen == []
    finally:
        front.close()
        target.close()
