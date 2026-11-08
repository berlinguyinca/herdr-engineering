# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""``herdr-eng rooms join-fabric NAME``: the real CLI as a subprocess against the real fabric listener. No mocks."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from tests.control_plane.test_collab_fabric_auth import fabric_agent, fw, lan_ip  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin import aw, lw, set_trust, trust  # noqa: F401
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401

REPO = str(Path(__file__).resolve().parents[2])


def run(lw, tmp_path, *argv, env_extra=None, port=None, home=None, control_plane=True):
    home = home or tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "nobin"
    bindir.mkdir(exist_ok=True)  # no tailscale on PATH: the machine hint is simply omitted
    env = {"PATH": str(bindir), "HOME": str(home), "PYTHONPATH": REPO, **(env_extra or {})}
    code_ = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    p = subprocess.run(
        [sys.executable, "-c", code_, "rooms", *argv,
         *(["--control-plane", f"http://{lw.lan}:{port or lw.fabric_port}"] if control_plane else [])],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )
    return p.returncode, p.stdout, p.stderr


def identity(tmp_path, name="bender-claude"):
    return tmp_path / "home" / "herdr-agents" / f"{name}.json"


def test_join_fabric_writes_a_credential_less_0600_identity_file(lw, tmp_path):
    rc, out, err = run(lw, tmp_path, "join-fabric", "bender-claude")
    assert rc == 0, (out, err)
    f = identity(tmp_path)
    data = json.loads(f.read_text())
    assert set(data) == {"agent_id", "name", "machine"}
    assert data["name"] == "bender-claude" and data["machine"] == "bender"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert data["agent_id"] in lw.s._agents
    assert data["agent_id"] in out


def test_join_fabric_is_idempotent(lw, tmp_path):
    run(lw, tmp_path, "join-fabric", "bender-claude")
    first = json.loads(identity(tmp_path).read_text())
    rc, _, err = run(lw, tmp_path, "join-fabric", "bender-claude")
    assert rc == 0, err
    assert json.loads(identity(tmp_path).read_text()) == first


def test_join_fabric_needs_no_token_and_ignores_one(lw, tmp_path):
    rc, _, err = run(lw, tmp_path, "join-fabric", "bender-claude", env_extra={"HERDR_COLLAB_TOKEN": USERS["alice"]})
    assert rc == 0, err
    assert USERS["alice"] not in err and USERS["alice"] not in identity(tmp_path).read_text()


def test_the_written_file_feeds_the_existing_identity_discovery(lw, tmp_path):
    from herdr_engineering.rooms_client import _agent_identity

    run(lw, tmp_path, "join-fabric", "bender-claude")
    old = os.environ.get("HERDR_AGENT_DIR"), os.environ.get("HERDR_AGENT_ID")
    os.environ["HERDR_AGENT_DIR"] = str(tmp_path / "home" / "herdr-agents")
    os.environ.pop("HERDR_AGENT_ID", None)
    try:
        assert _agent_identity(None) == json.loads(identity(tmp_path).read_text())["agent_id"]
    finally:
        for k, v in zip(("HERDR_AGENT_DIR", "HERDR_AGENT_ID"), old, strict=True):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_hostile_names_are_refused_and_write_nothing(lw, tmp_path):
    for bad in ("../evil", "a/b", ".hidden", "", "x" * 65, "UPPER", "a b"):
        rc, _, err = run(lw, tmp_path, "join-fabric", bad)
        assert rc != 0 and err
    assert not (tmp_path / "home" / "herdr-agents").exists() or not list((tmp_path / "home" / "herdr-agents").iterdir())
    assert not (tmp_path / "evil.json").exists() and not (tmp_path / "home" / "evil.json").exists()


def test_a_refused_join_writes_no_file(lw, tmp_path):
    set_trust(lw, auto_join=False)
    rc, _, err = run(lw, tmp_path, "join-fabric", "bender-claude")
    assert rc != 0 and err
    assert not identity(tmp_path).exists()


def test_the_public_listener_does_not_serve_it(lw, tmp_path):
    rc, _, _ = run(lw, tmp_path, "join-fabric", "bender-claude", port=lw.public_port)
    assert rc != 0 and not identity(tmp_path).exists()


def test_agent_dir_env_is_honoured_like_join_sh(lw, tmp_path):
    d = tmp_path / "custom"
    rc, _, err = run(lw, tmp_path, "join-fabric", "bender-claude", env_extra={"HERDR_AGENT_DIR": str(d)})
    assert rc == 0, err
    assert (d / "bender-claude.json").exists() and not identity(tmp_path).exists()


def test_json_flag_prints_only_the_server_answer(lw, tmp_path):
    rc, out, err = run(lw, tmp_path, "join-fabric", "bender-claude", "--json")
    assert rc == 0, err
    assert set(json.loads(out)) >= {"agent_id", "name", "machine"}


def test_without_a_fabric_listener_url_it_fails_early_and_says_how_to_fix_it(lw, tmp_path):
    rc, out, err = run(lw, tmp_path, "join-fabric", "bender-claude", control_plane=False)
    assert rc == 1 and not out
    assert "fabric listener" in err and "--control-plane" in err and "Traceback" not in err
    assert not identity(tmp_path).exists()


def test_the_fabric_url_is_discovered_from_the_host_configuration(fake_host_config):
    from herdr_engineering import rooms_client

    fake_host_config({"control_plane": {"collab": {"fabric_trust": {"listen_host": "100.64.1.2", "listen_port": 8765}}}})
    assert rooms_client._discover_fabric_url() == "http://100.64.1.2:8765"
    assert rooms_client._fabric_url(None) == "http://100.64.1.2:8765"
    assert rooms_client._fabric_url("http://x:1/") == "http://x:1"          # an explicit URL always wins


def test_discovery_refuses_what_the_listener_could_not_bind(fake_host_config):
    import pytest

    from herdr_engineering import rooms_client

    for trust in ({"listen_host": "127.0.0.1", "listen_port": 8765}, {"listen_host": "100.64.1.2", "listen_port": 0},
                  {"listen_host": "100.64.1.2", "listen_port": "8765"}, {"listen_host": "100.64.1.2"}, {}, None):
        fake_host_config({"control_plane": {"collab": {"fabric_trust": trust}}})
        assert rooms_client._discover_fabric_url() is None
        with pytest.raises(ValueError, match="fabric listener"):
            rooms_client._fabric_url(None)


WS_ENV = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_TAB_ID": "tab-2", "HERDR_PANE_ID": "pane-3", "HERDR_WORKSPACE_LABEL": "my work"}


def ws_on(lw):
    from herdr_engineering.control_plane.collab_fabric_trust import normalize_fabric_trust
    lw.s.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True))


def test_join_fabric_sends_the_workspace_env_and_lands_in_the_room(lw, tmp_path):
    ws_on(lw)
    rc, out, err = run(lw, tmp_path, "join-fabric", "bender-claude", env_extra=WS_ENV)
    assert rc == 0, (out, err)
    data = json.loads(identity(tmp_path).read_text())
    agent = lw.s._agents[data["agent_id"]]
    assert (agent["workspace"]["workspace_id"], agent["workspace"]["tab_id"], agent["workspace"]["pane_id"]) == ("ws-1", "tab-2", "pane-3")
    assert data["room_id"] == agent["workspace"]["room_id"] and lw.s._rooms[data["room_id"]]["name"] == "bender/my work"
    assert set(data) == {"agent_id", "name", "machine", "room_id"}


def test_join_fabric_no_workspace_opts_out(lw, tmp_path):
    ws_on(lw)
    rc, _, err = run(lw, tmp_path, "join-fabric", "bender-claude", "--no-workspace", env_extra=WS_ENV)
    assert rc == 0, err
    data = json.loads(identity(tmp_path).read_text())
    assert set(data) == {"agent_id", "name", "machine"} and not lw.s._agents[data["agent_id"]].get("workspace")


def test_join_fabric_without_the_env_is_the_old_behaviour(lw, tmp_path):
    ws_on(lw)
    rc, _, err = run(lw, tmp_path, "join-fabric", "bender-claude")
    assert rc == 0, err
    assert set(json.loads(identity(tmp_path).read_text())) == {"agent_id", "name", "machine"}
