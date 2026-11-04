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
from tests.control_plane.wg_pg import wg, wg_pg  # noqa: F401

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


# ============================================================== revoking an operator-owned coordinator
def reborn(p, tmp_path):
    svc = _service(tmp_path, p.clock, name="reborn")
    svc.hydrate(p.svc._log)
    return svc


def owner_pr(svc):
    return svc.authenticate(f"Bearer {USERS['alice']}", None)


def test_clearing_the_coordinator_cancels_its_open_work_stops_new_work_and_survives_a_restart(cp, tmp_path):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    c1 = p.make(coord, target_role="implementer", parent_task_id=root, text="one")[1]
    c2 = p.make(coord, target_role="implementer", parent_task_id=root, text="two")[1]
    holder = next(a for a in held if a["agent_id"] == c1["routed_to"][6:])
    p.accept(holder, c1["task_id"])
    s, out = p.clear_coord()
    assert s == 200 and sorted(out["cancelled"]) == sorted([c1["task_id"], c2["task_id"]]) and out["requeued"] == []
    for a in held:
        assert p.inbox(a) == []
    assert p.make(coord, target_role="implementer", parent_task_id=root)[0] == 404         # its access went with the designation
    assert p.view_coord()[1]["holder"] is None
    n = len(p.svc._log)
    assert p.clear_coord()[1]["replayed"] is True and len(p.svc._log) == n                 # repeating changes nothing
    again = reborn(p, tmp_path)
    assert again.view_team_coordinator(owner_pr(again), p.room, TEAM)["holder"] is None
    assert [t["status"] for t in again._tasks.values() if t.get("coord")] == ["cancelled", "cancelled"]
    # a later designation starts clean
    assert p.set_coord("agent:" + coord["agent_id"])[0] == 200


def test_the_designation_and_the_cap_survive_a_restart(cp, tmp_path):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    again = reborn(p, tmp_path)
    row = again.view_team_coordinator(owner_pr(again), p.room, TEAM)
    assert row["holder"] == "agent:" + coord["agent_id"] and row["active"] is True
    assert row["limits"]["tasks_per_hour"] == 4 and row["usage"] == {"created_last_hour": 0, "open": 0}
    pr = again.authenticate(f"Agent {coord['credential']}", coord["device_id"])
    assert again.create_task(pr, p.room, {"target_role": "implementer", "parent_task_id": root, "text": "x"})["status"] == "pending"


def test_detaching_the_team_ends_the_coordinator_and_its_open_work_and_the_flags_off_still_allow_it(cp):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    child = p.make(coord, target_role="implementer", parent_task_id=root)[1]["task_id"]
    p.svc.coordinators = normalize_coordinators(None)                  # a kill switch never blocks a revocation
    s, out = p.detach()
    assert s == 200 and coord["agent_id"] in out["removed"]
    pr = p.svc.authenticate(f"Agent {coord['credential']}", coord["device_id"])
    assert p.svc.agent_rooms(pr)["rooms"] == []
    p.svc.coordinators = normalize_coordinators(COORD)
    p.svc.sweep_coordinators()
    assert p.svc._tasks[child]["status"] == "cancelled"
    assert p.view_coord()[0] == 404


def test_a_revoked_coordinator_agent_loses_its_open_work_at_the_next_sweep(cp):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    child = p.make(coord, target_role="implementer", parent_task_id=root)[1]["task_id"]
    assert p.c("alice", "POST", f"{BASE}/agents/{coord['agent_id']}/revoke", {})[0] == 200
    assert p.svc.sweep_coordinators()["cancelled"] == 1
    assert p.svc._tasks[child]["status"] == "cancelled"
    assert p.svc.sweep_coordinators()["cancelled"] == 0                # idempotent


# ============================================================== independent review reaches the coordinator chain
def test_the_coordinator_is_never_the_independent_reviewer_of_work_it_decomposed(cp):
    p = cp
    held, rev, coord = crew(p)
    root = root_for(p, coord)
    child = p.make(coord, target_role="implementer", parent_task_id=root)[1]
    holder = next(a for a in held if a["agent_id"] == child["routed_to"][6:])
    p.accept(holder, child["task_id"])
    p.update(holder, child["task_id"], "done", result="ok")
    s, out = p.make("alice", target_agent_id=coord["agent_id"], reviews_task_id=child["task_id"], text="review it")
    assert s == 400 and "reviewer" in out["error"], out                       # the coordinator that decomposed it
    s, out = p.make("alice", target_agent_id=holder["agent_id"], reviews_task_id=child["task_id"], text="review it")
    assert s == 400, out                                                       # the implementer (slice 2 rule, still true)
    assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM}/roles", {"role": "reviewer", "holder": "user:bob"})[0] == 200
    s, out = p.make("alice", target_role="reviewer", reviews_task_id=child["task_id"], text="review it")
    assert s == 201 and out["routed_to"] == "user:bob", out                  # a different principal is accepted


# ============================================================== the Work Graph shows the coordinator, it never grants
def test_the_work_graph_shows_the_coordinator_as_a_team_role_fact(tmp_path, wg, wg_pg):
    from herdr_engineering.control_plane import workspace_contracts as wc
    from herdr_engineering.control_plane.work_graph_store import WorkGraphStore
    from tests.control_plane.test_team_rooms import Plane
    wg.run(wg.pool.execute("DELETE FROM fabric_events"))
    clock = Clock()
    svc = _service(tmp_path, clock)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, api_keys={"op-key": "operator"}, collab=svc,
                             unified_workspace_config={"enabled": True}, github_pg_dsn=wg_pg["dsn"])
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    for _ in range(100):
        if srv.work_graph.available():
            break
        threading.Event().wait(0.1)
    try:
        p = Plane(svc, srv, wg, clock)
        team = p.team()
        impl, coord = p.agent(name="impl"), p.agent(name="coordy")
        assert p.attach(team, {"implementer": [impl["agent_id"]]})[0] == 200
        assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/teams/{team}/coordinator", {"holder": "agent:" + coord["agent_id"]})[0] == 200
        p.sync_graph()
        dump = p.wg.run(WorkGraphStore(p.wg.pool).dump())
        urn = wc.urn_for("team", team)
        held = {f["subject_ref"] for f in dump["facts"].values()
                if f["fact_kind"] == "team_role" and f["object_id"] == urn and f["open"]}
        assert held == {f"agent:{coord['agent_id']}", f"agent:{impl['agent_id']}"}
        assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/teams/{team}/coordinator/clear", {})[0] == 200
        p.sync_graph()
        dump = p.wg.run(WorkGraphStore(p.wg.pool).dump())
        held = {f["subject_ref"] for f in dump["facts"].values()
                if f["fact_kind"] == "team_role" and f["object_id"] == urn and f["open"]}
        assert held == {f"agent:{impl['agent_id']}"}
    finally:
        srv.shutdown()


# ============================================================== (b) external coordinators
EXT_LIMITS = {"roles": ["implementer", "reviewer"]}


def ext_team(p, impls=1, limits=None, team=TEAM2, who="dave"):
    """A team with NO operator-owned coordinator, opted in for external coordination and coordinated by ``who``."""
    held = [p.agent(name=f"eimpl{i}") for i in range(impls)]
    rev = p.agent(name="erev")
    s, out = p.attach({"implementer": [a["agent_id"] for a in held], "reviewer": [rev["agent_id"]]}, team=team)
    assert s == 200, out
    s, out = p.ext(True, limits or EXT_LIMITS, team=team)
    assert s == 200 and out["enabled"] is True, out
    s, out = p.set_coord("user:" + who, team=team)
    assert s == 200 and out["kind"] == "external", out
    return held, rev


def ext_root(p, user="dave", text="build the thing", by="alice"):
    """The operator's own root task for the external coordinator, accepted by them."""
    s, out = p.make(by, target_user_id=user, text=text)
    assert s == 201, out
    s, out2 = p.respond(user, out["task_id"], "accepted")
    assert s == 200, out2
    return out["task_id"]


def child(p, root, user="dave", role="implementer", **body):
    return p.make(user, target_role=role, parent_task_id=root, **body)


def test_the_opt_in_is_the_owners_alone_and_off_by_default(cp):
    p = cp
    held = [p.agent(name="w")]
    assert p.attach({"implementer": [held[0]["agent_id"]]}, team=TEAM2)[0] == 200
    assert p.view_coord(team=TEAM2)[1]["external"]["enabled"] is False
    s, out = p.set_coord("user:dave", team=TEAM2)
    assert s == 409 and out["code"] == "external_opt_in_required"
    for user in ("bob", "carol", "dave", "erin", "mallory"):
        assert p.ext(True, EXT_LIMITS, team=TEAM2, user=user)[0] == 404, user
    assert not p.events("team_external_coordination_set")
    # the owner's limits are validated against the service-wide ceiling
    for bad in (None, {}, {"roles": []}, {"roles": ["coordinator"]}, {"roles": ["implementer"], "tasks_per_hour": 99},
                {"roles": ["implementer"], "max_open": 0}, {"roles": ["implementer"], "nope": 1},
                {"roles": ["implementer"], "max_depth": 3}):
        assert p.ext(True, bad, team=TEAM2)[0] == 400, bad
    s, out = p.ext(True, {"roles": ["implementer"], "tasks_per_hour": 2}, team=TEAM2)
    assert s == 200 and out["limits"]["roles"] == ["implementer"] and out["replayed"] is False
    assert p.ext(True, {"roles": ["implementer"], "tasks_per_hour": 2}, team=TEAM2)[1]["replayed"] is True
    # who may be designated once the opt-in is on
    assert p.set_coord("user:alice", team=TEAM2)[0] == 400
    assert p.set_coord("user:bob", team=TEAM2)[0] == 400                      # a contributor already holds grant_agent
    assert p.set_coord("user:carol", team=TEAM2)[0] == 400                    # a viewer is not a guest
    assert p.set_coord("user:mallory", team=TEAM2)[0] == 400                  # not a member
    assert p.set_coord("user:dave", team=TEAM2)[0] == 200
    # the coordinator can neither widen nor switch off the opt-in, and an agent gets 403
    for body in ({"enabled": True, "limits": {"roles": ["implementer", "tester"], "tasks_per_hour": 5}}, {"enabled": False}):
        before = len(p.denials(actor_user_id="dave"))
        p.clock.t += 61
        assert p.c("dave", "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM2}/external-coordination", body)[0] == 403
        assert len(p.denials(actor_user_id="dave")) == before + 1
    agent = p.agent(name="x")
    assert p.a(agent, "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM2}/external-coordination", {"enabled": False})[0] == 404
    assert [e["payload"]["enabled"] for e in p.events("team_external_coordination_set")] == [True]
    assert p.view_coord(team=TEAM2)[1]["external"]["enabled"] is True


def test_an_external_coordinator_acts_as_themselves_and_the_chain_really_runs(cp):
    p = cp
    (impl,), rev = ext_team(p)
    root = ext_root(p)
    mark = len(p.svc._log)
    s, out = child(p, root, text="part one")
    assert s == 201 and out["routed_to"] == "agent:" + impl["agent_id"], out
    caused = [e for e in p.svc._log[mark:]]
    assert [e["event_type"] for e in caused] == ["task_created"]
    pl = caused[0]["payload"]
    assert pl["actor_user_id"] == "dave" and pl["created_by_kind"] == "user" and pl["created_by_id"] == "dave"
    assert pl["origin"] == "coordinator" and pl["coordinator_key"] == "user:dave" and pl["root_user_id"] == "alice"
    assert "alice" not in json.dumps({k: v for k, v in pl.items() if k != "root_user_id"})   # never attributed to the operator
    t = p.svc._tasks[out["task_id"]]
    assert (t["by_kind"], t["by_id"]) == ("user", "dave")
    # the assignee sees it, accepts it, finishes it: a guest-made task is really served
    assert [x["task_id"] for x in p.inbox(impl)] == [out["task_id"]]
    p.accept(impl, out["task_id"])
    assert p.update(impl, out["task_id"], "done", result="a secret result")[0] == 200
    # tracking is status only: no result, no task list, no task by id
    s, view = p.coordination("dave")
    assert s == 200 and view["kind"] == "external" and view["usage"]["created_last_hour"] == 1
    assert view["tasks"][0]["status"] == "done" and "result" not in view["tasks"][0]
    assert "a secret result" not in json.dumps(view)
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/tasks")[1]["tasks"] == []
    assert p.task("dave", out["task_id"])[0] == 404
    assert p.task("erin", out["task_id"])[0] == 404
    assert p.task("alice", out["task_id"])[0] == 200
    # a coordinator's own audit trail is attributed to them with the marker
    for e in p.svc._log[mark:]:
        if e["event_type"] == "audit_denied":
            assert e["payload"]["actor_user_id"] == "dave" and e["payload"]["origin"] == "coordinator"


def test_an_external_coordinator_stays_inside_the_team_roles_and_limits(cp):
    p = cp
    p.svc.coordinators = normalize_coordinators({**COORD, "external": {"enabled": True, "tasks_per_hour": 3, "max_open": 4,
                                                                         "max_items_per_request": 3}})
    (impl,), rev = ext_team(p, limits={"roles": ["implementer"], "max_items_per_request": 2})
    root = ext_root(p)
    bobs = p.make("bob", target_user_id="dave", text="bob's own idea")[1]["task_id"]         # a task a contributor made
    p.respond("dave", bobs, "accepted")
    assert child(p, bobs)[0] == 400                                                          # no operator-made root: refused
    assert p.make("dave", target_role="implementer")[0] == 400                                # no root at all
    # a role outside the list, another team, a named agent or person, a foreign agent: refused, and audited under dave
    p.clock.t += 61
    n = len(p.denials(actor_user_id="dave"))
    assert child(p, root, role="reviewer")[0] == 403
    assert child(p, root, team_id=TEAM)[0] == 403
    assert len(p.denials(actor_user_id="dave")) == n + 2
    inv = p.svc.create_external_invite(p.svc.authenticate(f"Bearer {USERS['alice']}", None), p.room, {"label": "foreign"})
    foreign = p.svc.redeem_join_token({"token": inv["code"]})
    for body in ({"target_agent_id": impl["agent_id"]}, {"target_agent_id": foreign["agent_id"]}, {"target_user_id": "erin"},
                 {"target_role": "implementer", "lease_seconds": 100}):
        assert p.make("dave", parent_task_id=root, **body)[0] == 400, body
    # items per request: the 3rd child of one root is the cap's doing
    assert child(p, root, text="a")[0] == 201 and child(p, root, text="b")[0] == 201
    s, out = child(p, root, text="c")
    assert s == 429 and out["code"] == "coordinator_items", out
    # a child of a child is not a root: the chain cannot be extended past the operator's task
    first = p.svc._tasks[next(iter(p.svc._tc_tasks[(p.room, TEAM2)]))]
    assert first["depth"] == 1
    # the person's own created tasks never made a task for a different room
    assert child(p, root, room=p.room2)[0] in (403, 404)


def test_an_external_coordinator_cannot_target_an_assistant_that_holds_a_role_in_their_team(cp):
    p = cp
    (asst,) = _assistants(p, 1, plain_grants=())
    impl = p.agent(name="eimpl")
    assert p.attach({"implementer": [impl["agent_id"]], "tester": [asst["agent_id"]]}, team=TEAM2)[0] == 200
    assert p.ext(True, {"roles": ["implementer", "tester"]}, team=TEAM2)[0] == 200
    assert p.set_coord("user:dave", team=TEAM2)[0] == 200
    root = ext_root(p)
    s, out = child(p, root, role="tester")
    assert s == 400 and out["code"] == "coordination_loop", out
    assert child(p, root, role="implementer")[0] == 201


def test_the_external_cap_is_lower_than_the_operator_owned_cap_at_the_same_moment(cp):
    p = cp
    held, rev, coord = crew(p, impls=3)                                       # TEAM, operator-owned, cap 4
    (eimpl,), erev = ext_team(p, limits={"roles": ["implementer"], "max_open": 3})
    oroot, eroot = root_for(p, coord), ext_root(p)
    for i in range(2):
        assert child(p, eroot, text=f"e{i}")[0] == 201
    s, out = child(p, eroot, text="e2")
    assert s == 429 and out["code"] == "coordinator_rate", out                # (cap = 2) + 1
    for i in range(3):                                                         # the operator-owned coordinator, same instant
        assert p.make(coord, target_role="implementer", parent_task_id=oroot, text=f"o{i}")[0] == 201
    # the per-team limit can only go down from the service-wide ceiling, also when the ceiling is lowered later
    p.clock.t += 3601
    p.svc.coordinators = normalize_coordinators({**COORD, "external": {"enabled": True, "tasks_per_hour": 1, "max_open": 3}})
    assert child(p, eroot, text="late")[0] == 201
    assert child(p, eroot, text="later")[1]["code"] == "coordinator_rate"


def test_the_open_limit_for_an_external_coordinator_is_its_own_code(cp):
    p = cp
    p.svc.coordinators = normalize_coordinators({**COORD, "external": {"enabled": True, "tasks_per_hour": 3, "max_open": 3}})
    ext_team(p, limits={"roles": ["implementer"], "max_open": 1})
    root = ext_root(p)
    assert child(p, root, text="one")[0] == 201
    s, out = child(p, root, text="two")
    assert s == 429 and out["code"] == "coordinator_open", out


def test_everything_an_external_coordinator_must_not_do_is_403_with_an_audit_denial_under_their_identity(cp):
    p = cp
    held, rev, coord = crew(p)
    ext_team(p)
    spare = p.agent(name="spare")
    s, out = p.suggest_as_erin = p.c("erin", "POST", f"{BASE}/rooms/{p.room}/suggestions", {"text": "please"})
    sg = out["suggestion_id"] if s == 201 else None
    tries = {
        "grant": ("POST", f"/rooms/{p.room}/agents", {"agent_id": spare["agent_id"], "actions": ["read", "post"]}),
        "invite": ("POST", f"/rooms/{p.room}/invites", {"email": "x@example.com", "role": "guest"}),
        "add_member": ("POST", f"/rooms/{p.room}/members", {"user_id": "mallory", "role": "guest"}),
        "role_assign": ("POST", f"/rooms/{p.room}/teams/{TEAM2}/roles", {"role": "tester", "holder": "user:dave"}),
        "coordinator_set": ("POST", f"/rooms/{p.room}/teams/{TEAM}/coordinator", {"holder": "agent:" + spare["agent_id"]}),
        "coordinator_clear": ("POST", f"/rooms/{p.room}/teams/{TEAM2}/coordinator/clear", {}),
        "guest_level": ("POST", f"/rooms/{p.room}/guest-level", {"level": "read_only"}),
        "optin": ("POST", f"/rooms/{p.room}/teams/{TEAM2}/external-coordination", {"enabled": False}),
        "attach": ("POST", f"/rooms/{p.room}/teams/{'team-' + 'f' * 20}/attach", {"roles": {"implementer": [spare["agent_id"]]}}),
        "detach": ("POST", f"/rooms/{p.room}/teams/{TEAM2}/detach", {}),
        "decide": ("POST", f"/rooms/{p.room}/suggestions/{sg}/decide", {"decision": "reject", "expected_digest": "0" * 64}),
        "policy": ("POST", f"/rooms/{p.room}/teams/{TEAM2}/policy", {"no_response_policy": "wait"}),
        "visibility": ("POST", f"/rooms/{p.room}/member-visibility", {"enabled": True}),
    }
    for name, (method, path, body) in tries.items():
        before = len(p.denials(actor_user_id="dave"))
        p.clock.t += 61
        s, _ = p.c("dave", method, BASE + path, body)
        assert s == 403, (name, s)
        assert len(p.denials(actor_user_id="dave")) == before + 1, name
    assert all(e["payload"]["origin"] == "coordinator" for e in p.denials(actor_user_id="dave"))
    assert p.view_coord(team=TEAM2)[1]["holder"] == "user:dave" and not p.events("team_detached")
    assert not p.events("team_role_assigned") and not p.events("invite_created")
    assert p.c("alice", "GET", f"{BASE}/rooms/{p.room}/guest-level")[1]["explicit"] is False
    # erin, a plain guest, still gets the old 404 from the owner-only routes (the 403 is for a known coordinator)
    assert p.c("erin", "POST", f"{BASE}/rooms/{p.room}/guest-level", {"level": "read_only"})[0] == 404


def _http(port, method, path, body=None, headers=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, None


def test_no_coordinator_can_approve_an_external_action_only_the_owner_decides(tmp_path):
    """A real assistant service on the same server: a pending approval, decided over real HTTP. The collaboration credentials
    (an agent's, an external person's) do not authenticate on the assistant API at all, and the approval stays pending."""
    from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
    clock = Clock()
    svc = _service(tmp_path, clock)
    asvc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True,
                                                                           "actions": {"allow_self_approval": True}}}))
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, assistant_service=asvc,
                             api_keys={"op-key": "operator"})
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    svc.work_graph_handle = None
    try:
        p = CPlane(svc, srv, clock)
        held, rev, coord = crew(p)
        ext_team(p)
        op = {"X-API-Key": "op-key"}
        s, a = _http(srv.port, "POST", "/api/v1/assistants", {"name": "A"}, op)
        assert s in (200, 201), a
        aid = a["assistant_id"]
        _, t = _http(srv.port, "POST", f"/api/v1/assistants/{aid}/threads", {}, op)
        _, r = _http(srv.port, "POST", f"/api/v1/assistants/{aid}/threads/{t['thread_id']}/messages", {"content": "hi"}, op)
        run = r["run"]["run_id"]
        assert _http(srv.port, "POST", f"/api/v1/assistants/{aid}/permissions", {"tool_id": "edit_shared", "scope": "rec"}, op)[0] == 201
        s, need = _http(srv.port, "POST", f"/api/v1/assistant-runs/{run}/tool",
                        {"tool_id": "edit_shared", "scope": "rec", "idempotency_key": "k1",
                         "payload": {"record": "rec", "field": "f", "value": "v"}}, op)
        assert s == 403 and need.get("approval_id"), need
        path = f"/api/v1/assistant-approvals/{need['approval_id']}/decision"
        agent_headers = {"Authorization": f"Agent {coord['credential']}", "X-Herdr-Device": coord["device_id"]}
        for headers in (agent_headers, {"Authorization": f"Bearer {USERS['dave']}"}, {"Authorization": f"Bearer {USERS['alice']}"}, {}):
            s, _ = _http(srv.port, "POST", path, {"decision": "approved"}, headers)
            assert s in (401, 403, 404), headers
        for route in ("/api/v1/assistants", "/api/v1/assistant-approvals", "/api/v1/assistant-memory"):
            assert _http(srv.port, "GET", route, None, {"Authorization": f"Bearer {USERS['dave']}"})[0] in (401, 403, 404), route
            assert _http(srv.port, "GET", route, None, agent_headers)[0] in (401, 403, 404), route
        _, listing = _http(srv.port, "GET", "/api/v1/assistant-approvals", None, op)
        assert [x["decision"] for x in listing["approvals"]] == ["pending"]
        s, done = _http(srv.port, "POST", path, {"decision": "approved"}, op)
        assert s == 200 and done["decision"] == "approved", done
    finally:
        srv.shutdown()


# -- revocation: the opt-in, the removal, the identity provider, a detach
def two_children(p, root):
    c1 = child(p, root, text="one")[1]
    c2 = child(p, root, text="two")[1]
    return c1["task_id"], c2["task_id"]


def test_turning_the_opt_in_off_ends_the_coordinator_requeues_their_work_and_fences_the_old_run(cp):
    p = cp
    (impl,), rev = ext_team(p)
    root = ext_root(p)
    c1, c2 = two_children(p, root)
    p.accept(impl, c1)                                                       # one is running, one is waiting
    s, out = p.ext(False, team=TEAM2)
    assert s == 200 and out["enabled"] is False and sorted(out["requeued"]) == sorted([c1, c2]) and out["cancelled"] == []
    assert [e["payload"]["enabled"] for e in p.events("team_external_coordination_set")] == [True, False]
    assert p.events("team_coordinator_cleared")[0]["payload"]["reason"] == "external_coordination_off"
    assert len(p.events("task_requeued")) == 2
    # the work goes back to the role queue under the operator, with a higher attempt, and is still served
    for tid in (c1, c2):
        t = p.svc._tasks[tid]
        assert t["status"] == "pending" and t["attempt"] == 2 and t["by_id"] == "alice" and t["adopted_from"] == "user:dave"
    assert sorted(x["task_id"] for x in p.inbox(impl)) == sorted([c1, c2]) and {x["attempt"] for x in p.inbox(impl)} == {2}
    # the old run cannot finish it: with the old attempt, or with none
    assert p.update(impl, c1, "done", attempt=1)[0] == 409
    assert p.update(impl, c1, "accepted")[0] == 409
    assert p.update(impl, c1, "accepted", attempt=2)[0] == 200 and p.update(impl, c1, "done", result="ok", attempt=2)[0] == 200
    # dave's credential no longer authorizes coordinate
    p.clock.t += 61
    s, _ = child(p, root, text="again")
    assert s == 403 and p.denials("task.create", actor_user_id="dave")
    assert p.coordination("dave")[0] == 404
    # repeating the revocation changes nothing
    n = len(p.svc._log)
    s, out = p.ext(False, team=TEAM2)
    assert s == 200 and out["replayed"] is True and len(p.svc._log) == n
    assert p.clear_coord(team=TEAM2)[1]["replayed"] is True and len(p.svc._log) == n
    # and a re-grant starts clean: the old tasks are the operator's now, not the coordinator's
    assert p.ext(True, EXT_LIMITS, team=TEAM2)[0] == 200 and p.set_coord("user:dave", team=TEAM2)[0] == 200
    assert p.coordination("dave")[1]["tasks"] == [] or all(t["adopted"] for t in p.coordination("dave")[1]["tasks"])


def test_a_requeued_task_still_excludes_the_revoked_coordinator_as_its_reviewer(cp):
    p = cp
    (impl,), rev = ext_team(p)
    p.c("alice", "POST", f"{BASE}/rooms/{p.room}/teams/{TEAM2}/roles", {"role": "reviewer", "holder": "user:erin"})
    root = ext_root(p)
    c1 = child(p, root)[1]["task_id"]
    assert p.ext(False, team=TEAM2)[0] == 200
    s, out = p.make("alice", target_user_id="dave", reviews_task_id=c1, text="review")
    assert s == 400, out                                                      # the one who decomposed it, even after the requeue
    assert p.make("alice", target_user_id="erin", reviews_task_id=c1, text="review")[0] == 201


def test_removing_the_person_from_the_room_requeues_their_work_at_once(cp):
    p = cp
    (impl,), rev = ext_team(p)
    root = ext_root(p)
    c1, c2 = two_children(p, root)
    assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/members/dave/remove", {})[0] == 200
    assert [p.svc._tasks[t]["attempt"] for t in (c1, c2)] == [2, 2]
    assert p.update(impl, c1, "accepted")[0] == 409
    p.clock.t += 61
    assert p.c("dave", "POST", f"{BASE}/rooms/{p.room}/tasks", {"target_role": "implementer", "parent_task_id": root,
                                                                "text": "x"})[0] == 404


def test_detaching_the_team_cancels_what_cannot_be_requeued(cp):
    p = cp
    (impl,), rev = ext_team(p)
    root = ext_root(p)
    c1, c2 = two_children(p, root)
    assert p.detach(team=TEAM2)[0] == 200
    assert [p.svc._tasks[t]["status"] for t in (c1, c2)] == ["cancelled", "cancelled"]


def test_with_the_second_flag_off_a_coordinators_tasks_are_no_longer_served_but_the_owner_can_still_revoke(cp):
    p = cp
    (impl,), rev = ext_team(p)
    root = ext_root(p)
    c1 = child(p, root)[1]["task_id"]
    p.svc.coordinators = normalize_coordinators({**COORD, "external": {"enabled": False, "tasks_per_hour": 2}})
    p.clock.t += 61
    assert child(p, root)[0] == 403
    assert p.inbox(impl) == []                                               # authority withdrawn: nothing is handed to the agent
    assert p.svc.sweep_coordinators()["requeued"] == 1                        # the withdrawn authority is requeued by the sweep
    assert p.svc._tasks[c1]["attempt"] == 2 and p.svc._tasks[c1]["by_id"] == "alice"
    assert [x["task_id"] for x in p.inbox(impl)] == [c1]                       # and served again, under the operator
    s, out = p.ext(False, team=TEAM2)                                         # the OFF direction stays reachable
    assert s == 200 and out["requeued"] == []
    assert p.ext(True, EXT_LIMITS, team=TEAM2)[0] == 404


def test_a_restart_keeps_the_opt_in_the_cap_and_a_finished_requeue(cp, tmp_path):
    p = cp
    (impl,), rev = ext_team(p)
    root = ext_root(p)
    c1, c2 = two_children(p, root)
    again = reborn(p, tmp_path)
    row = again.view_team_coordinator(owner_pr(again), p.room, TEAM2)
    assert row["holder"] == "user:dave" and row["external"]["enabled"] is True and row["usage"]["created_last_hour"] == 2
    pr = again.authenticate(f"Bearer {USERS['dave']}", None)
    from herdr_engineering.control_plane.collab import CollabError
    with pytest.raises(CollabError) as ei:
        again.create_task(pr, p.room, {"target_role": "implementer", "parent_task_id": root, "text": "x"})
    assert ei.value.code == "coordinator_rate"
    p.ext(False, team=TEAM2)
    final = reborn(p, tmp_path)
    assert [final._tasks[t]["attempt"] for t in (c1, c2)] == [2, 2] and final._tc_coord == {}


# -- the identity provider (Cognito) drops the person's group
def test_the_identity_provider_group_removal_ends_access_within_its_window_and_the_requeue_still_runs(tmp_path):
    from tests.control_plane.test_collab_cognito import auth, token
    clock = Clock()
    groups = {u: ["dev"] for u in ("alice", "dave")}
    a = auth(group_lookup=lambda u: groups.get(u), now=clock)
    svc = CollabService(a, sink=FileEventLog(str(tmp_path / "cog")), now=clock, max_agents_per_user=20)
    svc.team_rooms = normalize_team_rooms({"enabled": True})
    svc.team_roles = normalize_team_roles(ROLES)
    svc.coordinators = normalize_coordinators(COORD)
    srv = _serve(svc)
    api = Api(srv.port)

    def call(user, method, path, body=None):
        return api.call(method, path, body, headers={"Authorization": f"Bearer {token(user, exp=9_999_999_999)}"})
    try:
        s, pr = call("alice", "POST", f"{BASE}/projects", {"name": "p"})
        s, r = call("alice", "POST", f"{BASE}/projects/{pr['project_id']}/rooms", {"name": "r"})
        room = r["room_id"]
        assert call("alice", "POST", f"{BASE}/rooms/{room}/members", {"user_id": "dave", "role": "guest"})[0] == 201
        s, impl = call("alice", "POST", f"{BASE}/agents/connect", {"harness_type": "claude-code", "name": "i"})
        assert call("alice", "POST", f"{BASE}/rooms/{room}/teams/{TEAM2}/attach", {"roles": {"implementer": [impl["agent_id"]]}})[0] == 200
        assert call("alice", "POST", f"{BASE}/rooms/{room}/teams/{TEAM2}/external-coordination",
                    {"enabled": True, "limits": {"roles": ["implementer"]}})[0] == 200
        assert call("alice", "POST", f"{BASE}/rooms/{room}/teams/{TEAM2}/coordinator", {"holder": "user:dave"})[0] == 200
        s, root = call("alice", "POST", f"{BASE}/rooms/{room}/tasks", {"target_user_id": "dave", "text": "go"})
        assert s == 201
        assert call("dave", "POST", f"{BASE}/rooms/{room}/tasks/{root['task_id']}/respond", {"status": "accepted"})[0] == 200
        s, c1 = call("dave", "POST", f"{BASE}/rooms/{room}/tasks",
                     {"target_role": "implementer", "parent_task_id": root["task_id"], "text": "one"})
        assert s == 201, c1
        inbox = lambda: api.call("GET", f"{BASE}/tasks/inbox", agent=impl)[1]["tasks"]  # noqa: E731
        assert [t["task_id"] for t in inbox()] == [c1["task_id"]]
        # the group is removed: inside the recheck window nothing has changed yet
        groups["dave"] = []
        assert svc.sweep_coordinators()["requeued"] == 0
        # past the window the person's token and their coordinate right are gone, without any request of theirs
        clock.t += 61
        assert call("dave", "GET", f"{BASE}/rooms/{room}/coordination")[0] == 401
        assert inbox() == []                                                 # their task is no longer handed out
        out = svc.sweep_coordinators()
        assert out["requeued"] == 1
        t = svc._tasks[c1["task_id"]]
        assert t["attempt"] == 2 and t["adopted_from"] == "user:dave" and t["by_id"] == "alice"
        assert [x["task_id"] for x in inbox()] == [c1["task_id"]]            # and it is served again, under the operator
        assert api.call("POST", f"{BASE}/tasks/{c1['task_id']}/update", {"status": "accepted", "attempt": 1}, agent=impl)[0] == 409
        assert svc.sweep_coordinators()["requeued"] == 0                      # repeating changes nothing
    finally:
        srv.shutdown()
