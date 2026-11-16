# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The command the /herdr::join skill runs (`herdr-eng rooms join-fabric NAME --json`), as the real CLI in separate panes against
the real fabric listener: same workspace shares a room, a different workspace does not, a non-tailnet caller is refused."""
from __future__ import annotations

import json

from tests.control_plane.test_collab_fabric_auth import fabric_agent, fw, lan_ip  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin import aw, lw  # noqa: F401
from tests.control_plane.test_collab_fabric_autojoin_cli import identity, run, ws_on
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401


def pane(ws, tab, pane_id):
    return {"HERDR_WORKSPACE_ID": ws, "HERDR_TAB_ID": tab, "HERDR_PANE_ID": pane_id}


def join(lw, tmp_path, name, env, *extra):
    return run(lw, tmp_path, "join-fabric", name, "--json", *extra, env_extra=env)


def test_two_panes_of_one_workspace_share_one_room_with_the_granted_actions_only(lw, tmp_path):
    ws_on(lw)
    a = join(lw, tmp_path, "claude-p1", pane("ws-1", "t1", "p1"))
    b = join(lw, tmp_path, "codex-p2", pane("ws-1", "t1", "p2"))
    assert a[0] == 0 and b[0] == 0, (a, b)
    ja, jb = json.loads(a[1]), json.loads(b[1])
    assert ja["room_id"] == jb["room_id"] and ja["agent_id"] != jb["agent_id"]
    assert ja["room_access"] is True and ja["workspace"]["id"] == "ws-1"
    room = lw.s._rooms[ja["room_id"]]
    assert room["name"] == "bender/ws-1"
    for ident in (ja, jb):
        assert set(room["grants"][ident["agent_id"]]["actions"]) == {"read", "post", "instruct", "execute"}
    assert json.loads(identity(tmp_path, "claude-p1").read_text())["room_id"] == ja["room_id"]


def test_different_workspaces_get_different_rooms(lw, tmp_path):
    ws_on(lw)
    a = json.loads(join(lw, tmp_path, "claude-p1", pane("ws-1", "t1", "p1"))[1])
    b = json.loads(join(lw, tmp_path, "claude-p9", pane("ws-2", "t1", "p9"))[1])
    assert a["room_id"] != b["room_id"]
    assert lw.s._rooms[b["room_id"]]["name"] == "bender/ws-2"


def test_the_same_name_from_another_workspace_is_refused(lw, tmp_path):
    ws_on(lw)
    assert join(lw, tmp_path, "claude-p1", pane("ws-1", "t1", "p1"))[0] == 0
    rc, _, err = join(lw, tmp_path, "claude-p1", pane("ws-2", "t1", "p1"))
    assert rc != 0 and err


def test_no_workspace_opts_out_of_the_room(lw, tmp_path):
    ws_on(lw)
    rc, out, err = join(lw, tmp_path, "claude-p1", pane("ws-1", "t1", "p1"), "--no-workspace")
    assert rc == 0, err
    assert "room_id" not in json.loads(out) and "workspace" not in json.loads(out)


def test_a_caller_that_is_not_on_the_tailnet_is_refused_and_nothing_is_written(lw, tmp_path):
    ws_on(lw)
    lw.localapi["nodes"].pop(lw.lan)                      # tailscaled does not know this peer
    rc, _, err = join(lw, tmp_path, "claude-p1", pane("ws-1", "t1", "p1"))
    assert rc != 0 and err and not identity(tmp_path, "claude-p1").exists()
    lw.localapi["nodes"][lw.lan] = "stranger.tail1234.ts.net."   # on the tailnet but not allow-listed
    rc, _, err = join(lw, tmp_path, "claude-p1", pane("ws-1", "t1", "p1"))
    assert rc != 0 and err and not identity(tmp_path, "claude-p1").exists()
    assert not [a for a in lw.s._agents.values() if a.get("origin") == "fabric_join"]
