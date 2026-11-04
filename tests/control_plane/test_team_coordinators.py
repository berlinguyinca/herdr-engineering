# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Slice 3 of docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md: the coordinator role, with limits.

Real ``CollabService`` with its file event log and real HTTP through ``ControlPlaneServer``. Nothing is mocked. The only thing
a test moves is the service's clock. Most tests need no Work Graph (a shape-valid team id attaches unverified, as the
collab-only process does); the projection test uses the real Postgres fixture."""
from __future__ import annotations

import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import (
    REVOCABLE_CREATE,
    SAFETY_EVENTS,
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.collab_coordinators import normalize_coordinators
from herdr_engineering.control_plane.collab_team_roles import normalize_team_roles
from herdr_engineering.control_plane.collab_team_rooms import normalize_team_rooms
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import BASE, Api, Clock, _digest
from tests.control_plane.test_team_rooms import USERS

TEAM = "team-" + "c" * 20
TEAM2 = "team-" + "d" * 20
ROLES = {"enabled": True, "person_lease_seconds": 1000, "hard_expiry_seconds": 5000, "max_lease_extensions": 2}
# A deliberately low cap so the cap is what refuses the next task (TASK_RATE is 20 per 10 minutes and MAX_OPEN_TASKS_PER_AGENT
# is 10, so the defaults would trip those first and hide a missing cap).
COORD = {"enabled": True, "tasks_per_hour": 4, "max_open": 8,
         "external": {"enabled": True, "tasks_per_hour": 2, "max_open": 3, "max_depth": 2, "max_items_per_request": 3}}


def _service(tmp_path, clock, coord=None, name="log"):
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                        sink=FileEventLog(str(tmp_path / name)), now=clock, max_agents_per_user=20)
    svc.team_rooms = normalize_team_rooms({"enabled": True})
    svc.team_roles = normalize_team_roles(ROLES)
    svc.coordinators = normalize_coordinators(COORD if coord is None else coord)
    return svc


class CPlane:
    """alice owns room r (and r2); bob is a contributor, carol a viewer, dave and erin are guests, mallory is in no room."""

    def __init__(self, svc, srv, clock):
        self.svc, self.srv, self.clock = svc, srv, clock
        self.api = Api(srv.port)
        s, out = self.c("alice", "POST", f"{BASE}/projects", {"name": "p"})
        assert s == 201, out
        self.project = out["project_id"]
        self.room = self.make_room("r")
        self.room2 = self.make_room("r2")
        for u, role in (("bob", "contributor"), ("carol", "viewer"), ("dave", "guest"), ("erin", "guest")):
            s, out = self.c("alice", "POST", f"{BASE}/rooms/{self.room}/members", {"user_id": u, "role": role})
            assert s == 201, out

    # -- plumbing
    def c(self, user, method, path, body=None):
        return self.api.call(method, path, body, raw_token=f"Bearer {USERS[user]}")

    def a(self, agent, method, path, body=None):
        return self.api.call(method, path, body, agent=agent)

    def make_room(self, name):
        s, out = self.c("alice", "POST", f"{BASE}/projects/{self.project}/rooms", {"name": name})
        assert s == 201, out
        return out["room_id"]

    def agent(self, owner="alice", name=None):
        s, out = self.c(owner, "POST", f"{BASE}/agents/connect", {"harness_type": "claude-code", **({"name": name} if name else {})})
        assert s == 201, out
        return out

    def events(self, kind):
        return [e for e in self.svc._log if e["event_type"] == kind]

    def denials(self, action=None, **match):
        return [e for e in self.events("audit_denied")
                if (action is None or e["payload"]["action"] == action) and all(e["payload"].get(k) == v for k, v in match.items())]

    # -- teams, rooms, coordinators
    def attach(self, roles, team=TEAM, room=None):
        return self.c("alice", "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/attach", {"roles": roles})

    def detach(self, team=TEAM, room=None):
        return self.c("alice", "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/detach", {})

    def set_coord(self, holder, team=TEAM, user="alice", room=None):
        return self.c(user, "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/coordinator", {"holder": holder})

    def clear_coord(self, team=TEAM, user="alice", room=None):
        return self.c(user, "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/coordinator/clear", {})

    def view_coord(self, team=TEAM, user="alice", room=None):
        return self.c(user, "GET", f"{BASE}/rooms/{room or self.room}/teams/{team}/coordinator")

    def ext(self, enabled, limits=None, team=TEAM, user="alice", room=None):
        body = {"enabled": enabled, **({"limits": limits} if limits is not None else {})}
        return self.c(user, "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/external-coordination", body)

    # -- tasks
    def make(self, who, **body):
        body.setdefault("text", "do the thing")
        if isinstance(who, dict):
            return self.a(who, "POST", f"{BASE}/rooms/{body.pop('room', self.room)}/tasks", body)
        return self.c(who, "POST", f"{BASE}/rooms/{body.pop('room', self.room)}/tasks", body)

    def inbox(self, agent):
        return self.a(agent, "GET", f"{BASE}/tasks/inbox")[1]["tasks"]

    def update(self, agent, task_id, status, **extra):
        return self.a(agent, "POST", f"{BASE}/tasks/{task_id}/update", {"status": status, **extra})

    def accept(self, agent, task_id):
        s, out = self.update(agent, task_id, "accepted")
        assert s == 200, out
        return out

    def respond(self, user, task_id, status, result=None):
        body = {"status": status, **({"result": result} if result else {})}
        return self.c(user, "POST", f"{BASE}/rooms/{self.room}/tasks/{task_id}/respond", body)

    def task(self, user, task_id):
        return self.c(user, "GET", f"{BASE}/rooms/{self.room}/tasks/{task_id}")

    def grant(self, agent, actions, room=None):
        return self.c("alice", "POST", f"{BASE}/rooms/{room or self.room}/agents", {"agent_id": agent["agent_id"], "actions": actions})

    def coordination(self, who):
        path = f"{BASE}/rooms/{self.room}/coordination"
        return self.a(who, "GET", path) if isinstance(who, dict) else self.c(who, "GET", path)


def _serve(svc):
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc)
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    svc.work_graph_handle = None          # the collab-only process has no Work Graph: a shape-valid team id attaches unverified
    return srv


@pytest.fixture
def cp(tmp_path):
    clock = Clock()
    svc = _service(tmp_path, clock)
    srv = _serve(svc)
    try:
        yield CPlane(svc, srv, clock)
    finally:
        srv.shutdown()


def crew(p, impls=2):
    """A team attached to the room: ``impls`` implementer agents, one reviewer, and an operator-owned coordinator agent."""
    held = [p.agent(name=f"impl{i}") for i in range(impls)]
    rev = p.agent(name="rev")
    s, out = p.attach({"implementer": [a["agent_id"] for a in held], "reviewer": [rev["agent_id"]]})
    assert s == 200, out
    coord = p.agent(name="coordy")
    s, out = p.set_coord("agent:" + coord["agent_id"])
    assert s == 200, out
    return held, rev, coord


def root_for(p, coord, text="build the thing"):
    """The operator's own root task for the coordinator, accepted by it (design contradiction 9)."""
    s, out = p.make("alice", target_agent_id=coord["agent_id"], text=text)
    assert s == 201, out
    p.accept(coord, out["task_id"])
    return out["task_id"]


# ============================================================== flags: absent means off and nothing changes
def test_config_is_strict_and_the_external_cap_is_lower_than_the_operator_owned_cap():
    cfg = normalize_coordinators(None)
    assert cfg["enabled"] is False and cfg["external"]["enabled"] is False
    assert cfg["tasks_per_hour"] == 20 and cfg["external"]["tasks_per_hour"] == 5
    with pytest.raises(ValueError):
        normalize_coordinators({"external": {"tasks_per_hour": 20}})
    with pytest.raises(ValueError):
        normalize_coordinators({"tasks_per_hour": 3, "external": {"tasks_per_hour": 3}})
    assert normalize_coordinators({"tasks_per_hour": 2})["external"]["tasks_per_hour"] == 1       # the default follows the parent


def test_with_the_flags_off_every_new_route_is_a_404_and_writes_nothing(cp):
    p = cp
    held, rev, coord = crew(p)
    n = len(p.svc._log)
    for flags in ({"enabled": False}, {"enabled": True, "kill_switch": True}, None):
        p.svc.coordinators = normalize_coordinators(flags)
        for method, path, body in (
                ("POST", f"/rooms/{p.room}/teams/{TEAM}/coordinator", {"holder": "agent:" + coord["agent_id"]}),
                ("GET", f"/rooms/{p.room}/teams/{TEAM}/coordinator", None),
                ("POST", f"/rooms/{p.room}/teams/{TEAM}/external-coordination", {"enabled": True, "limits": {"roles": ["tester"]}}),
                ("GET", f"/rooms/{p.room}/coordination", None)):
            assert p.c("alice", method, BASE + path, body)[0] == 404, (flags, method, path)
    assert len(p.svc._log) == n                                               # not even an audit event
    # the slice 2 behaviour is exactly as before: an agent with `instruct` may hand a task to any agent that can execute
    p.svc.coordinators = normalize_coordinators(None)
    other = p.agent(name="other")
    assert p.grant(other, ["read", "post", "execute", "instruct"])[0] == 201
    root = p.make("alice", target_agent_id=other["agent_id"])[1]["task_id"]
    p.accept(other, root)
    s, out = p.make(other, target_agent_id=held[0]["agent_id"], parent_task_id=root)
    assert s == 201 and "origin" not in p.events("task_created")[-1]["payload"], out


def test_second_flag_off_means_no_external_coordination_while_the_operator_owned_one_works(cp):
    p = cp
    held, rev, coord = crew(p)
    p.svc.coordinators = normalize_coordinators({"enabled": True, "tasks_per_hour": 4,
                                                 "external": {"enabled": False, "tasks_per_hour": 2}})
    assert p.ext(True, {"roles": ["implementer"]})[0] == 404 and not p.events("team_external_coordination_set")
    assert p.set_coord("user:dave")[0] in (404, 409) and not p.events("team_coordinator_set")[1:]
    root = root_for(p, coord)
    assert p.make(coord, target_role="implementer", parent_task_id=root)[0] == 201


def test_events_are_classified_on_purpose():
    assert "team_coordinator_cleared" in SAFETY_EVENTS and "team_coordinator_set" not in SAFETY_EVENTS
    assert REVOCABLE_CREATE["team_coordinator_set"] == "teamcoord"
    assert REVOCABLE_CREATE["team_external_coordination_set"] == "teamext"
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    assert svc._is_safety("team_external_coordination_set", {"enabled": False}) is True      # only the revocation is a safety write
    assert svc._is_safety("team_external_coordination_set", {"enabled": True}) is False
    assert svc._creation_cost("team_external_coordination_set", {"enabled": True}) == 1
    assert svc._creation_cost("team_external_coordination_set", {"enabled": False}) == 0


# ============================================================== (a) an operator-owned coordinator
def test_the_owner_designates_an_operator_owned_agent_and_it_gets_exactly_the_coordinator_actions(cp):
    p = cp
    held, rev, coord = crew(p)
    s, view = p.view_coord()
    assert s == 200 and view["holder"] == "agent:" + coord["agent_id"] and view["kind"] == "operator"
    assert view["external"]["enabled"] is False
    pr = p.svc.authenticate(f"Agent {coord['credential']}", coord["device_id"])
    assert {r["room_id"]: sorted(r["actions"]) for r in p.svc.agent_rooms(pr)["rooms"]} == {
        p.room: ["execute", "instruct", "post", "read"]}
    assert p.set_coord("agent:" + coord["agent_id"])[1]["replayed"] is True
    assert len(p.events("team_coordinator_set")) == 1


def test_who_can_and_cannot_be_designated(cp):
    p = cp
    held, rev, _ = crew(p)
    assert p.attach({"implementer": [p.agent(name="w")["agent_id"]]}, team=TEAM2)[0] == 200      # a second team, no coordinator yet
    mine = p.agent(name="mine")
    bobs = p.agent("bob", name="bobs")
    inv = p.svc.create_external_invite(p.svc.authenticate(f"Bearer {USERS['alice']}", None), p.room, {"label": "foreign"})
    foreign = p.svc.redeem_join_token({"token": inv["code"]})
    n = len(p.events("team_coordinator_set"))
    assert p.set_coord("agent:" + bobs["agent_id"], team=TEAM2)[0] == 404                       # not the operator's agent
    assert p.set_coord("agent:" + foreign["agent_id"], team=TEAM2)[0] == 400                    # a foreign agent
    assert p.set_coord("agent:agent-nope", team=TEAM2)[0] == 404
    assert p.set_coord("user:alice", team=TEAM2)[0] == 400                                      # the operator's own user
    s, out = p.set_coord("user:dave", team=TEAM2)                                               # an external person, no opt-in
    assert s == 409 and out["code"] == "external_opt_in_required", out
    assert p.set_coord("agent:" + held[0]["agent_id"], team=TEAM2)[0] == 409                    # already holds access in this room
    assert p.set_coord("agent:" + mine["agent_id"], team="team-" + "9" * 20)[0] == 404          # that team is not attached here
    assert p.set_coord("agent:" + mine["agent_id"], team="not-a-team")[0] == 404
    assert len(p.events("team_coordinator_set")) == n
    # one coordinator per team, and one team per coordinator per room
    assert p.set_coord("agent:" + mine["agent_id"])[0] == 409
    s, out = p.set_coord("agent:" + p.view_coord()[1]["holder"][6:], team=TEAM2)
    assert s == 409 and out["code"] == "coordinator_elsewhere", out
    assert p.set_coord("agent:" + mine["agent_id"], team=TEAM2)[0] == 200


def test_only_the_owner_user_designates_and_an_agent_gets_403_with_an_audit_denial(cp):
    p = cp
    held, rev, coord = crew(p)
    other = p.agent(name="other")
    for user in ("bob", "carol", "dave", "mallory"):
        assert p.set_coord("agent:" + other["agent_id"], user=user)[0] == 404, user
        assert p.clear_coord(user=user)[0] == 404, user
        assert p.ext(True, {"roles": ["implementer"]}, user=user)[0] == 404, user
    assert p.a(coord, "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM}/coordinator", {"holder": "agent:" + other["agent_id"]})[0] == 403
    assert p.a(coord, "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM}/coordinator/clear", {})[0] == 403
    assert p.a(coord, "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM}/external-coordination", {"enabled": True})[0] == 403
    assert len(p.denials(actor_agent_id=coord["agent_id"])) >= 3
    assert len(p.events("team_coordinator_set")) == 1


def test_the_coordinator_decomposes_an_operator_approved_root_task_by_role_and_cannot_make_a_root_task(cp):
    p = cp
    held, rev, coord = crew(p)
    # no root task: refused, whatever the target
    assert p.make(coord, target_role="implementer")[0] == 400
    assert p.make(coord, target_role="implementer", parent_task_id="nope")[0] == 400
    root = p.make("alice", target_agent_id=coord["agent_id"], text="build the thing")[1]["task_id"]
    assert p.make(coord, target_role="implementer", parent_task_id=root)[0] == 400             # not accepted yet
    assert [t["task_id"] for t in p.inbox(coord)] == [root]
    p.accept(coord, root)
    s, out = p.make(coord, target_role="implementer", parent_task_id=root, text="part one")
    assert s == 201 and out["routed_to"].startswith("agent:") and out["routed_to"][6:] in {a["agent_id"] for a in held}, out
    pl = p.events("task_created")[-1]["payload"]
    assert pl["origin"] == "coordinator" and pl["created_by_id"] == coord["agent_id"] and pl["parent_task_id"] == root
    assert pl["depth"] == 1 and pl["root_user_id"] == "alice"
    # the assignee sees it in its inbox, accepts it, finishes it: the chain really runs end to end
    holder = next(a for a in held if a["agent_id"] == out["routed_to"][6:])
    assert [t["task_id"] for t in p.inbox(holder)] == [out["task_id"]]
    p.accept(holder, out["task_id"])
    assert p.update(holder, out["task_id"], "done", result="ok")[0] == 200
    # tracking: the coordinator reads its own chain
    s, mine = p.coordination(coord)
    assert s == 200 and mine["kind"] == "operator" and {t["task_id"] for t in mine["tasks"]} == {out["task_id"]}
    assert mine["tasks"][0]["status"] == "done" and mine["usage"]["created_last_hour"] == 1


def test_a_coordinator_assigns_only_by_role_to_its_own_teams_holders(cp):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    stranger = p.agent(name="stranger")
    assert p.grant(stranger, ["read", "post", "execute"])[0] == 201                          # in the room, but not in the team
    # naming an agent, a person, or a role nobody holds, or another team: all refused
    for body in ({"target_agent_id": held[0]["agent_id"]}, {"target_agent_id": stranger["agent_id"]},
                 {"target_user_id": "bob"}, {"target_role": "tester"}, {"target_role": "implementer", "team_id": TEAM2},
                 {"target_role": "coordinator"}):
        s, out = p.make(coord, parent_task_id=root, **body)
        assert s in (400, 403, 404, 409), (body, out)
    assert len(p.events("task_created")) == 1                                                  # only the root exists


def test_the_per_hour_cap_refuses_the_next_task_survives_a_restart_and_resets_after_an_hour(cp, tmp_path):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    for i in range(4):
        assert p.make(coord, target_role="implementer", parent_task_id=root, text=f"item {i}")[0] == 201
        p.clock.t += 700                                    # spread over the hour: TASK_RATE (per 10 minutes) never trips
    # 4 in 2800 s: the fifth is the cap's doing, with its own code
    s, out = p.make(coord, target_role="implementer", parent_task_id=root, text="item 4")
    assert s == 429 and out["code"] == "coordinator_rate", out
    # the cap is counted from the replayed log, so a restart does not reset it
    svc2 = _service(tmp_path, p.clock, name="reborn")
    svc2.hydrate(p.svc._log)
    pr = svc2.authenticate(f"Agent {coord['credential']}", coord["device_id"])
    from herdr_engineering.control_plane.collab import CollabError
    with pytest.raises(CollabError) as ei:
        svc2.create_task(pr, p.room, {"target_role": "implementer", "parent_task_id": root, "text": "item 5"})
    assert ei.value.status == 429 and ei.value.code == "coordinator_rate"
    p.clock.t += 3600
    assert p.make(coord, target_role="implementer", parent_task_id=root, text="next hour")[0] == 201


def test_the_open_task_cap_is_separate_from_the_hourly_cap(cp):
    p = cp
    p.svc.coordinators = normalize_coordinators({**COORD, "tasks_per_hour": 50, "max_open": 2,
                                                 "external": {"enabled": True, "tasks_per_hour": 2, "max_open": 2}})
    held, rev, coord = crew(p, impls=3)
    root = root_for(p, coord)
    assert p.make(coord, target_role="implementer", parent_task_id=root, text="one")[0] == 201
    assert p.make(coord, target_role="implementer", parent_task_id=root, text="two")[0] == 201
    s, out = p.make(coord, target_role="implementer", parent_task_id=root, text="three")
    assert s == 429 and out["code"] == "coordinator_open", out


def test_nothing_an_agent_cannot_do_is_open_to_the_coordinator_each_denial_is_audited(cp):
    p = cp
    held, rev, coord = crew(p)
    other = p.agent(name="other")
    spare = p.agent(name="spare")
    tries = {
        "grant": ("POST", f"/rooms/{p.room}/agents", {"agent_id": coord["agent_id"], "actions": ["read", "post", "invite"]}),
        "invite": ("POST", f"/rooms/{p.room}/invites", {"email": "x@example.com", "role": "guest"}),
        "add_member": ("POST", f"/rooms/{p.room}/members", {"user_id": "mallory", "role": "contributor"}),
        "role_assign": ("POST", f"/rooms/{p.room}/teams/{TEAM}/roles", {"role": "tester", "holder": "user:dave"}),
        "coordinator_change": ("POST", f"/rooms/{p.room}/teams/{TEAM}/coordinator", {"holder": "agent:" + other["agent_id"]}),
        "coordinator_clear": ("POST", f"/rooms/{p.room}/teams/{TEAM}/coordinator/clear", {}),
        "guest_level": ("POST", f"/rooms/{p.room}/guest-level", {"level": "read_only"}),
        "attach": ("POST", f"/rooms/{p.room}/teams/{TEAM2}/attach", {"roles": {"implementer": [spare["agent_id"]]}}),
        "detach": ("POST", f"/rooms/{p.room}/teams/{TEAM}/detach", {}),
        "optin": ("POST", f"/rooms/{p.room}/teams/{TEAM}/external-coordination", {"enabled": True}),
    }
    for name, (method, path, body) in tries.items():
        before = len(p.denials(actor_agent_id=coord["agent_id"]))
        p.clock.t += 61                                      # the audit trail records a repeated denial once a minute
        s, _ = p.a(coord, method, BASE + path, body)
        assert s == 403, (name, s)
        assert len(p.denials(actor_agent_id=coord["agent_id"])) == before + 1, name
    # and nothing changed
    assert p.view_coord()[1]["holder"] == "agent:" + coord["agent_id"] and not p.events("team_detached")
    assert p.c("alice", "GET", f"{BASE}/rooms/{p.room}/guest-level")[1]["explicit"] is False
    assert not p.events("team_role_assigned") and not p.events("member_added")[4:]


def _assistants(p, count, plain_grants=("read", "post", "execute", "instruct")):
    """Assistant-kind agents: designated coordinators of teams in room 2. In room 1 they are plain agents with ``plain_grants``."""
    out = []
    for i in range(count):
        team = "team-" + f"{i + 1:x}" * 20
        a = p.agent(name=f"asst{i}")
        assert p.attach({"implementer": [p.agent(name=f"w{i}")["agent_id"]]}, team=team, room=p.room2)[0] == 200
        assert p.set_coord("agent:" + a["agent_id"], team=team, room=p.room2)[0] == 200
        if plain_grants:
            assert p.grant(a, list(plain_grants))[0] == 201
        out.append(a)
    return out


def test_no_assistant_triggers_an_assistant_even_through_a_third_agent(cp):
    p = cp
    asst_a, asst_b = _assistants(p, 2)
    mid = p.agent(name="mid")
    assert p.grant(mid, ["read", "post", "execute", "instruct"])[0] == 201
    root = p.make("alice", target_agent_id=asst_a["agent_id"], text="start")[1]["task_id"]
    p.accept(asst_a, root)
    # (1) an assistant hands a task straight to another assistant: refused, with its own code
    s, out = p.make(asst_a, target_agent_id=asst_b["agent_id"], parent_task_id=root)
    assert s == 400 and out["code"] == "coordination_loop", out
    # (2) through a third agent: assistant A -> B (plain) -> assistant: the chain already holds an assistant
    t1 = p.make(asst_a, target_agent_id=mid["agent_id"], parent_task_id=root)[1]["task_id"]
    p.accept(mid, t1)
    s, out = p.make(mid, target_agent_id=asst_b["agent_id"], parent_task_id=t1)
    assert s == 400 and out["code"] == "coordination_loop", out
    # (3) the rule follows the chain, not the agent: a person's chain through the same agents may reach an assistant
    root2 = p.make("alice", target_agent_id=mid["agent_id"], text="start again")[1]["task_id"]
    p.accept(mid, root2)
    assert p.make(mid, target_agent_id=asst_b["agent_id"], parent_task_id=root2)[0] == 201
    # (4) with the flag off none of this applies (slice 2 behaviour)
    p.svc.coordinators = normalize_coordinators(None)
    assert p.make(mid, target_agent_id=asst_b["agent_id"], parent_task_id=t1)[0] == 201


def test_a_coordinator_cannot_be_routed_to_an_assistant_that_holds_a_role_in_its_own_team(cp):
    p = cp
    (asst,) = _assistants(p, 1, plain_grants=())
    impl = p.agent(name="impl")
    assert p.attach({"implementer": [impl["agent_id"]], "tester": [asst["agent_id"]]})[0] == 200
    coord = p.agent(name="coordy")
    assert p.set_coord("agent:" + coord["agent_id"])[0] == 200
    root = root_for(p, coord)
    s, out = p.make(coord, target_role="tester", parent_task_id=root)
    assert s == 400 and out["code"] == "coordination_loop", out                       # the only tester is an assistant
    assert p.make(coord, target_role="implementer", parent_task_id=root)[0] == 201    # a plain holder is still fine


def test_a_to_b_to_c_to_a_is_refused_with_the_flag_and_allowed_without_it(cp):
    p = cp
    agents = {n: p.agent(name=n) for n in ("aa", "bb", "cc")}
    for a in agents.values():
        assert p.grant(a, ["read", "post", "execute", "instruct"])[0] == 201

    def chain(expect_last):
        root = p.make("alice", target_agent_id=agents["aa"]["agent_id"], text="go")[1]["task_id"]
        p.accept(agents["aa"], root)
        t1 = p.make(agents["aa"], target_agent_id=agents["bb"]["agent_id"], parent_task_id=root)[1]["task_id"]
        p.accept(agents["bb"], t1)
        t2 = p.make(agents["bb"], target_agent_id=agents["cc"]["agent_id"], parent_task_id=t1)[1]["task_id"]
        p.accept(agents["cc"], t2)
        s, out = p.make(agents["cc"], target_agent_id=agents["aa"]["agent_id"], parent_task_id=t2)       # C hands back to A
        assert s == expect_last, out
        return out

    out = chain(400)
    assert out["code"] == "coordination_loop"
    p.svc.coordinators = normalize_coordinators(None)           # flag off: slice 2 behaviour, the chain is allowed
    chain(201)
