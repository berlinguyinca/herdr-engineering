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


def coordinator(w, owner="alice", team=TEAM, actions=("read", "post", "execute", "coordinate"), grant=True):
    """An agent (fabric bit on, machine bender) and, when ``grant``, the owner's ``coordinate`` grant for ``team``."""
    pr = getattr(w, owner)
    info = w.s.connect_agent(pr, {"harness_type": "claude-code", "machine": "bender"})
    w.s.set_agent_fabric(pr, info["agent_id"], {"fabric": True})
    if grant:
        out = w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": list(actions), "team_id": team})
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
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute", "coordinate"], "team_id": TEAM})
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


def test_role_not_on_the_allow_list_is_403(cw):
    w = cw
    info = coordinator(w)
    root = root_task(w, info)
    s, b = make(w, info, "credential", target_role="janitor", parent_task_id=root)
    assert s in (400, 403), b
    assert len([e for e in w.s._log if e["event_type"] == "task_created" and e["payload"].get("origin") == "coordinator"]) == 0


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
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post", "execute", "coordinate"], "team_id": TEAM})
    root = root_task(w, info)                                       # a re-grant starts afresh: the old root task predates it
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 201
    w.s.detach_team(w.alice, w.room, TEAM)
    pr = w.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert "coordinate" not in w.s.agent_rooms(pr)["rooms"][0]["actions"]
    w.s.attach_team(w.alice, w.room, TEAM, {"roles": {"implementer": [w.members[0]["agent_id"]]}})
    root = root_task(w, info)
    assert make(w, info, "credential", target_role="implementer", parent_task_id=root)[0] == 403   # not revived by re-attaching


def test_the_grant_survives_a_restart_from_the_log(cw):
    w = cw
    info = coordinator(w)
    events = [e for e in w.s._log if e["event_type"] == "agent_granted" and e["payload"]["agent_id"] == info["agent_id"]]
    assert events[-1]["payload"]["coordinate_team"] == TEAM
    fresh = CollabService(w.s.auth, now=w.s._now)
    fresh.hydrate(list(w.s._log))
    assert fresh._rooms[w.room]["grants"][info["agent_id"]]["coordinate_team"] == TEAM


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
