# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric auto-join, workspace rooms: agents that auto-join from the same herdr WORKSPACE on the same machine land in ONE room.

Real CollabService over a real file event log, real HTTP, real unix-socket tailscaled-LocalAPI fixture. No mocks."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_fabric_trust import (
    FABRIC_TRUST_KEYS,
    FabricPeerResolver,
    normalize_fabric_trust,
)
from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_fabric_auth import (  # noqa: F401
    BEAST,
    BENDER,
    SUFFIX,
    fabric_agent,
    fw,
    lan_ip,
)
from tests.control_plane.test_collab_fabric_autojoin import (  # noqa: F401
    OWNER,
    agent_auth,
    aw,
    denied_rows,
    hreq,
    join,
    lw,
    trust,
)
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, _digest, code, svc  # noqa: F401

PEER_ACTIONS = ["execute", "instruct", "post", "read"]


def set_ws(w, **kw):
    w.s.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True, **kw))


@pytest.fixture
def ww(aw):
    """aw with workspace rooms switched on."""
    set_ws(aw)
    return aw


def jw(w, name, ws="w1", label=None, peer=BENDER, fabric_listener=True, **extra):
    workspace = {"id": ws, **({"label": label} if label else {})}
    return join(w, name=name, peer=peer, body={"workspace": workspace, **extra}, fabric_listener=fabric_listener)


def ws_rooms(w):
    return [e["payload"] for e in w.s._log if e["event_type"] == "room_created" and e["payload"].get("fabric_workspace")]


def denied_ws(w, reason):
    return denied_rows(w, reason)


# ------------------------------------------------------------------ config
def test_config_keys_exist_default_off_and_are_validated():
    for key in ("workspace_rooms", "workspace_rooms_per_machine", "workspace_rooms_total", "workspace_room_members"):
        assert key in FABRIC_TRUST_KEYS
    cfg = normalize_fabric_trust(trust(auto_join=True))
    assert cfg["workspace_rooms"] is False
    assert (cfg["workspace_rooms_per_machine"], cfg["workspace_rooms_total"], cfg["workspace_room_members"]) == (100, 500, 50)
    on = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True, workspace_rooms_per_machine=3,
                                      workspace_rooms_total=9, workspace_room_members=4))
    assert on["workspace_rooms"] is True and on["workspace_rooms_per_machine"] == 3 and on["workspace_room_members"] == 4
    for bad in ("yes", 1, None, []):
        with pytest.raises(ValueError, match="workspace_rooms"):
            normalize_fabric_trust(trust(auto_join=True, workspace_rooms=bad))
    for key in ("workspace_rooms_per_machine", "workspace_rooms_total", "workspace_room_members"):
        for bad in (0, -1, True, "5", 1.5, 10**9):
            with pytest.raises(ValueError, match=key):
                normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True, **{key: bad}))
    with pytest.raises(ValueError, match="workspace_rooms requires auto_join"):
        normalize_fabric_trust(trust(workspace_rooms=True))                         # needs auto_join
    with pytest.raises(ValueError, match="workspace_rooms"):
        normalize_fabric_trust({"workspace_rooms": True})


def test_workspace_rooms_is_hot_reloaded(aw):
    raw = {"v": trust(auto_join=True)}
    aw.s.fabric_trust_source = lambda: raw["v"]
    assert jw(aw, "a-1")[1].get("room_id") is None                                  # off: joins, no room
    raw["v"] = trust(auto_join=True, workspace_rooms=True)
    status, out = jw(aw, "a-1")                                                     # the same agent now lands in its room
    assert status == 200 and out["room_id"]
    raw["v"] = {"workspace_rooms": "maybe"}                                         # an invalid edit means OFF
    assert jw(aw, "a-2")[0] == 404


def test_render_collab_carries_the_new_keys(tmp_path):
    import yaml

    from tests.test_central_config_servers import COLLAB, _collab_slice
    ft = {"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["bender"], "auto_join": True, "workspace_rooms": True,
          "workspace_rooms_per_machine": 7, "workspace_rooms_total": 70, "workspace_room_members": 9}
    _, out = _collab_slice(tmp_path, dict(COLLAB, fabric_trust=ft))
    rendered = yaml.safe_load(out.read_text())["sections"]["control_plane"]["values"]["collab"]["fabric_trust"]
    assert rendered == ft
    from herdr_engineering.fleet_config_cli import FleetConfigError
    with pytest.raises(FleetConfigError, match="workspace_rooms"):
        _collab_slice(tmp_path / "bad", dict(COLLAB, fabric_trust=dict(ft, workspace_rooms_total=0)))


# ------------------------------------------------------------------ first join / sharing
def test_first_join_creates_the_room_and_grants_the_agent(ww):
    status, out = jw(ww, "bender-claude", label="my ws")
    assert status == 201
    assert out["workspace"] == {"id": "w1", "label": "my ws"} and out["room_id"] in ww.s._rooms
    room = ww.s._rooms[out["room_id"]]
    assert room["created_by"] == OWNER and room["members"][OWNER]["role"] == "owner"
    assert room["name"] == "bender/my ws"
    proj = ww.s._projects[room["project_id"]]
    assert proj["name"] == "fabric-workspaces" and proj["members"] == {OWNER: "manager"}
    assert sorted(room["grants"][out["agent_id"]]["actions"]) == PEER_ACTIONS
    assert "spawn_helpers" not in room["grants"][out["agent_id"]]["actions"]
    assert "coordinate" not in room["grants"][out["agent_id"]]["actions"]
    pr = agent_auth(ww, out["agent_id"])
    assert ww.s.agent_rooms(pr)["rooms"][0]["room_id"] == out["room_id"]
    assert sorted(ww.s.agent_rooms(pr)["rooms"][0]["actions"]) == PEER_ACTIONS     # effective, not just granted
    ww.s.post_message(pr, out["room_id"], {"text": "hello", "idempotency_key": "k1"})


def test_events_are_fabric_audited_and_carry_the_key(ww):
    jw(ww, "bender-claude", label="x", tab_id="t1", pane_id="p1")
    room_ev = ws_rooms(ww)[-1]
    assert room_ev["fabric_workspace"]["machine"] == "bender" and room_ev["fabric_workspace"]["workspace_id"] == "w1"
    assert (room_ev["via"], room_ev["actor_user_id"], room_ev["actor_agent_id"]) == ("fabric", OWNER, None)
    grant = [e["payload"] for e in ww.s._log if e["event_type"] == "agent_granted"][-1]
    assert grant["via"] == "fabric" and grant["machine"] == "bender" and grant["actor_user_id"] == OWNER
    assert grant["fabric_workspace"]["tab_id"] == "t1" and grant["fabric_workspace"]["pane_id"] == "p1"
    proj = [e["payload"] for e in ww.s._log if e["event_type"] == "project_created"][-1]
    assert proj["via"] == "fabric" and proj["name"] == "fabric-workspaces"


def test_a_second_agent_of_the_same_workspace_shares_the_room(ww):
    _, a = jw(ww, "a-1")
    n_rooms = len(ww.s._rooms)
    status, b = jw(ww, "a-2", ws="w1", tab_id="t2", pane_id="p9")                    # another tab/pane, same workspace
    assert status == 201 and b["room_id"] == a["room_id"] and len(ww.s._rooms) == n_rooms
    assert set(ww.s._rooms[a["room_id"]]["grants"]) == {a["agent_id"], b["agent_id"]}
    assert len(ws_rooms(ww)) == 1
    assert len([e for e in ww.s._log if e["event_type"] == "project_created" and e["payload"].get("fabric_workspaces")]) == 1
    pa, pb = agent_auth(ww, a["agent_id"]), agent_auth(ww, b["agent_id"])
    ww.s.post_message(pa, a["room_id"], {"text": "ping", "idempotency_key": "k"})
    assert "ping" in json.dumps(ww.s.read_events(pb, a["room_id"], 0, 50)["events"])


def test_the_same_workspace_id_on_another_machine_is_another_room(ww):
    _, a = jw(ww, "a-1", peer=BENDER)
    ww.localapi["nodes"][BEAST] = f"beast.{SUFFIX}."
    _, b = jw(ww, "b-1", peer=BEAST)
    assert a["room_id"] != b["room_id"]
    assert ww.s._rooms[b["room_id"]]["name"] == "beast/w1"
    assert b["agent_id"] not in ww.s._rooms[a["room_id"]]["grants"]
    assert code(ww.s.read_events, agent_auth(ww, b["agent_id"], BEAST), a["room_id"], 0, 10) in (403, 404)


def test_a_different_workspace_on_one_machine_is_a_different_room(ww):
    _, a = jw(ww, "a-1", ws="w1")
    _, b = jw(ww, "a-2", ws="w2")
    assert a["room_id"] != b["room_id"]


def test_a_repeat_join_is_idempotent_and_writes_nothing_new(ww):
    _, a = jw(ww, "a-1", label="L")
    n = len(ww.s._log)
    status, again = jw(ww, "a-1", label="renamed")
    assert status == 200 and again["room_id"] == a["room_id"] and again["agent_id"] == a["agent_id"]
    assert len(ww.s._log) == n
    assert ww.s._rooms[a["room_id"]]["name"] == "bender/L"


def test_the_workspace_label_is_display_only(ww):
    _, a = jw(ww, "a-1", label="same")
    _, b = jw(ww, "a-2", label="different")
    assert a["room_id"] == b["room_id"]                                              # the id is the key
    _, c = jw(ww, "a-3", ws="w9")
    assert ww.s._rooms[c["room_id"]]["name"] == "bender/w9"                          # no label: the id names it


def test_no_workspace_is_exactly_the_old_behaviour(ww):
    status, out = join(ww, name="plain")
    assert status == 201 and set(out) == {"agent_id", "name", "machine", "fabric_expires_at"}
    assert not ws_rooms(ww) and not [e for e in ww.s._log if e["event_type"] == "agent_granted"]
    assert ww.s.agent_rooms(agent_auth(ww, out["agent_id"]))["rooms"] == []


def test_workspace_given_while_the_feature_is_off_joins_without_a_room(aw):
    status, out = jw(aw, "a-1")
    assert status == 201 and "room_id" not in out and not ws_rooms(aw)
    assert ww_agent_unbound(aw, out["agent_id"])


def ww_agent_unbound(w, aid):
    return not w.s._agents[aid].get("workspace") and not w.s._rooms_by_agent.get(aid)


def test_an_already_joined_agent_gets_its_room_when_it_first_sends_a_workspace(ww):
    _, plain = join(ww, name="a-1")
    status, out = jw(ww, "a-1")
    assert status == 200 and out["agent_id"] == plain["agent_id"] and out["room_id"]
    assert ww.s._agents[plain["agent_id"]]["workspace"]["workspace_id"] == "w1"


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("bad", ["", " ", "a" * 65, "../x", "a/b", "has space", "x\n", "-lead", 5, None, ["a"], {"a": 1}])
def test_workspace_ids_are_validated(ww, bad):
    status, _ = join(ww, name="a-1", body={"workspace": {"id": bad}})
    assert status == 400
    assert not [a for a in ww.s._agents.values() if a.get("origin") == "fabric_join"] and not ws_rooms(ww)


@pytest.mark.parametrize("body", [{"workspace": "w1"}, {"workspace": []}, {"workspace": {}}, {"workspace": {"id": "w1", "x": 1}},
                                  {"workspace": {"id": "w1", "label": 5}}, {"workspace": {"id": "w1", "label": "x" * 65}},
                                  {"workspace": {"id": "w1", "label": "a\x00b"}}, {"workspace": {"id": "w1", "label": "a b"}},
                                  {"workspace": {"id": "w1"}, "tab_id": "bad id"}, {"workspace": {"id": "w1"}, "pane_id": 3},
                                  {"workspace": {"id": "w1"}, "tab_id": "t" * 65}, {"workspace": {"id": "w1"}, "extra": 1}])
def test_workspace_shapes_are_validated(ww, body):
    assert join(ww, name="a-1", body=body)[0] == 400
    assert not ws_rooms(ww)


def test_machine_in_the_workspace_object_is_not_accepted(ww):
    assert join(ww, name="a-1", body={"workspace": {"id": "w1", "machine": "beast"}})[0] == 400


# ------------------------------------------------------------------ the route
def test_the_route_exists_only_on_the_fabric_listener(ww):
    assert jw(ww, "a-1", fabric_listener=False)[0] == 404
    assert not ws_rooms(ww)


def test_over_real_http_on_the_fabric_listener(lw):
    set_ws(lw)
    status, out = hreq(lw, "POST", f"{BASE}/agents/fabric-join",
                       body={"name": "bender-claude", "workspace": {"id": "w1", "label": "ws"}, "tab_id": "t1", "pane_id": "p1"})
    assert status == 201 and out["room_id"] and out["workspace"]["id"] == "w1"
    status, again = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "second", "workspace": {"id": "w1"}})
    assert status == 201 and again["room_id"] == out["room_id"]
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "x-1", "workspace": {"id": "w1"}}, port=lw.public_port)
    assert status in (404, 401, 403)


# ------------------------------------------------------------------ caps (fail closed, 429, audit-throttled)
def test_rooms_per_machine_cap(ww):
    set_ws(ww, workspace_rooms_per_machine=2)
    assert jw(ww, "a-1", ws="w1")[0] == 201
    assert jw(ww, "a-2", ws="w2")[0] == 201
    n_agents = sum(1 for a in ww.s._agents.values() if a.get("origin") == "fabric_join")
    status, body = jw(ww, "a-3", ws="w3")
    assert status == 429 and "workspace" in body["error"]
    assert sum(1 for a in ww.s._agents.values() if a.get("origin") == "fabric_join") == n_agents     # no agent made either
    assert jw(ww, "a-4", ws="w1")[0] == 201                                                           # an existing room still takes agents
    ww.localapi["nodes"][BEAST] = f"beast.{SUFFIX}."
    assert jw(ww, "b-1", ws="w3", peer=BEAST)[0] == 201                                               # another machine has its own budget
    assert len(denied_ws(ww, "fabric_ws_machine_cap")) == 1


def test_total_rooms_cap(ww):
    set_ws(ww, workspace_rooms_total=2)
    ww.localapi["nodes"][BEAST] = f"beast.{SUFFIX}."
    assert jw(ww, "a-1", ws="w1")[0] == 201
    assert jw(ww, "b-1", ws="w1", peer=BEAST)[0] == 201
    assert jw(ww, "a-2", ws="w2")[0] == 429
    assert jw(ww, "b-2", ws="w2", peer=BEAST)[0] == 429
    assert jw(ww, "a-3", ws="w1")[0] == 201
    assert len(denied_ws(ww, "fabric_ws_total_cap")) == 2                                             # throttled per (machine, reason)
    assert len(ws_rooms(ww)) == 2


def test_members_per_room_cap(ww):
    set_ws(ww, workspace_room_members=2)
    assert jw(ww, "a-1")[0] == 201
    assert jw(ww, "a-2")[0] == 201
    n = len(ww.s._rooms)
    status, _ = jw(ww, "a-3")
    assert status == 429 and len(ww.s._rooms) == n
    assert len(denied_ws(ww, "fabric_ws_members_cap")) == 1
    aid = next(a for a, v in ww.s._agents.items() if v["name"] == "a-1")
    ww.s.revoke_agent(ww.alice, aid)                                                                 # a revoked agent frees its seat
    assert jw(ww, "a-3")[0] == 201


def test_cap_refusal_audits_are_throttled_not_per_name(ww):
    set_ws(ww, workspace_rooms_per_machine=1)
    jw(ww, "first", ws="w1")
    for i in range(30):
        assert jw(ww, f"loop-{i}", ws=f"x{i}")[0] == 429
    assert len(denied_ws(ww, "fabric_ws_machine_cap")) == 1


def test_a_lowered_cap_does_not_evict_existing_rooms(ww):
    _, a = jw(ww, "a-1")
    set_ws(ww, workspace_rooms_per_machine=1)
    set_ws(ww, workspace_rooms_per_machine=1, workspace_room_members=1)
    status, again = jw(ww, "a-1")
    assert status == 200 and again["room_id"] == a["room_id"]


# ------------------------------------------------------------------ restart / rebuild
def test_the_index_is_rebuilt_from_the_log_and_survives_a_restart(ww):
    _, a = jw(ww, "a-1", label="L", tab_id="t1")
    ww.s.revoke_agent(ww.alice, jw(ww, "a-r")[1]["agent_id"])
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=ww.s._now)
    fresh.hydrate(list(ww.s._log))
    assert fresh._fabric_ws_rooms == ww.s._fabric_ws_rooms and fresh._fabric_ws_rooms[("bender", "w1")] == a["room_id"]
    assert fresh._fabric_ws_project == ww.s._fabric_ws_project
    assert fresh._agents[a["agent_id"]]["workspace"] == ww.s._agents[a["agent_id"]]["workspace"]
    fresh.fabric_trust = normalize_fabric_trust(trust(auto_join=True, workspace_rooms=True))
    fresh.fabric_resolver = FabricPeerResolver(fresh.fabric_trust_cfg, whois=ww.s.fabric_resolver.whois)
    n_rooms = len(fresh._rooms)
    status, b = fresh.dispatch("POST", ["agents", "fabric-join"], {}, {"name": "after-restart", "workspace": {"id": "w1"}}, "", None,
                               peer_ip=BENDER, fabric_listener=True, fabric_owner=OWNER)
    assert status == 201 and b["room_id"] == a["room_id"] and len(fresh._rooms) == n_rooms         # same room, nothing new
    status, again = fresh.dispatch("POST", ["agents", "fabric-join"], {}, {"name": "a-1", "workspace": {"id": "w1"}}, "", None,
                                   peer_ip=BENDER, fabric_listener=True, fabric_owner=OWNER)
    assert status == 200 and again["agent_id"] == a["agent_id"]
    status, _ = fresh.dispatch("POST", ["agents", "fabric-join"], {}, {"name": "a-1", "workspace": {"id": "other"}}, "", None,
                               peer_ip=BENDER, fabric_listener=True, fabric_owner=OWNER)
    assert status == 409                                                                             # the binding survived too


# ------------------------------------------------------------------ revocation and workspace change
def test_revoking_the_agent_removes_its_room_access_and_it_stays_blocked(ww):
    _, a = jw(ww, "a-1")
    _, keep = jw(ww, "a-2")
    ww.s.revoke_agent(ww.alice, a["agent_id"])
    assert code(agent_auth, ww, a["agent_id"]) == 401
    assert ww.s.agent_rooms(ww.s._fabric_actor(a["agent_id"], OWNER, "bender"))["rooms"] == []
    status, _ = jw(ww, "a-1")
    assert status == 403
    assert ww.s.agent_rooms(agent_auth(ww, keep["agent_id"]))["rooms"][0]["room_id"] == a["room_id"]  # the peer is untouched


def test_clearing_the_fabric_bit_ends_access_too(ww):
    _, a = jw(ww, "a-1")
    ww.s.set_agent_fabric(ww.alice, a["agent_id"], {"fabric": False})
    assert code(agent_auth, ww, a["agent_id"]) == 401
    assert jw(ww, "a-1")[0] == 403


def test_a_rejoin_from_a_different_workspace_is_409_and_changes_nothing(ww):
    _, a = jw(ww, "a-1", ws="w1")
    n, rooms = len(ww.s._log), len(ww.s._rooms)
    status, body = jw(ww, "a-1", ws="w2")
    assert status == 409 and "workspace" in body["error"]
    assert len(ww.s._rooms) == rooms and ww.s._agents[a["agent_id"]]["workspace"]["workspace_id"] == "w1"
    assert set(ww.s._rooms_by_agent[a["agent_id"]]) == {a["room_id"]}
    assert [e for e in ww.s._log[n:] if e["event_type"] != "audit_denied"] == []
    assert len(denied_ws(ww, "fabric_join_workspace_changed")) == 1
    status, same = jw(ww, "a-1", ws="w1")
    assert status == 200 and same["room_id"] == a["room_id"]


def test_a_rejoin_without_a_workspace_changes_nothing(ww):
    _, a = jw(ww, "a-1")
    n = len(ww.s._log)
    status, out = join(ww, name="a-1")
    assert status == 200 and out["agent_id"] == a["agent_id"] and len(ww.s._log) == n


def test_an_owner_who_removed_the_grant_is_not_overridden_by_a_rejoin(ww):
    _, a = jw(ww, "a-1")
    ww.s.grant_agent(ww.alice, a["room_id"], {"agent_id": a["agent_id"], "actions": ["read"]})   # narrowed by the owner
    assert jw(ww, "a-1")[0] == 200
    assert ww.s._rooms[a["room_id"]]["grants"][a["agent_id"]]["actions"] == ["read"]


# ------------------------------------------------------------------ what the grant allows (peer assign evidence)
def test_peers_get_exactly_read_post_instruct_execute_and_never_coordinate_or_helpers(ww):
    _, a = jw(ww, "a-1")
    pa = agent_auth(ww, a["agent_id"])
    allowed = ww.s._effective(pa, ww.s._rooms[a["room_id"]])[0]
    assert sorted(allowed) == PEER_ACTIONS
    assert code(ww.s.grant_agent, pa, a["room_id"], {"agent_id": a["agent_id"], "actions": ["coordinate"]}) in (403, 404)


def test_peer_delegation_only_chains_from_a_person_task_without_coordinate(ww):
    """Evidence for the report: ``instruct`` + ``execute`` let a peer hand on a task a PERSON gave it; they do not let it start one."""
    _, a = jw(ww, "a-1")
    _, b = jw(ww, "a-2")
    pa, pb = agent_auth(ww, a["agent_id"]), agent_auth(ww, b["agent_id"])
    status = code(ww.s.create_task, pa, a["room_id"], {"target_agent_id": b["agent_id"], "text": "do it"})
    assert status == 400                                                                             # no parent: peers cannot originate
    task = ww.s.create_task(ww.alice, a["room_id"], {"target_agent_id": a["agent_id"], "text": "root job"})
    ww.s.update_task(pa, task["task_id"], {"status": "accepted"})
    sub = ww.s.create_task(pa, a["room_id"], {"target_agent_id": b["agent_id"], "text": "part", "parent_task_id": task["task_id"]})
    inbox = ww.s.task_inbox(pb)["tasks"]
    assert [t["task_id"] for t in inbox] == [sub["task_id"]]
    ww.s.update_task(pb, sub["task_id"], {"status": "accepted"})
    ww.s.update_task(pb, sub["task_id"], {"status": "done", "result": "ok"})


# ------------------------------------------------------------------ workspace rooms stay ORDINARY rooms (owner paths)
def token_agent(w, machine="somewhere-else", owner="alice"):
    return w.s.connect_agent(getattr(w, owner), {"harness_type": "claude-code", "machine": machine})


def test_owner_can_grant_a_token_enrolled_agent_on_any_machine_into_a_workspace_room(ww):
    """(a) the normal grant_agent path, an agent that never auto-joined and lives on another machine."""
    _, a = jw(ww, "a-1")
    coord = token_agent(ww, machine="mac-laptop")
    out = ww.s.grant_agent(ww.alice, a["room_id"], {"agent_id": coord["agent_id"], "actions": ["read", "post", "instruct"]})
    assert out["status"] != "denied"
    if out["status"] == "pending":
        ww.s.decide_agent_request(ww.alice, a["room_id"], out["request_id"], {"decision": "approve"})
    assert coord["agent_id"] in ww.s._rooms[a["room_id"]]["grants"]
    assert "workspace" not in ww.s._agents[coord["agent_id"]] or not ww.s._agents[coord["agent_id"]]["workspace"]


def test_members_guests_and_invites_work_as_in_any_room(ww):
    """(b) a person member through the ordinary add_member path sees what the agents post."""
    _, a = jw(ww, "a-1")
    ww.s.add_member(ww.alice, a["room_id"], {"user_id": "bob", "role": "contributor"})
    assert "bob" in ww.s._rooms[a["room_id"]]["members"]
    ww.s.post_message(agent_auth(ww, a["agent_id"]), a["room_id"], {"text": "for bob", "idempotency_key": "kb"})
    assert "for bob" in json.dumps(ww.s.read_events(ww.bob, a["room_id"], 0, 50)["events"])


def test_one_workspace_rule_binds_only_the_auto_join_path_not_owner_grants(ww):
    """(c) an agent granted by the owner into SEVERAL workspace rooms keeps all of them; its own auto-join still 409s on a change."""
    _, a = jw(ww, "a-1", ws="w1")
    _, b = jw(ww, "b-1", ws="w2")
    _, c = jw(ww, "c-1", ws="w3")
    assert len({a["room_id"], b["room_id"], c["room_id"]}) == 3
    coord = token_agent(ww)
    for r in (a, b, c):
        out = ww.s.grant_agent(ww.alice, r["room_id"], {"agent_id": coord["agent_id"], "actions": ["read", "post", "instruct"]})
        if out["status"] == "pending":
            ww.s.decide_agent_request(ww.alice, r["room_id"], out["request_id"], {"decision": "approve"})
    assert {a["room_id"], b["room_id"], c["room_id"]} <= set(ww.s._rooms_by_agent[coord["agent_id"]])
    # an auto-joined agent that the owner ALSO granted into a second workspace room keeps both, yet its own join cannot move it
    out = ww.s.grant_agent(ww.alice, b["room_id"], {"agent_id": a["agent_id"], "actions": ["read", "post"]})
    if out["status"] == "pending":
        ww.s.decide_agent_request(ww.alice, b["room_id"], out["request_id"], {"decision": "approve"})
    assert {a["room_id"], b["room_id"]} <= set(ww.s._rooms_by_agent[a["agent_id"]])
    assert jw(ww, "a-1", ws="w2")[0] == 409
    assert jw(ww, "a-1", ws="w1")[0] == 200
    assert {a["room_id"], b["room_id"]} <= set(ww.s._rooms_by_agent[a["agent_id"]])


def test_owner_changes_survive_later_auto_joins_and_a_restart(ww):
    """(d) extra members/grants and a narrowed grant persist across later auto-joins and rebuild."""
    _, a = jw(ww, "a-1")
    coord = token_agent(ww)
    out = ww.s.grant_agent(ww.alice, a["room_id"], {"agent_id": coord["agent_id"], "actions": ["read", "post"]})
    if out["status"] == "pending":
        ww.s.decide_agent_request(ww.alice, a["room_id"], out["request_id"], {"decision": "approve"})
    ww.s.add_member(ww.alice, a["room_id"], {"user_id": "bob", "role": "contributor"})
    ww.s.grant_agent(ww.alice, a["room_id"], {"agent_id": a["agent_id"], "actions": ["read", "post"]})   # narrowed
    _, b = jw(ww, "a-2")                                                                                   # a later auto-join
    assert jw(ww, "a-1")[0] == 200
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=ww.s._now)
    fresh.hydrate(list(ww.s._log))
    for s in (ww.s, fresh):
        room = s._rooms[a["room_id"]]
        assert "bob" in room["members"] and coord["agent_id"] in room["grants"]
        assert sorted(room["grants"][a["agent_id"]]["actions"]) == ["post", "read"]
        assert sorted(room["grants"][b["agent_id"]]["actions"]) == PEER_ACTIONS
    assert fresh._fabric_ws_rooms[("bender", "w1")] == a["room_id"]
