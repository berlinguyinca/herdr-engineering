# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The workspace-room clients against a real HTTP server that answers like an OLD server or like each refusal, plus the real server
for sanitising. ``herdr-eng rooms join-fabric`` (subprocess) and ``join.sh --fabric`` (bash)."""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.collab_fabric_trust import WS_ID, WS_LABEL_MAX, normalize_fabric_trust
from tests.control_plane import test_collab_fabric_autojoin_cli as cli_t
from tests.control_plane import test_collab_fabric_autojoin_script as sh_t
from tests.control_plane.test_collab_fabric_auth import fabric_agent, fw, lan_ip  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin import aw, lw, trust  # noqa: F401
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import code, svc  # noqa: F401

needs_tools = pytest.mark.skipif(not (shutil.which("curl") and shutil.which("bash") and shutil.which("python3")), reason="needs curl, bash, python3")
WS_ENV = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_TAB_ID": "tab-2", "HERDR_PANE_ID": "pane-3", "HERDR_WORKSPACE_LABEL": "my work"}


class Stub:
    """A real HTTP server. ``mode`` is either an int (always that status + error text) or "old" (400 unknown field when the body carries
    a workspace, else 201)."""

    def __init__(self, mode):
        self.mode, self.bodies = mode, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.bodies.append(body)
                if outer.mode == "old":
                    status, out = (400, {"error": "unknown field"}) if "workspace" in body else (
                        201, {"agent_id": "agent_old", "name": body["name"], "machine": "bender", "fabric_expires_at": 1})
                elif outer.mode == "ok-no-access":
                    status, out = 200, {"agent_id": "agent_x", "name": body["name"], "machine": "bender", "fabric_expires_at": 1,
                                        "workspace": {"id": "ws-1", "label": None}, "room_access": False}
                else:
                    status, out = outer.mode, {"error": f"server says {outer.mode}"}
                raw = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def stub(request):
    s = Stub(request.param)
    yield s
    s.close()


def cli_run(stub, tmp_path, *argv, env=None):
    return cli_t.run(SimpleNamespace(lan="127.0.0.1", fabric_port=stub.port), tmp_path, "join-fabric", "bender-claude", *argv, env_extra=env)


def sh_run(stub, tmp_path, *opts, env=None):
    return sh_t.run(SimpleNamespace(lan="127.0.0.1", fabric_port=stub.port, public_port=stub.port), tmp_path, "--name", "bender-claude", *opts,
                    env_extra=env)


# ---- 1. client/server skew: an old server answers 400 unknown field, the client retries ONCE without the workspace
@pytest.mark.parametrize("stub", ["old"], indirect=True)
def test_cli_retries_once_without_workspace_against_an_old_server(stub, tmp_path):
    rc, out, err = cli_run(stub, tmp_path, env=WS_ENV)
    assert rc == 0, (out, err)
    assert ["workspace" in b for b in stub.bodies] == [True, False] and "tab_id" not in stub.bodies[1] and "pane_id" not in stub.bodies[1]
    assert "room was not used" in (out + err)


@needs_tools
@pytest.mark.parametrize("stub", ["old"], indirect=True)
def test_join_sh_retries_once_without_workspace_against_an_old_server(stub, tmp_path):
    rc, out, err = sh_run(stub, tmp_path, env=WS_ENV)
    assert rc == 0, (out, err)
    assert ["workspace" in b for b in stub.bodies] == [True, False] and "tab_id" not in stub.bodies[1]
    assert "room was not used" in (out + err)


@pytest.mark.parametrize("stub", [400], indirect=True)
def test_cli_does_not_retry_a_400_that_is_not_about_an_unknown_field(stub, tmp_path):
    rc, _, _ = cli_run(stub, tmp_path, env=WS_ENV)
    assert rc == 1 and len(stub.bodies) == 1


# ---- 3a. no access to the workspace room
@pytest.mark.parametrize("stub", ["ok-no-access"], indirect=True)
def test_cli_says_the_room_access_is_gone(stub, tmp_path):
    rc, out, err = cli_run(stub, tmp_path, env=WS_ENV)
    assert rc == 0 and "no access to the workspace room" in (out + err)
    assert "room_id" not in json.loads(cli_t.identity(tmp_path).read_text())


@needs_tools
@pytest.mark.parametrize("stub", ["ok-no-access"], indirect=True)
def test_join_sh_says_the_room_access_is_gone(stub, tmp_path):
    rc, out, err = sh_run(stub, tmp_path, env=WS_ENV)
    assert rc == 0 and "no access to the workspace room" in (out + err)


# ---- 6. join.sh: a message per HTTP status, with the server's own one-line error text
@needs_tools
@pytest.mark.parametrize("stub,needle", [(400, "rejected"), (403, "revoked"), (404, "auto_join"), (409, "different workspace"),
                                         (429, "limit"), (500, "HTTP 500")], indirect=["stub"])
def test_join_sh_explains_each_refusal(stub, needle, tmp_path):
    rc, out, err = sh_run(stub, tmp_path, env=WS_ENV)
    assert rc == 1 and needle in err and f"server says {stub.mode}" in err


# ---- 2. display-only fields never fail a join (client side)
HOSTILE = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_TAB_ID": "bad id", "HERDR_PANE_ID": "p" * 80,
           "HERDR_WORKSPACE_LABEL": "ev‮il\x1b[31m " + "x" * 100}


def _check_hostile(lw, who):
    agent = next(a for a in lw.s._agents.values() if a["name"] == "bender-claude")
    w = agent["workspace"]
    assert w["tab_id"] is None and w["pane_id"] is None
    assert w["label"] is not None and len(w["label"]) <= WS_LABEL_MAX and "‮" not in w["label"] and "\x1b" not in w["label"]


def test_cli_sanitises_the_label_and_omits_invalid_tab_and_pane(lw, tmp_path):
    lw.s.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True))
    rc, out, err = cli_t.run(lw, tmp_path, "join-fabric", "bender-claude", env_extra=HOSTILE)
    assert rc == 0, (out, err)
    _check_hostile(lw, "cli")


@needs_tools
def test_join_sh_sanitises_the_label_and_omits_invalid_tab_and_pane(lw, tmp_path):
    lw.s.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True))
    rc, out, err = sh_t.run(lw, tmp_path, "--name", "bender-claude", env_extra=HOSTILE)
    assert rc == 0, (out, err)
    _check_hostile(lw, "sh")


def test_cli_with_an_invalid_workspace_id_joins_without_a_room(lw, tmp_path):
    lw.s.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True))
    rc, _, err = cli_t.run(lw, tmp_path, "join-fabric", "bender-claude", env_extra={"HERDR_WORKSPACE_ID": "not valid!"})
    assert rc == 0, err
    assert "room_id" not in json.loads(cli_t.identity(tmp_path).read_text())


@needs_tools
def test_join_sh_with_an_invalid_workspace_id_joins_without_a_room(lw, tmp_path):
    lw.s.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True))
    rc, _, err = sh_t.run(lw, tmp_path, "--name", "bender-claude", env_extra={"HERDR_WORKSPACE_ID": "not valid!"})
    assert rc == 0, err
    assert "room_id" not in json.loads(sh_t.ident(tmp_path).read_text())


# ---- 7. one rule: join.sh must carry the server's regex and label length verbatim
def test_join_sh_carries_the_servers_workspace_id_rule():
    text = sh_t.SCRIPT.read_text()
    assert WS_ID.pattern in text, "join.sh's workspace id regex drifted from collab_fabric_trust.WS_ID"
    assert f"[:{WS_LABEL_MAX}]" in text, "join.sh's label limit drifted from collab_fabric_trust.WS_LABEL_MAX"
