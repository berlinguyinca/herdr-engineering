# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric trust slice 3: the agent task routes and the ``herdr-eng rooms my-tasks|respond`` agent mode.

Real CollabService, real HTTP server, real unix-socket whois fixture, and the real CLI run as a subprocess. No mocks."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.control_plane.collab_team_roles import normalize_team_roles
from herdr_engineering.control_plane.collab_team_rooms import normalize_team_rooms
from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_fabric_auth import (  # noqa: F401
    auth,
    fabric_agent,
    fw,
    http_world,
    req,
)
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, _digest, code, svc  # noqa: F401

REPO = str(Path(__file__).resolve().parents[2])
BENDER_NAME = "bender.tail1234.ts.net."


def cred(info):
    return {"Authorization": f"Agent {info['credential']}", "X-Herdr-Device": info["device_id"]}


def mk_task(w, info, text="do it", room=None):
    return w.s.create_task(w.bob if room is None else w.alice, room or w.room, {"target_agent_id": info["agent_id"], "text": text})["task_id"]


def tasks_path(w, room=None):
    return f"{BASE}/rooms/{room or w.room}/agent/tasks"


def respond_path(w, tid, room=None):
    return f"{tasks_path(w, room)}/{tid}/respond"


@pytest.fixture
def vouched(http_world):
    http_world.localapi["nodes"][http_world.lan] = BENDER_NAME
    return http_world


# ------------------------------------------------------------------ server: the happy path, for both principals
@pytest.mark.parametrize("how", ["fabric", "credential"])
def test_agent_lists_only_its_own_open_tasks_and_untrusted(vouched, how):
    w = vouched
    mine, other = fabric_agent(w), fabric_agent(w)
    h = auth(mine) if how == "fabric" else cred(mine)
    t1 = mk_task(w, mine, "mine")
    mk_task(w, other, "not mine")
    s, b = req(w, "GET", tasks_path(w), headers=h)
    assert s == 200
    assert [t["task_id"] for t in b["tasks"]] == [t1]
    assert b["tasks"][0]["text"] == "mine" and b["tasks"][0]["untrusted"] is True and b["tasks"][0]["attempt"] == 1


@pytest.mark.parametrize("how", ["fabric", "credential"])
def test_agent_accepts_then_finishes_and_the_creator_sees_the_result(vouched, how):
    w = vouched
    info = fabric_agent(w)
    h = auth(info) if how == "fabric" else cred(info)
    tid = mk_task(w, info)
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "done", "result": "x"})[0] == 400   # accept first
    s, b = req(w, "POST", respond_path(w, tid), headers=h, body={"status": "accepted"})
    assert s == 200 and b["status"] == "accepted"
    s, b = req(w, "POST", respond_path(w, tid), headers=h, body={"status": "done", "result": "all green", "attempt": 1})
    assert s == 200 and b["status"] == "done"
    s, b = req(w, "POST", respond_path(w, tid), headers=h, body={"status": "done", "result": "all green", "attempt": 1})
    assert s == 200 and b["replayed"] is True                                        # replay-safe, writes nothing
    seen = next(t for t in w.s.list_tasks(w.bob, w.room)["tasks"] if t["task_id"] == tid)
    assert seen["status"] == "done" and seen["result"] == "all green"
    assert req(w, "GET", tasks_path(w), headers=h)[1]["tasks"] == []
    last = [e for e in w.s._log if e["event_type"] == "task_updated"][-1]["payload"]
    assert (last.get("via") == "fabric") == (how == "fabric")                            # the audit row says how it got in


def test_failed_is_allowed_and_unknown_fields_are_not(vouched):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info)
    assert req(w, "POST", respond_path(w, tid), headers=auth(info), body={"status": "failed", "extra": 1})[0] == 400
    assert req(w, "POST", respond_path(w, tid), headers=auth(info), body={"status": "failed", "result": "no"})[0] == 200


# ------------------------------------------------------------------ server: scoping and refusals
def test_a_task_of_another_room_is_404_and_not_listed(vouched):
    w = vouched
    info = fabric_agent(w)
    out = w.s.grant_agent(w.alice, w.room2, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute"]})
    if out["status"] == "pending":
        w.s.decide_agent_request(w.alice, w.room2, out["request_id"], {"decision": "approve"})
    tid = mk_task(w, info, room=w.room2)
    assert req(w, "GET", tasks_path(w), headers=auth(info))[1]["tasks"] == []
    assert [t["task_id"] for t in req(w, "GET", tasks_path(w, w.room2), headers=auth(info))[1]["tasks"]] == [tid]
    assert req(w, "POST", respond_path(w, tid, w.room), headers=auth(info), body={"status": "accepted"})[0] == 404
    assert req(w, "POST", respond_path(w, tid, "room_nope"), headers=auth(info), body={"status": "accepted"})[0] == 404
    assert req(w, "POST", respond_path(w, tid, w.room2), headers=auth(info), body={"status": "accepted"})[0] == 200


def test_someone_elses_task_is_404(vouched):
    w = vouched
    a, b = fabric_agent(w), fabric_agent(w)
    tid = mk_task(w, b)
    assert req(w, "POST", respond_path(w, tid), headers=auth(a), body={"status": "accepted"})[0] == 404
    assert req(w, "POST", respond_path(w, "task_nope"), headers=auth(a), body={"status": "accepted"})[0] == 404


def test_stale_attempt_is_409_after_a_lease_requeue(vouched):
    w = vouched
    w.s.task_lease_seconds = 600.0
    info = fabric_agent(w)
    tid = mk_task(w, info)
    h = auth(info)
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "accepted"})[0] == 200
    w.s.clock.t += 601
    s, b = req(w, "GET", tasks_path(w), headers=h)                                    # polling requeues, attempt rises
    assert s == 200 and b["tasks"][0]["status"] == "pending" and b["tasks"][0]["attempt"] == 2
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "accepted", "attempt": 1})[0] == 409
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "accepted"})[0] == 409     # fence is mandatory
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "accepted", "attempt": 2})[0] == 200
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "done", "result": "late", "attempt": 1})[0] == 409
    assert req(w, "POST", respond_path(w, tid), headers=h, body={"status": "done", "result": "ok", "attempt": 2})[0] == 200


def test_external_agents_are_refused_as_update_task_does(vouched):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info)
    w.s._agents[info["agent_id"]]["external"] = {"room_id": w.room, "hard_expiry": 1e18}
    assert req(w, "GET", tasks_path(w), headers=cred(info))[0] == 403
    assert req(w, "POST", respond_path(w, tid), headers=cred(info), body={"status": "accepted"})[0] == 403
    assert req(w, "GET", tasks_path(w), headers=auth(info))[0] == 401                # and Fabric never admits one
    assert w.s._tasks[tid]["status"] == "pending"


def test_fabric_from_unlisted_or_loopback_peer_is_401_on_both_routes(vouched):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info)
    w.localapi["nodes"][w.lan] = "stranger.tail1234.ts.net."
    w.localapi["nodes"]["127.0.0.1"] = BENDER_NAME
    for host in (None, "127.0.0.1"):
        assert req(w, "GET", tasks_path(w), host=host, headers=auth(info))[0] == 401
        assert req(w, "POST", respond_path(w, tid), host=host, headers=auth(info), body={"status": "accepted"})[0] == 401
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    assert req(w, "GET", tasks_path(w), headers=auth(info, **{"X-Forwarded-For": "100.64.0.1"}))[0] == 401
    assert w.s._tasks[tid]["status"] == "pending"


def test_people_cannot_use_the_agent_routes_and_agents_cannot_use_the_human_ones(vouched):
    w = vouched
    w.s.team_rooms = normalize_team_rooms({"enabled": True})
    w.s.team_roles = normalize_team_roles({"enabled": True})
    info = fabric_agent(w)
    tid = mk_task(w, info)
    bearer = {"Authorization": f"Bearer {USERS['bob']}"}
    assert req(w, "GET", tasks_path(w), headers=bearer)[0] == 403
    assert req(w, "POST", respond_path(w, tid), headers=bearer, body={"status": "accepted"})[0] == 403
    human_get = f"{BASE}/rooms/{w.room}/my-tasks"
    human_post = f"{BASE}/rooms/{w.room}/tasks/{tid}/respond"
    for h in (auth(info), cred(info)):
        assert req(w, "GET", human_get, headers=h)[0] == 404
        assert req(w, "POST", human_post, headers=h, body={"status": "accepted"})[0] == 404
    assert w.s._tasks[tid]["status"] == "pending"


# ------------------------------------------------------------------ the real CLI, as a subprocess, against the real server
def cli(w, tmp_path, *argv, env_extra=None, home=None):
    home = home or (tmp_path / "home")
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "nobin"
    bindir.mkdir(exist_ok=True)                      # a PATH without tailscale: the machine header is simply omitted
    env = {"PATH": str(bindir), "HOME": str(home), "PYTHONPATH": REPO, **(env_extra or {})}
    code_ = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    p = subprocess.run([sys.executable, "-c", code_, "rooms", *argv, "--control-plane", f"http://{w.lan}:{w.port}"],
                       cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def test_cli_fabric_mode_with_as_agent(vouched, tmp_path):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info, "run the benchmark")
    rc, out, err = cli(w, tmp_path, "my-tasks", w.room, "--as-agent", info["agent_id"], "--machine", "bender")
    assert rc == 0, err
    assert tid in out and "run the benchmark" in out and "untrusted" in out
    rc, out, err = cli(w, tmp_path, "respond", w.room, tid, "--status", "accepted", "--as-agent", info["agent_id"], "--machine", "bender")
    assert rc == 0, err
    rc, out, err = cli(w, tmp_path, "respond", w.room, tid, "--status", "done", "--result", "42 jobs", "--attempt", "1",
                       "--as-agent", info["agent_id"])                                        # no --machine, no tailscale: header omitted
    assert rc == 0, err
    seen = next(t for t in w.s.list_tasks(w.bob, w.room)["tasks"] if t["task_id"] == tid)
    assert seen["status"] == "done" and seen["result"] == "42 jobs"
    assert [e for e in w.s._log if e["event_type"] == "task_updated"][-1]["payload"]["via"] == "fabric"
    rc, out, _ = cli(w, tmp_path, "my-tasks", w.room, "--as-agent", info["agent_id"], "--json")
    assert rc == 0 and json.loads(out) == {"tasks": []}


def test_cli_wrong_machine_and_stale_attempt_exit_nonzero(vouched, tmp_path):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info)
    rc, _, err = cli(w, tmp_path, "my-tasks", w.room, "--as-agent", info["agent_id"], "--machine", "beast")
    assert rc == 1 and "unauthorized" in err
    rc, _, err = cli(w, tmp_path, "respond", w.room, tid, "--status", "accepted", "--attempt", "7", "--as-agent", info["agent_id"])
    assert rc == 1 and "stale attempt" in err


def test_cli_agent_identity_comes_from_the_herdr_agents_dir(vouched, tmp_path):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info)
    home = tmp_path / "home"
    d = home / "herdr-agents"
    d.mkdir(parents=True)
    (d / "w-1.json").write_text(json.dumps({"agent_id": info["agent_id"], "name": "w-1", "credential": "ignored"}))
    rc, out, err = cli(w, tmp_path, "my-tasks", w.room, home=home)
    assert rc == 0 and tid in out, err
    assert "ignored" not in out + err                                                       # the secret in that file is never read out
    (d / "w-2.json").write_text(json.dumps({"agent_id": "agent_other"}))
    rc, _, err = cli(w, tmp_path, "my-tasks", w.room, home=home)
    assert rc == 1 and "--as-agent" in err                                                  # two agents here: never guess
    rc, _, _ = cli(w, tmp_path, "my-tasks", w.room, "--as-agent", info["agent_id"], home=home)
    assert rc == 0


def test_cli_existing_herdr_agent_id_env_is_honoured(vouched, tmp_path):
    w = vouched
    info = fabric_agent(w)
    tid = mk_task(w, info)
    rc, out, err = cli(w, tmp_path, "my-tasks", w.room, env_extra={"HERDR_AGENT_ID": info["agent_id"]})
    assert rc == 0 and tid in out, err


def test_cli_token_path_is_unchanged_and_wins_over_detection(vouched, tmp_path):
    w = vouched
    info = fabric_agent(w)
    mk_task(w, info)
    w.s.team_rooms = normalize_team_rooms({"enabled": True})
    w.s.team_roles = normalize_team_roles({"enabled": True})
    rc, out, err = cli(w, tmp_path, "my-tasks", w.room, env_extra={"HERDR_COLLAB_TOKEN": USERS["dave"],
                                                                   "HERDR_AGENT_ID": info["agent_id"]})
    assert rc == 0 and "Nothing is assigned to you." in out, err                          # the Bearer human route, not the agent one
    rc, out, err = cli(w, tmp_path, "my-tasks", w.room, "--token-env", "MY_TOK", env_extra={"MY_TOK": USERS["dave"]})
    assert rc == 0 and "Nothing is assigned to you." in out, err
    # positional status keeps working on the token path
    rc, _, err = cli(w, tmp_path, "respond", w.room, "task_nope", "accepted", env_extra={"HERDR_COLLAB_TOKEN": USERS["dave"]})
    assert rc == 1 and "not found" in err


def test_cli_without_token_or_identity_gives_the_helpful_error(vouched, tmp_path):
    w = vouched
    rc, _, err = cli(w, tmp_path, "my-tasks", w.room)
    assert rc == 1 and "HERDR_COLLAB_TOKEN" in err and "--as-agent" in err
    rc, _, err = cli(w, tmp_path, "roles", w.room, "team_x")                                 # other actions never go agent mode
    assert rc == 1 and "HERDR_COLLAB_TOKEN" in err and "--as-agent" not in err


def test_cli_agent_mode_when_fabric_is_off_says_unauthorized(vouched, tmp_path):
    w = vouched
    info = fabric_agent(w)
    from herdr_engineering.control_plane.collab_fabric_trust import normalize_fabric_trust
    w.s.fabric_trust = normalize_fabric_trust(None)
    rc, _, err = cli(w, tmp_path, "my-tasks", w.room, "--as-agent", info["agent_id"])
    assert rc == 1 and "unauthorized" in err


def test_machine_name_from_tailscale_status_json():
    from herdr_engineering.rooms_client import parse_self_machine
    assert parse_self_machine(json.dumps({"Self": {"DNSName": "Bender.tail1234.ts.net.", "HostName": "x"}})) == "bender"
    assert parse_self_machine(json.dumps({"Self": {"HostName": "Beast"}})) == "beast"
    for bad in ("", "nope", "[]", json.dumps({"Self": {}}), json.dumps({"Self": {"DNSName": "bad name!.ts.net."}})):
        assert parse_self_machine(bad) is None
