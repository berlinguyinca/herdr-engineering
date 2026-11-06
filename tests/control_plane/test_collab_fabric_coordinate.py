# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric trust slice 4: the ``coordinate`` agent action.

An agent the room OWNER granted ``coordinate`` (for one attached team) may create tasks BY ROLE inside that team, within the
existing coordinator limits, as a credential principal or as a Fabric principal alike. Real CollabService, real HTTP server, real
unix-socket whois fixture. No mocks."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import AGENT_ACTIONS, CollabError, CollabService
from herdr_engineering.control_plane.collab_coordinators import normalize_coordinators
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
from tests.control_plane.test_collab_tasks import code, svc  # noqa: F401

BENDER_NAME = "bender.tail1234.ts.net."
TEAM = "team-" + "c" * 20
TEAM2 = "team-" + "d" * 20
ROLES = {"enabled": True, "person_lease_seconds": 1000, "hard_expiry_seconds": 5000, "max_lease_extensions": 2}
COORD = {"enabled": True, "tasks_per_hour": 4, "max_open": 8}


def cred(info):
    return {"Authorization": f"Agent {info['credential']}", "X-Herdr-Device": info["device_id"]}


def hdr(info, how):
    return auth(info) if how == "fabric" else cred(info)


@pytest.fixture
def cw(http_world):
    """A world with fabric trust, team rooms, roles and coordinators ON, a team of two implementers and a reviewer attached."""
    w = http_world
    w.localapi["nodes"][w.lan] = BENDER_NAME
    w.s.team_rooms = normalize_team_rooms({"enabled": True})
    w.s.team_roles = normalize_team_roles(ROLES)
    w.s.coordinators = normalize_coordinators(COORD)
    w.s.work_graph_handle = None
    w.impls = [fabric_agent(w, actions=("read", "post", "execute")) for _ in range(2)]
    w.rev = fabric_agent(w, actions=("read", "post", "execute"))
    # the team brings its own grants; re-grant is refused for team agents, so enrol fresh agents for the team
    w.members = [w.s.connect_agent(w.alice, {"harness_type": "claude-code"}) for _ in range(2)]
    rev = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    out = w.s.attach_team(w.alice, w.room, TEAM, {"roles": {"implementer": [m["agent_id"] for m in w.members],
                                                            "reviewer": [rev["agent_id"]]}})
    assert out["team_id"] == TEAM
    return w


ALL_ROLES = ["implementer", "issue_writer", "reviewer", "tester"]


def coordinator(w, owner="alice", team=TEAM, actions=("read", "post", "execute", "coordinate"), grant=True, roles=None):
    """An agent (fabric bit on, machine bender) and, when ``grant``, the owner's ``coordinate`` grant for ``team`` with the role
    allow-list ``roles`` (every team role by default)."""
    pr = getattr(w, owner)
    info = w.s.connect_agent(pr, {"harness_type": "claude-code", "machine": "bender"})
    w.s.set_agent_fabric(pr, info["agent_id"], {"fabric": True})
    if grant:
        out = w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": list(actions), "team_id": team,
                                                   "roles": list(ALL_ROLES if roles is None else roles)})
        assert out["status"] == "granted", out
    return info


def root_task(w, info, how="credential", text="coordinate the benchmark"):
    out = w.s.create_task(w.alice, w.room, {"target_agent_id": info["agent_id"], "text": text})
    s, b = req(w, "POST", f"{BASE}/rooms/{w.room}/agent/tasks/{out['task_id']}/respond", headers=hdr(info, how),
               body={"status": "accepted"})
    assert s == 200, b
    return out["task_id"]


def make(w, info, how, **body):
    body.setdefault("text", "run job")
    return req(w, "POST", f"{BASE}/rooms/{w.room}/tasks", headers=hdr(info, how), body=body)


def denials(w, reason=None):
    return [e["payload"] for e in w.s._log if e["event_type"] == "audit_denied"
            and (reason is None or reason in e["payload"].get("reason", ""))]


# ------------------------------------------------------------------ the action and who may grant it
def test_coordinate_is_not_a_default_agent_action_and_not_in_any_ceiling(cw):
    w = cw
    assert "coordinate" not in AGENT_ACTIONS
    info = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    out = w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute", "instruct"]})
    assert "coordinate" not in out["actions"]
    pr = w.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert all("coordinate" not in r["actions"] for r in w.s.agent_rooms(pr)["rooms"])


def test_owner_grants_and_revokes_coordinate(cw):
    w = cw
    info = coordinator(w)
    pr = w.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert "coordinate" in w.s.agent_rooms(pr)["rooms"][0]["actions"]
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute"]})   # revoke by narrowing
    assert "coordinate" not in w.s.agent_rooms(pr)["rooms"][0]["actions"]
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute", "coordinate"], "team_id": TEAM,
                                       "roles": ALL_ROLES})
    assert "coordinate" in w.s.agent_rooms(pr)["rooms"][0]["actions"]
    w.s.remove_agent_from_room(w.alice, w.room, info["agent_id"])                                             # and by removal
    assert w.s.agent_rooms(pr) == {"rooms": []}


def test_coordinate_needs_an_attached_team_of_this_room(cw):
    w = cw
    info = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    base = {"agent_id": info["agent_id"], "actions": ["read", "coordinate"]}
    assert code(w.s.grant_agent, w.alice, w.room, base) == 400                       # no team named
    assert code(w.s.grant_agent, w.alice, w.room, {**base, "team_id": TEAM2}) == 404   # not attached here
    assert code(w.s.grant_agent, w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read"], "team_id": TEAM}) == 400


def test_a_contributor_cannot_grant_or_request_coordinate(cw):
    w = cw
    info = w.s.connect_agent(w.bob, {"harness_type": "claude-code"})
    body = {"agent_id": info["agent_id"], "actions": ["read", "coordinate"], "team_id": TEAM}
    assert code(w.s.grant_agent, w.bob, w.room, body) == 403
    assert w.s.list_agent_requests(w.alice, w.room)["requests"] == []               # not even a request
    assert denials(w, "coordinate")


def test_the_approval_of_a_request_cannot_add_coordinate(cw):
    w = cw
    info = w.s.connect_agent(w.bob, {"harness_type": "claude-code"})
    out = w.s.grant_agent(w.bob, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post"]})
    assert code(w.s.decide_agent_request, w.alice, w.room, out["request_id"],
                {"decision": "approve", "actions": ["read", "coordinate"]}) == 400


@pytest.mark.parametrize("how", ["fabric", "credential"])
def test_an_agent_cannot_grant_itself_or_another_agent_coordinate(cw, how):
    w = cw
    me, other = coordinator(w), coordinator(w, grant=False)
    plain = fabric_agent(w, actions=("read", "post", "execute", "instruct"))
    path = f"{BASE}/rooms/{w.room}/agents"
    body = {"agent_id": other["agent_id"], "actions": ["read", "coordinate"], "team_id": TEAM}
    for who in (me, plain):                                                           # a coordinator and an ordinary agent
        assert req(w, "POST", path, headers=hdr(who, how), body=body)[0] == 403
        assert req(w, "POST", path, headers=hdr(who, how), body={**body, "agent_id": who["agent_id"]})[0] == 403
    pr = w.s.authenticate(f"Agent {other['credential']}", other["device_id"])
    assert w.s.agent_rooms(pr) == {"rooms": []}


def test_a_team_agent_a_helper_and_an_external_agent_cannot_hold_coordinate(cw):
    w = cw
    team_agent = w.members[0]
    body = {"agent_id": team_agent["agent_id"], "actions": ["read", "coordinate"], "team_id": TEAM}
    assert code(w.s.grant_agent, w.alice, w.room, body) == 409                        # its access comes from the team
    ext = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    w.s._agents[ext["agent_id"]]["external"] = {"room_id": w.room, "hard_expiry": 1e18}
    assert code(w.s.grant_agent, w.alice, w.room, {**body, "agent_id": ext["agent_id"]}) == 403


# ------------------------------------------------------------------ creating by role
@pytest.mark.parametrize("how", ["fabric", "credential"])
def test_a_coordinator_creates_by_role_and_the_creator_reads_the_result(cw, how):
    w = cw
    info = coordinator(w)
    root = root_task(w, info, how)
    s, b = make(w, info, how, target_role="implementer", parent_task_id=root)
    assert s == 201, b
    tid, routed = b["task_id"], b["routed_to"]
    assert routed in {"agent:" + m["agent_id"] for m in w.members}
    created = [e for e in w.s._log if e["event_type"] == "task_created"][-1]["payload"]
    assert created["actor_agent_id"] == info["agent_id"] and created["origin"] == "coordinator" and created["coordinator_team"] == TEAM
    assert (created.get("via") == "fabric") == (how == "fabric")                       # the audit row says how it got in
    if how == "fabric":
        assert created["machine"] == "bender"
    # the assignee finishes it through the ordinary agent route
    assignee = next(m for m in w.members if "agent:" + m["agent_id"] == routed)
    h = cred(assignee)
    assert req(w, "POST", f"{BASE}/rooms/{w.room}/agent/tasks/{tid}/respond", headers=h, body={"status": "accepted"})[0] == 200
    s, b = req(w, "POST", f"{BASE}/rooms/{w.room}/agent/tasks/{tid}/respond", headers=h, body={"status": "done", "result": "42 ops/s"})
    assert s == 200, b
    # the creator reads it; so does the owner; nobody else's tasks show up
    s, b = req(w, "GET", f"{BASE}/rooms/{w.room}/agent/tasks/created", headers=hdr(info, how))
    assert s == 200 and b["untrusted"] is True
    row = next(t for t in b["tasks"] if t["task_id"] == tid)
    assert row["status"] == "done" and row["result"] == "42 ops/s"
    seen = next(t for t in w.s.list_tasks(w.alice, w.room)["tasks"] if t["task_id"] == tid)
    assert seen["status"] == "done" and seen["result"] == "42 ops/s"


def test_another_agents_created_tasks_are_not_readable(cw):
    w = cw
    a, b = coordinator(w), coordinator(w)
    root = root_task(w, a)
    assert make(w, a, "credential", target_role="implementer", parent_task_id=root)[0] == 201
    s, out = req(w, "GET", f"{BASE}/rooms/{w.room}/agent/tasks/created", headers=cred(b))
    assert s == 200 and out["tasks"] == []
    plain = fabric_agent(w)
    assert req(w, "GET", f"{BASE}/rooms/{w.room}/agent/tasks/created", headers=cred(plain))[0] == 404
    assert req(w, "GET", f"{BASE}/rooms/{w.room}/agent/tasks/created", headers=cred(coordinator(w, grant=False)))[0] == 404


def test_a_non_coordinator_agent_cannot_create_by_role(cw):
    w = cw
    info = fabric_agent(w, actions=("read", "post", "execute"))
    root = root_task(w, info)
    for how in ("fabric", "credential"):
        s, b = make(w, info, how, target_role="implementer", parent_task_id=root)
        assert s == 403, b
    assert not [e for e in w.s._log if e["event_type"] == "task_created" and e["payload"].get("actor_agent_id") == info["agent_id"]]


def test_without_the_flags_a_coordinate_grant_is_inert(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    w.s.coordinators = normalize_coordinators({"enabled": True, "kill_switch": True})
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 403
    w.s.coordinators = normalize_coordinators(COORD)
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 201


@pytest.mark.parametrize("how", ["fabric", "credential"])
def test_over_tasks_per_hour_is_429_with_an_audit_event(cw, how):
    w = cw
    info = coordinator(w)
    root = root_task(w, info, how)
    for i in range(COORD["tasks_per_hour"]):
        assert make(w, info, how, target_role="implementer", parent_task_id=root, text=f"job {i}")[0] == 201
    n = len(denials(w))
    s, b = make(w, info, how, target_role="implementer", parent_task_id=root, text="one too many")
    assert s == 429, b
    new = denials(w)[n:]
    assert new and new[-1]["action"] == "task.create" and "coordinator_rate" in new[-1]["reason"]
    assert new[-1]["actor_agent_id"] == info["agent_id"]
    assert (new[-1].get("via") == "fabric") == (how == "fabric")


def test_max_open_is_its_own_cap_with_an_audit_event(cw):
    w = cw
    w.s.coordinators = normalize_coordinators({"enabled": True, "tasks_per_hour": 50, "max_open": 2})
    info = coordinator(w)
    root = root_task(w, info)
    assert [make(w, info, "credential", target_role="implementer", parent_task_id=root, text=f"j{i}")[0] for i in range(2)] == [201, 201]
    s, b = make(w, info, "credential", target_role="implementer", parent_task_id=root, text="third")
    assert s == 429, b
    assert "coordinator_open" in denials(w)[-1]["reason"]


def test_cross_team_is_403_with_an_audit_event(cw):
    w = cw
    other = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    w.s.attach_team(w.alice, w.room, TEAM2, {"roles": {"tester": [other["agent_id"]]}})
    info = coordinator(w)
    root = root_task(w, info)
    s, b = make(w, info, "credential", target_role="tester", team_id=TEAM2, parent_task_id=root)
    assert s == 403, b
    assert "coordinator_other_team" in denials(w)[-1]["reason"]
    # it also cannot name a team it does not coordinate by leaving it out: the role must exist in ITS team
    s, b = make(w, info, "credential", target_role="tester", parent_task_id=root)
    assert s == 409, b


def test_a_role_not_on_the_coordinators_list_is_exactly_403_and_audited(cw):
    w = cw
    info = coordinator(w, roles=["implementer"])
    root = root_task(w, info)
    n = len(denials(w))
    s, b = make(w, info, "credential", target_role="reviewer", parent_task_id=root)         # a real team role, not on the list
    assert s == 403 and b["code"] == "coordinator_role_not_allowed", b
    assert "coordinator_role_not_allowed" in denials(w)[n:][-1]["reason"]
    assert not [e for e in w.s._log if e["event_type"] == "task_created" and e["payload"].get("origin") == "coordinator"]


def test_a_role_on_the_coordinators_list_is_201(cw):
    w = cw
    info = coordinator(w, roles=["implementer"])
    root = root_task(w, info)
    s, b = make(w, info, "credential", target_role="implementer", parent_task_id=root)
    assert s == 201, b


def test_the_role_list_is_what_the_view_reports(cw):
    w = cw
    info = coordinator(w, roles=["tester", "implementer"])
    pr = w.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert w.s.coordination_view(pr, w.room)["limits"]["roles"] == ["implementer", "tester"]


def test_a_coordinate_grant_needs_a_non_empty_list_of_real_team_roles(cw):
    w = cw
    info = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    base = {"agent_id": info["agent_id"], "actions": ["read", "coordinate"], "team_id": TEAM}
    assert code(w.s.grant_agent, w.alice, w.room, base) == 400                                  # no roles at all
    for bad in ([], "implementer", ["janitor"], ["implementer", 3], ["implementer", "implementer"], [None]):
        assert code(w.s.grant_agent, w.alice, w.room, {**base, "roles": bad}) == 400, bad
    assert code(w.s.grant_agent, w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read"], "roles": ["tester"]}) == 400
    assert w.s.grant_agent(w.alice, w.room, {**base, "roles": ["tester"]})["status"] == "granted"


def test_a_regrant_replaces_the_list_and_revoke_and_detach_clear_it(cw):
    w = cw
    info = coordinator(w, roles=["implementer"])
    aid = info["agent_id"]
    grant = lambda: w.s._rooms[w.room]["grants"][aid]                                           # noqa: E731
    assert grant()["coordinate_roles"] == ["implementer"]
    w.s.grant_agent(w.alice, w.room, {"agent_id": aid, "actions": ["read", "post", "execute", "coordinate"], "team_id": TEAM,
                                      "roles": ["reviewer", "tester"]})
    assert grant()["coordinate_roles"] == ["reviewer", "tester"]
    w.s.grant_agent(w.alice, w.room, {"agent_id": aid, "actions": ["read", "post", "execute"]})
    assert "coordinate_roles" not in grant() and "coordinate_team" not in grant()
    w.s.grant_agent(w.alice, w.room, {"agent_id": aid, "actions": ["read", "post", "execute", "coordinate"], "team_id": TEAM,
                                      "roles": ["tester"]})
    w.s.detach_team(w.alice, w.room, TEAM)
    assert "coordinate_roles" not in grant() and "coordinate_team" not in grant()


def test_lease_and_policy_are_rejected(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    for extra in ({"lease_seconds": 60}, {"no_response_policy": "escalate"}):
        s, b = make(w, info, "credential", target_role="implementer", parent_task_id=root, **extra)
        assert s == 400, b


def test_direct_assignment_to_an_agent_or_a_person_is_rejected(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    for extra in ({"target_agent_id": w.members[0]["agent_id"]}, {"target_user_id": "bob"}):
        s, b = make(w, info, "credential", parent_task_id=root, **extra)
        assert s in (400, 403), b
    s, b = make(w, info, "credential", parent_task_id=root)                              # no role at all
    assert s == 400, b


@pytest.mark.parametrize("how", ["fabric", "credential"])
def test_a_coordinator_cannot_administer_or_read_other_rooms(cw, how):
    w = cw
    info = coordinator(w)
    h = hdr(info, how)
    assert req(w, "POST", f"{BASE}/rooms/{w.room}/teams/{TEAM}/roles", headers=h, body={"role": "tester", "holder": "user:bob"})[0] == 403
    assert req(w, "POST", f"{BASE}/rooms/{w.room}/teams/{TEAM2}/attach", headers=h, body={"roles": {}})[0] == 403
    assert req(w, "POST", f"{BASE}/rooms/{w.room}/teams/{TEAM}/detach", headers=h, body={})[0] == 403
    assert req(w, "POST", f"{BASE}/rooms/{w.room}/teams/{TEAM}/coordinator", headers=h, body={"holder": "agent:" + info["agent_id"]})[0] == 403
    assert req(w, "GET", f"{BASE}/rooms/{w.room2}/agent/tasks/created", headers=h)[0] == 404
    assert req(w, "GET", f"{BASE}/rooms/{w.room2}/tasks", headers=h)[0] == 404


def test_revoking_coordinate_stops_creation_and_detaching_the_team_ends_it(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 201
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute"]})
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 403
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute", "coordinate"], "team_id": TEAM,
                                       "roles": ALL_ROLES})
    root = root_task(w, info)                                       # a re-grant starts afresh: the old root task predates it
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 201
    w.s.detach_team(w.alice, w.room, TEAM)
    pr = w.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert "coordinate" not in w.s.agent_rooms(pr)["rooms"][0]["actions"]
    w.s.attach_team(w.alice, w.room, TEAM, {"roles": {"implementer": [w.members[0]["agent_id"]]}})
    root = root_task(w, info)
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 403   # not revived by re-attaching


def restarted(w, rows=None):
    """A second service that only replays the durable log, with the same flags: nothing is carried over from the first."""
    fresh = CollabService(w.s.auth, now=w.s._now)
    fresh.team_rooms, fresh.team_roles, fresh.coordinators = w.s.team_rooms, w.s.team_roles, w.s.coordinators
    fresh.work_graph_handle = None
    fresh.fabric_trust, fresh.fabric_resolver = w.s.fabric_trust, w.s.fabric_resolver
    fresh.hydrate(list(w.s._log) if rows is None else rows)
    return fresh


def test_the_grant_and_its_roles_survive_a_real_restart_and_still_gate_creation(cw):
    w = cw
    info = coordinator(w, roles=["implementer"])
    root = root_task(w, info)
    events = [e for e in w.s._log if e["event_type"] == "agent_granted" and e["payload"]["agent_id"] == info["agent_id"]]
    assert events[-1]["payload"]["coordinate_team"] == TEAM and events[-1]["payload"]["coordinate_roles"] == ["implementer"]
    fresh = restarted(w)
    g = fresh._rooms[w.room]["grants"][info["agent_id"]]
    assert g["coordinate_team"] == TEAM and g["coordinate_roles"] == ["implementer"]
    pr = fresh.authenticate(f"Agent {info['credential']}", info["device_id"])
    with pytest.raises(CollabError) as e:                                                      # off the list, after replay
        fresh.create_task(pr, w.room, {"target_role": "reviewer", "text": "x", "parent_task_id": root})
    assert e.value.status == 403
    assert fresh.create_task(pr, w.room, {"target_role": "implementer", "text": "x", "parent_task_id": root})["routed_to"]


def test_an_old_grant_event_without_roles_replays_as_an_empty_list_and_fails_closed(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    rows = []
    for e in w.s._log:
        e = {**e, "payload": dict(e["payload"])}
        if e["event_type"] == "agent_granted":
            e["payload"].pop("coordinate_roles", None)                                         # as written before the roles existed
        rows.append(e)
    fresh = restarted(w, rows)
    assert fresh._rooms[w.room]["grants"][info["agent_id"]]["coordinate_roles"] == []
    pr = fresh.authenticate(f"Agent {info['credential']}", info["device_id"])
    for role in ALL_ROLES:
        with pytest.raises(CollabError) as e:
            fresh.create_task(pr, w.room, {"target_role": role, "text": "x", "parent_task_id": root})
        assert e.value.status == 403


def test_revoking_coordinate_makes_the_sweep_cancel_the_coordinators_open_tasks(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    made = [make(w, info, "credential", target_role="implementer", parent_task_id=root, text=f"j{i}")[1]["task_id"] for i in range(2)]
    assert w.s.sweep_coordinators()["cancelled"] == 0                                           # still authorised: nothing to do
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute"]})   # revoke
    out = w.s.sweep_coordinators()
    assert out["cancelled"] == 2
    assert {w.s._task_status(w.s._tasks[t]) for t in made} == {"cancelled"}
    assert w.s.sweep_coordinators()["cancelled"] == 0                                           # idempotent


def test_a_helper_cannot_be_granted_coordinate_and_does_not_list_it(cw):
    w = cw
    w.mallory.admin = True
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"]})
    parent = coordinator(w, actions=("read", "post", "spawn_helpers", "coordinate"))
    ppr = w.s.authenticate(f"Agent {parent['credential']}", parent["device_id"])
    h = w.s.request_helper(ppr, {"room_id": w.room, "role": "reviewer", "task": "look at it"})
    c = w.s.claim_helper(w.mallory, h["helper_id"])
    granted = [e for e in w.s._log if e["event_type"] == "agent_granted" and e["payload"]["agent_id"] == c["helper_agent_id"]]
    assert granted and "coordinate" not in granted[-1]["payload"]["actions"]
    assert code(w.s.grant_agent, w.alice, w.room, {"agent_id": c["helper_agent_id"], "actions": ["read", "coordinate"], "team_id": TEAM,
                                                   "roles": ["tester"]}) == 400


def test_granted_coordinator_refusals_are_audited_under_its_own_identity(cw):
    w = cw
    info = coordinator(w)
    n = len(denials(w))
    s, _ = req(w, "POST", f"{BASE}/rooms/{w.room}/agents", headers=cred(info),
               body={"agent_id": info["agent_id"], "actions": ["read"]})
    assert s == 403
    new = denials(w)[n:]
    assert new and new[-1]["actor_agent_id"] == info["agent_id"] and "agent_principal" in new[-1]["reason"]


def test_a_granted_coordinator_counts_as_an_assistant_in_the_chain_guards(cw):
    w = cw
    # asst is a designated coordinator in room 2 (assistant-kind) and an ordinary instructing agent in room 1
    asst = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    team3 = "team-" + "e" * 20
    w.s.attach_team(w.alice, w.room2, team3, {"roles": {"implementer": [w.s.connect_agent(w.alice, {})["agent_id"]]}})
    w.s.set_team_coordinator(w.alice, w.room2, team3, {"holder": "agent:" + asst["agent_id"]})
    assert w.s.grant_agent(w.alice, w.room, {"agent_id": asst["agent_id"], "actions": ["read", "post", "execute", "instruct"]})["status"] == "granted"
    g = coordinator(w)
    root = w.s.create_task(w.alice, w.room, {"target_agent_id": asst["agent_id"], "text": "start"})["task_id"]
    assert req(w, "POST", f"{BASE}/rooms/{w.room}/agent/tasks/{root}/respond", headers=cred(asst), body={"status": "accepted"})[0] == 200
    s, b = make(w, asst, "credential", target_agent_id=g["agent_id"], parent_task_id=root)
    assert s == 400 and b["code"] == "coordination_loop", b
    assert w.s._tc_is_coordinator_agent(g["agent_id"])
    w.s.grant_agent(w.alice, w.room, {"agent_id": g["agent_id"], "actions": ["read", "post", "execute"]})   # once revoked, it is plain
    assert not w.s._tc_is_coordinator_agent(g["agent_id"])


# ------------------------------------------------------------------ regressions
def test_the_instruct_path_and_human_routes_are_unchanged(cw):
    w = cw
    boss = fabric_agent(w, actions=("read", "post", "execute", "instruct"))
    worker = fabric_agent(w, actions=("read", "post", "execute"))
    root = root_task(w, boss)
    s, b = make(w, boss, "credential", target_agent_id=worker["agent_id"], parent_task_id=root)
    assert s == 201 and "origin" not in [e for e in w.s._log if e["event_type"] == "task_created"][-1]["payload"], b
    out = w.s.create_task(w.alice, w.room, {"target_agent_id": worker["agent_id"], "text": "human task"})   # a person, by key
    assert out["status"] == "pending"
    assert w.s.create_task(w.alice, w.room, {"target_role": "implementer", "text": "by role"})["routed_to"].startswith("agent:")
    with pytest.raises(CollabError) as e:                                          # a human never needs, and cannot use, coordinate
        w.s.grant_agent(w.carol, w.room, {"agent_id": worker["agent_id"], "actions": ["read"]})
    assert e.value.status in (403, 404)
