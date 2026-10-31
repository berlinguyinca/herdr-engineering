# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Slice 2 of docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md: roles, routing by role, people as role
holders, per-task leases with the no-response policy, and independent review across agents and people.

Real ``CollabService`` with its file event log, a real Postgres for the Work Graph, real HTTP through ``ControlPlaneServer``.
Nothing is mocked. The only thing a test moves is the service's clock, and the sweep is called directly after moving it."""
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
from herdr_engineering.control_plane.collab_team_roles import normalize_team_roles
from herdr_engineering.control_plane.collab_team_rooms import normalize_team_rooms
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import BASE, Clock, _digest
from tests.control_plane.test_team_rooms import USERS, Plane
from tests.control_plane.wg_pg import wg, wg_pg  # noqa: F401

ROLES = {"enabled": True, "person_lease_seconds": 1000, "hard_expiry_seconds": 5000, "max_lease_extensions": 2}


def _service(tmp_path, clock, roles=None, name="log"):
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                        sink=FileEventLog(str(tmp_path / name)), now=clock, max_agents_per_user=20)
    svc.team_rooms = normalize_team_rooms({"enabled": True})
    svc.team_roles = normalize_team_roles(ROLES if roles is None else roles)
    return svc


class RPlane(Plane):
    def assign(self, team, role, holder, user="alice", room=None):
        return self.c(user, "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/roles", {"role": role, "holder": holder})

    def clear(self, team, role, holder, user="alice", room=None):
        return self.c(user, "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/roles/clear", {"role": role, "holder": holder})

    def roles(self, team, user="alice", room=None):
        return self.c(user, "GET", f"{BASE}/rooms/{room or self.room}/teams/{team}/roles")

    def policy(self, team, policy, user="alice"):
        return self.c(user, "POST", f"{BASE}/rooms/{self.room}/teams/{team}/policy", {"no_response_policy": policy})

    def make(self, user, **body):
        body.setdefault("text", "do the thing")
        return self.c(user, "POST", f"{BASE}/rooms/{self.room}/tasks", body)

    def mine(self, user):
        return self.c(user, "GET", f"{BASE}/rooms/{self.room}/my-tasks")

    def respond(self, user, task_id, status, result=None):
        body = {"status": status, **({"result": result} if result else {})}
        return self.c(user, "POST", f"{BASE}/rooms/{self.room}/tasks/{task_id}/respond", body)

    def visibility(self, user, enabled):
        return self.c(user, "POST", f"{BASE}/rooms/{self.room}/member-visibility", {"enabled": enabled})

    def sweep(self):
        return self.svc.sweep_team_roles()

    def inbox(self, agent):
        return self.agent_call(agent, "GET", f"{BASE}/tasks/inbox")[1]["tasks"]

    def feed_kinds(self, user):
        return [e["event_type"] for e in self.c(user, "GET", f"{BASE}/rooms/{self.room}/events?cursor=0")[1]["events"]]


@pytest.fixture
def rp(tmp_path, wg, wg_pg):
    import threading as _t
    wg.run(wg.pool.execute("DELETE FROM fabric_events"))
    clock = Clock()
    svc = _service(tmp_path, clock)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, api_keys={"op-key": "operator"}, collab=svc,
                             unified_workspace_config={"enabled": True}, github_pg_dsn=wg_pg["dsn"])
    _t.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    for _ in range(100):
        if srv.work_graph.available():
            break
        _t.Event().wait(0.1)
    assert srv.work_graph.available()
    try:
        yield RPlane(svc, srv, wg, clock)
    finally:
        srv.shutdown()


def crew(p, agents=2):
    """A team attached to the room: ``agents`` implementer agents (alice's own)."""
    held = [p.agent(name=f"impl{i}") for i in range(agents)]
    team = p.team()
    s, out = p.attach(team, {"implementer": [a["agent_id"] for a in held]})
    assert s == 200, out
    return team, held


def reborn(p, tmp_path, roles=None):
    svc = _service(tmp_path, p.clock, roles, name="reborn")
    svc.hydrate(p.svc._log)
    return svc


def user_pr(svc, user):
    return svc.authenticate(f"Bearer {USERS[user]}", None)


# ============================================================== flag off: nothing changes
def test_with_the_flag_off_role_fields_are_unknown_and_the_new_routes_do_not_exist(rp):
    p = rp
    team, (a1, _) = crew(p)
    p.svc.team_roles = normalize_team_roles(None)
    assert p.make("alice", target_role="implementer")[0] == 400                      # unknown field, exactly as before
    assert p.make("alice", target_user_id="dave")[0] == 400
    assert p.mine("dave")[0] == 404 and p.roles(team)[0] == 404 and p.assign(team, "tester", "user:bob")[0] == 404
    assert p.policy(team, "wait")[0] == 404 and p.visibility("alice", True)[0] == 404
    assert p.respond("dave", "x", "done")[0] == 404
    s, out = p.make("alice", target_agent_id=a1["agent_id"], text="old style")
    assert s == 201 and out["status"] == "pending"
    assert [t["text"] for t in p.inbox(a1)] == ["old style"]
    assert p.svc.sweep_team_roles() == {"active": False}
    assert p.tasks("dave")[1]["tasks"] == []                                          # slice 1 blinding is unchanged


def test_team_roles_needs_team_rooms_on_too(rp):
    p = rp
    team, _ = crew(p)
    p.svc.team_rooms = normalize_team_rooms({"enabled": False})
    assert p.mine("dave")[0] == 404 and p.assign(team, "tester", "user:bob")[0] == 404
    p.svc.team_rooms = normalize_team_rooms({"enabled": True})
    p.svc.team_roles = normalize_team_roles({"enabled": True, "kill_switch": True})
    assert p.mine("dave")[0] == 404


def test_config_is_validated_and_registered_as_a_hot_switch():
    with pytest.raises(ValueError):
        normalize_team_roles({"enabled": True, "bogus": 1})
    with pytest.raises(ValueError):
        normalize_team_roles({"person_lease_seconds": 30})
    with pytest.raises(ValueError):
        normalize_team_roles({"person_lease_seconds": 99 * 86400})
    with pytest.raises(ValueError):
        normalize_team_roles({"default_no_response_policy": "drop"})
    with pytest.raises(ValueError):
        normalize_team_roles({"reminder_fractions": [0.5, 1.5]})
    with pytest.raises(ValueError):
        normalize_team_roles({"reviewer_groups": [["user:a"]]})
    cfg = normalize_team_roles(None)
    assert cfg["enabled"] is False and cfg["default_no_response_policy"] == "escalate"
    assert cfg["person_lease_seconds"] == 3 * 86400 and cfg["reminder_fractions"] == [0.5, 0.9]
    from herdr_engineering import cli
    assert "team_roles" in cli.COLLAB_CONFIG_KEYS and "team_roles" in cli.HOT_COLLAB_SWITCHES


def test_event_types_are_classified_on_purpose():
    assert "team_role_cleared" in SAFETY_EVENTS and "team_role_assigned" in REVOCABLE_CREATE


# ============================================================== (a) role holders: agents and people, owner only
def test_the_owner_assigns_people_to_a_role_and_the_view_lists_agents_and_people(rp):
    p = rp
    team, (a1, a2) = crew(p)
    s, out = p.assign(team, "tester", "user:dave")                                     # an external guest
    assert s == 200 and out["holder"] == "user:dave" and out["kind"] == "implementer" and out["replayed"] is False
    assert p.assign(team, "tester", "user:dave")[1]["replayed"] is True
    assert p.assign(team, "issue_writer", "user:bob")[1]["kind"] == "planner"
    assert p.assign(team, "reviewer", "user:carol")[1]["kind"] == "reviewer"
    s, view = p.roles(team)
    assert s == 200 and view["no_response_policy"] == "escalate"
    by_role = {r["role"]: r for r in view["roles"]}
    assert {h["holder"] for h in by_role["implementer"]["holders"]} == {"agent:" + a1["agent_id"], "agent:" + a2["agent_id"]}
    assert [h["holder"] for h in by_role["tester"]["holders"]] == ["user:dave"]
    assert by_role["implementer"]["kind"] == "implementer" and by_role["issue_writer"]["kind"] == "planner"
    # membership of the room is untouched: assigning a role never changes the person's room role (slice 1 rule)
    assert p.svc._rooms[p.room]["members"]["dave"]["role"] == "guest"


def test_only_the_owner_user_manages_roles_an_agent_gets_403_and_an_audit_denial(rp):
    p = rp
    team, (a1, _) = crew(p)
    for user in ("bob", "carol", "dave", "mallory"):
        assert p.assign(team, "tester", "user:dave", user=user)[0] == 404, user
        assert p.clear(team, "tester", "user:dave", user=user)[0] == 404, user
        assert p.policy(team, "wait", user=user)[0] == 404, user
        assert p.visibility(user, True)[0] == 404, user
    s, _ = p.agent_call(a1, "POST", f"{BASE}/rooms/{p.room}/teams/{team}/roles", {"role": "tester", "holder": "user:dave"})
    assert s == 403
    assert [e for e in p.events("audit_denied") if e["payload"]["action"] == "team.role.assign"
            and e["payload"]["actor_agent_id"] == a1["agent_id"]]
    assert not p.events("team_role_assigned")


def test_holder_validation_fails_closed(rp):
    p = rp
    team, (a1, _) = crew(p)
    assert p.assign(team, "coordinator", "user:bob")[0] == 400                          # slice 3
    assert p.assign(team, "wizard", "user:bob")[0] == 400
    assert p.assign(team, "tester", "user:mallory")[0] == 400                           # not a member of this room
    assert p.assign(team, "tester", "agent:" + a1["agent_id"])[0] == 400                # agents join a role by attach
    assert p.assign(team, "tester", "bob")[0] == 400
    assert p.assign("team-" + "b" * 20, "tester", "user:bob")[0] == 404                 # not attached here
    assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/teams/{team}/roles", {"role": "tester", "holder": "user:bob", "x": 1})[0] == 400
    assert not p.events("team_role_assigned")


def test_clearing_a_role_is_idempotent_and_the_state_survives_a_restart(rp, tmp_path):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.assign(team, "tester", "user:erin")
    p.policy(team, "wait")
    assert p.clear(team, "tester", "user:erin")[1]["replayed"] is False
    n = len(p.svc._log)
    assert p.clear(team, "tester", "user:erin")[1]["replayed"] is True and len(p.svc._log) == n
    again = reborn(p, tmp_path)
    owner = user_pr(again, "alice")
    assert again.team_role_view(owner, p.room, team) == p.roles(team)[1]


# ============================================================== (b) routing by role
def test_a_role_task_goes_to_the_least_loaded_holder_ties_by_key_and_a_replay_never_reroutes(rp, tmp_path):
    p = rp
    team, (a1, a2) = crew(p)
    p.assign(team, "implementer", "user:bob")
    keys = sorted(["agent:" + a1["agent_id"], "agent:" + a2["agent_id"], "user:bob"])
    picked = []
    for i in range(4):
        s, out = p.make("alice", target_role="implementer", text=f"job {i}", idempotency_key=f"k{i}")
        assert s == 201, out
        picked.append(out["routed_to"])
    assert picked[:3] == keys                                                           # all idle: ties go in key order
    assert picked[3] == keys[0]                                                         # all equally loaded again
    created = p.events("task_created")
    assert [e["payload"]["routed_to"] for e in created] == picked and all(e["payload"]["target_role"] == "implementer" for e in created)
    # the load changed afterwards; a replay of the same key must return the SAME task and write nothing
    n = len(p.svc._log)
    s, out = p.make("alice", target_role="implementer", text="job 0", idempotency_key="k0")
    assert s in (200, 201) and out["replayed"] is True and len(p.svc._log) == n
    # a restart from the same log keeps every resolved holder
    again = reborn(p, tmp_path)
    assert [t["routed_to"] for t in again._tasks.values()] == picked
    pr = user_pr(again, "alice")
    out = again.create_task(pr, p.room, {"target_role": "implementer", "text": "job 0", "idempotency_key": "k0"})
    assert out["replayed"] is True and len(again._log) == n


def test_an_agent_holder_task_lands_in_that_agents_inbox_and_a_person_task_in_no_inbox(rp):
    p = rp
    team, (a1, a2) = crew(p)
    p.assign(team, "tester", "user:dave")
    s, out = p.make("alice", target_role="tester", text="check it")
    assert s == 201 and out["routed_to"] == "user:dave"
    assert p.inbox(a1) == [] and p.inbox(a2) == []
    s, out = p.make("alice", target_role="implementer", text="build it")
    assert out["routed_to"].startswith("agent:") and len(p.inbox(a1)) + len(p.inbox(a2)) == 1


def test_no_holder_fails_closed_and_writes_nothing(rp):
    p = rp
    team, (a1, _) = crew(p)
    n = len(p.svc._log)
    s, out = p.make("alice", target_role="tester")
    assert s == 409 and out["code"] == "no_role_holder" and len(p.svc._log) == n
    assert p.make("alice", target_role="coordinator")[0] == 400
    assert p.make("alice", target_role="implementer", target_agent_id=a1["agent_id"])[0] == 400   # one way to name the target
    assert p.make("alice", target_role="implementer", target_user_id="bob")[0] == 400
    # holder gone: a person who left the room is no holder
    p.assign(team, "tester", "user:erin")
    p.c("alice", "POST", f"{BASE}/rooms/{p.room}/members/erin/remove", {})
    s, out = p.make("alice", target_role="tester")
    assert s == 409 and out["code"] == "no_role_holder"
    assert not [e for e in p.events("task_created")]


def test_agents_that_cannot_execute_are_never_a_fall_through(rp):
    """A reviewer agent holds read and post only. Routing to it would need `execute`; it fails closed rather than widening,
    and the request never lands on an implementer agent that is not in the role."""
    p = rp
    team, (a1, _) = crew(p)
    rev = p.agent(name="rev")
    other = p.team("Reviewers")
    assert p.attach(other, {"reviewer": [rev["agent_id"]]})[0] == 200
    work = p.make("alice", target_agent_id=a1["agent_id"], text="work")[1]["task_id"]
    n = len(p.events("task_created"))
    s, out = p.make("alice", target_role="reviewer", reviews_task_id=work)
    assert s == 409 and out["code"] == "no_role_holder" and len(p.events("task_created")) == n
    assert p.inbox(rev) == []
    p.assign(other, "reviewer", "user:carol")                     # a person holding the same role makes it routable
    s, out = p.make("alice", target_role="reviewer", reviews_task_id=work)
    assert s == 201 and out["routed_to"] == "user:carol"


def test_two_teams_holding_one_role_need_a_team_id_and_it_must_be_attached(rp):
    p = rp
    team, _ = crew(p)
    other = p.team("Other")
    extra = p.agent(name="x1")
    assert p.attach(other, {"implementer": [extra["agent_id"]]})[0] == 200
    s, out = p.make("alice", target_role="implementer")
    assert s == 400 and out["code"] == "team_required"
    s, out = p.make("alice", target_role="implementer", team_id=other)
    assert s == 201 and out["routed_to"] == "agent:" + extra["agent_id"]
    assert p.make("alice", target_role="implementer", team_id="team-" + "c" * 20)[0] == 404


def test_a_guest_cannot_create_role_work_and_the_task_rate_limits_still_apply(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:bob")
    assert p.make("dave", target_role="tester")[0] in (403, 404)
    assert p.make("carol", target_role="tester")[0] in (403, 404)
    assert p.make("bob", target_role="tester")[0] == 201


# ============================================================== (c) tasks for people land in their queue
def test_a_person_task_is_in_their_my_tasks_and_in_no_one_elses_view(rp):
    p = rp
    team, (a1, a2) = crew(p)
    p.assign(team, "tester", "user:dave")
    s, made = p.make("alice", target_role="tester", text="please verify the fix")
    tid = made["task_id"]
    mine = p.mine("dave")[1]["tasks"]
    assert [t["task_id"] for t in mine] == [tid] and mine[0]["text"] == "please verify the fix" and mine[0]["status"] == "pending"
    assert mine[0]["role"] == "tester" and mine[0]["untrusted"] is True and "quoted_source" not in mine[0]
    assert mine[0]["lease"]["seconds"] == 1000 and mine[0]["no_response_policy"] == "escalate"
    assert p.inbox(a1) == [] and p.inbox(a2) == []
    for other in ("bob", "carol", "erin"):                                             # a contributor, a viewer, another guest
        assert p.mine(other)[1]["tasks"] == [], other
        assert all(t["task_id"] != tid for t in p.tasks(other)[1]["tasks"]), other
        assert tid not in json.dumps(p.c(other, "GET", f"{BASE}/rooms/{p.room}/events?cursor=0")[1]), other
        assert tid not in json.dumps(p.c(other, "GET", f"{BASE}/rooms/{p.room}/state")[1]), other
        assert "please verify" not in json.dumps(p.c(other, "GET", f"{BASE}/rooms/{p.room}/state")[1]), other
    assert tid not in json.dumps(p.c("bob", "GET", f"{BASE}/rooms/{p.room}/flow")[1])
    assert [t["task_id"] for t in p.tasks("alice")[1]["tasks"]] == [tid]                # the owner sees everything
    assert p.mine("mallory")[0] == 404
    assert p.agent_call(a1, "GET", f"{BASE}/rooms/{p.room}/my-tasks")[0] in (403, 404)
    assert "task_created" not in p.feed_kinds("dave")           # a guest never sees task events (slice 1)


def test_a_task_can_name_a_person_directly_and_the_person_must_be_a_member(rp):
    p = rp
    crew(p)
    s, out = p.make("alice", target_user_id="bob", text="direct")
    assert s == 201 and out["routed_to"] == "user:bob"
    assert [t["text"] for t in p.mine("bob")[1]["tasks"]] == ["direct"]
    assert p.make("alice", target_user_id="mallory")[0] == 404
    assert p.make("alice", target_user_id="bob", lease_seconds=99 * 86400)[0] == 400
    assert p.make("bob", target_user_id="carol", lease_seconds=500)[0] == 400           # only the owner sets lease and policy
    assert p.make("alice", target_user_id="bob", no_response_policy="drop")[0] == 400


def test_the_assignee_accepts_and_completes_and_only_the_assignee_may(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    tid = p.make("alice", target_role="tester")[1]["task_id"]
    assert p.respond("bob", tid, "done")[0] == 404 and p.respond("erin", tid, "done")[0] == 404
    assert p.respond("dave", tid, "bogus")[0] == 400
    assert p.respond("dave", tid, "done")[0] == 400                                     # accept first
    s, out = p.respond("dave", tid, "accepted")
    assert s == 200 and out["status"] == "accepted"
    assert p.respond("dave", tid, "accepted")[0] == 400
    s, out = p.respond("dave", tid, "done", "all green")
    assert s == 200 and out["status"] == "done"
    assert p.tasks("alice")[1]["tasks"][0]["status"] == "done" and p.tasks("alice")[1]["tasks"][0]["result"] == "all green"
    assert p.respond("dave", tid, "done", "all green")[1]["replayed"] is True
    tid2 = p.make("alice", target_role="tester")[1]["task_id"]
    assert p.respond("dave", tid2, "declined")[1]["status"] == "declined"
    assert p.mine("dave")[1]["tasks"] == []                                             # finished work leaves the queue


def test_the_creator_or_owner_can_cancel_a_person_task(rp):
    p = rp
    crew(p)
    tid = p.make("alice", target_user_id="bob")[1]["task_id"]
    assert p.c("carol", "POST", f"{BASE}/rooms/{p.room}/tasks/{tid}/cancel", {})[0] in (403, 404)
    assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/tasks/{tid}/cancel", {})[0] == 200
    assert p.mine("bob")[1]["tasks"] == []


# ============================================================== (c) the timer
def sweeps(p):
    return {k: len(p.events(k)) for k in ("task_reminded", "task_reassigned", "task_escalated", "task_lease_extended")}


def test_reminders_fire_once_each_before_expiry_then_the_default_policy_escalates(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    tid = p.make("alice", target_role="tester")[1]["task_id"]
    assert p.sweep()["reminded"] == 0
    p.clock.t += 500
    assert p.sweep()["reminded"] == 1 and p.sweep()["reminded"] == 0                    # idempotent
    mine = p.mine("dave")[1]["tasks"][0]
    assert mine["reminders_sent"] == 1
    p.clock.t += 400
    assert p.sweep()["reminded"] == 1 and p.sweep() == {"active": True, "reminded": 0, "reassigned": 0, "escalated": 0, "extended": 0}
    assert sweeps(p) == {"task_reminded": 2, "task_reassigned": 0, "task_escalated": 0, "task_lease_extended": 0}
    p.clock.t += 100                                                                    # lease is over
    assert p.sweep()["escalated"] == 1 and p.sweep()["escalated"] == 0
    assert sweeps(p)["task_escalated"] == 1
    owner_view = [t for t in p.tasks("alice")[1]["tasks"] if t["task_id"] == tid][0]
    assert owner_view["status"] == "blocked"
    esc = p.events("task_escalated")[0]["payload"]
    assert esc["task_id"] == tid and esc["reason"] == "no_response" and esc["policy"] == "escalate"
    assert p.respond("dave", tid, "accepted")[0] == 400                                 # blocked: the owner decides
    assert p.c("alice", "POST", f"{BASE}/rooms/{p.room}/tasks/{tid}/cancel", {})[0] == 200


def test_a_sweep_that_wakes_after_expiry_sends_no_late_reminder(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.make("alice", target_role="tester")
    p.clock.t += 2000
    out = p.sweep()
    assert out["reminded"] == 0 and out["escalated"] == 1


def test_an_accepted_task_is_not_chased(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    tid = p.make("alice", target_role="tester")[1]["task_id"]
    p.clock.t += 600
    p.sweep()
    p.respond("dave", tid, "accepted")
    p.clock.t += 600
    assert p.sweep() == {"active": True, "reminded": 0, "reassigned": 0, "escalated": 0, "extended": 0}


def test_reassign_moves_the_role_once_then_escalates(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.assign(team, "tester", "user:erin")
    assert p.policy(team, "reassign")[1]["no_response_policy"] == "reassign"
    s, out = p.make("alice", target_role="tester", text="verify")
    first, tid = out["routed_to"], out["task_id"]
    other = "user:erin" if first == "user:dave" else "user:dave"
    p.clock.t += 1000
    assert p.sweep()["reassigned"] == 1 and p.sweep()["reassigned"] == 0
    moved = p.events("task_reassigned")[0]["payload"]
    assert moved["from"] == first and moved["to"] == other and moved["task_id"] == tid
    assert [t["task_id"] for t in p.mine(other[5:])[1]["tasks"]] == [tid] and p.mine(first[5:])[1]["tasks"] == []
    assert p.respond(first[5:], tid, "accepted")[0] == 404                              # the old holder is out
    p.clock.t += 1000
    assert p.sweep()["escalated"] == 1 and p.sweep()["reassigned"] == 0                 # once, then escalate
    assert [t for t in p.tasks("alice")[1]["tasks"] if t["task_id"] == tid][0]["status"] == "blocked"


def test_reassign_with_no_other_holder_and_a_direct_assignment_escalate(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.policy(team, "reassign")
    p.make("alice", target_role="tester")
    p.make("alice", target_user_id="bob", no_response_policy="reassign")
    p.clock.t += 1000
    out = p.sweep()
    assert out["reassigned"] == 0 and out["escalated"] == 2


def test_reassign_can_land_on_an_agent_holder_which_then_owns_the_task_like_any_agent_task(rp):
    p = rp
    team, (a1, a2) = crew(p)
    p.assign(team, "implementer", "user:dave")
    p.policy(team, "reassign")
    # make dave the only idle holder so the task is routed to him: load the agents first
    for a in (a1, a2):
        p.make("alice", target_agent_id=a["agent_id"], text="busy")
    s, out = p.make("alice", target_role="implementer")
    assert out["routed_to"] == "user:dave"
    p.clock.t += 1000
    assert p.sweep()["reassigned"] == 1
    got = [t for a in (a1, a2) for t in p.inbox(a) if t["task_id"] == out["task_id"]]
    assert len(got) == 1 and p.mine("dave")[1]["tasks"] == []


def test_wait_extends_but_never_beyond_the_hard_expiry_or_the_extension_cap(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.policy(team, "wait")
    tid = p.make("alice", target_role="tester")[1]["task_id"]
    p.clock.t += 1000
    assert p.sweep()["extended"] == 1
    p.clock.t += 1000
    assert p.sweep()["extended"] == 1
    p.clock.t += 1000                                                                   # cap of 2 extensions reached
    out = p.sweep()
    assert out["extended"] == 0 and out["escalated"] == 1
    assert sweeps(p)["task_lease_extended"] == 2
    ends = [e["payload"]["new_expires_epoch"] for e in p.events("task_lease_extended")]
    hard = p.events("task_created")[0]["payload"]["hard_expiry_epoch"]
    assert ends == sorted(ends) and max(ends) <= hard
    # hard expiry: past it the task simply expires and the sweep leaves it alone
    p.clock.t += 5000
    assert [t for t in p.tasks("alice")[1]["tasks"] if t["task_id"] == tid][0]["status"] in ("blocked", "expired")
    assert p.sweep()["escalated"] == 0


def test_wait_stops_at_the_hard_expiry(rp, tmp_path):
    p = rp
    p.svc.team_roles = normalize_team_roles({**ROLES, "person_lease_seconds": 1000, "hard_expiry_seconds": 1500, "max_lease_extensions": 5})
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.policy(team, "wait")
    p.make("alice", target_role="tester")
    p.clock.t += 1000
    assert p.sweep()["extended"] == 1
    new_end = p.events("task_lease_extended")[0]["payload"]["new_expires_epoch"]
    hard = p.events("task_created")[0]["payload"]["hard_expiry_epoch"]
    assert new_end == hard                                                              # clipped, never beyond
    p.clock.t = hard
    assert p.sweep()["extended"] == 0


def test_the_timer_state_is_in_the_log_so_a_restart_neither_repeats_nor_loses_anything(rp, tmp_path):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    tid = p.make("alice", target_role="tester")[1]["task_id"]
    p.clock.t += 600
    p.sweep()
    again = reborn(p, tmp_path)
    assert again.sweep_team_roles()["reminded"] == 0                                    # the reminder is not sent twice
    p.clock.t += 300                                                                    # same clock for both: 900s, the 90% mark
    out = again.sweep_team_roles()
    assert out["reminded"] == 1 and out["escalated"] == 0
    p.clock.t += 100
    assert again.sweep_team_roles()["escalated"] == 1
    third = _service(tmp_path, p.clock, name="third")
    third.hydrate(again._log)
    assert third.sweep_team_roles() == {"active": True, "reminded": 0, "reassigned": 0, "escalated": 0, "extended": 0}
    assert third._tasks[tid]["status"] == "blocked"


def test_a_holder_who_leaves_or_is_cleared_is_fed_into_the_policy_at_the_next_sweep(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.assign(team, "tester", "user:erin")
    p.policy(team, "reassign")
    a = p.make("alice", target_role="tester")[1]
    holder = p.events("task_created")[0]["payload"]["routed_to"][5:]
    p.clear(team, "tester", "user:" + holder)
    assert p.sweep()["reassigned"] == 1                                                 # no waiting for the lease
    now_with = "erin" if holder == "dave" else "dave"
    assert [t["task_id"] for t in p.mine(now_with)[1]["tasks"]] == [a["task_id"]]
    # a member removed from the room while holding open work
    p.c("alice", "POST", f"{BASE}/rooms/{p.room}/members/{now_with}/remove", {})
    out = p.sweep()
    assert out["escalated"] == 1                                                        # reassign already used: escalate


def test_the_sweep_is_skipped_while_degraded_and_never_raises_on_a_full_log(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    p.make("alice", target_role="tester")
    p.clock.t += 600
    p.svc.degraded = True
    assert p.sweep() == {"active": True, "degraded": True}
    p.svc.degraded = False
    assert p.sweep()["reminded"] == 1


def test_the_operator_sees_an_overdue_person_task_in_attention_and_the_assignee_sees_theirs(rp):
    p = rp
    from herdr_engineering.control_plane.collab_attention import normalize_insights
    p.svc.insights = normalize_insights({"enabled": True})
    team, _ = crew(p)
    p.assign(team, "tester", "user:bob")
    tid = p.make("alice", target_role="tester", text="secret wording")[1]["task_id"]
    mine = p.c("bob", "GET", f"{BASE}/rooms/{p.room}/attention")[1]["items"]
    got = [i for i in mine if i["category"] == "task_assigned"]
    assert len(got) == 1 and got[0]["detail"]["task_id"] == tid and "secret wording" not in json.dumps(got)
    assert not [i for i in p.c("alice", "GET", f"{BASE}/rooms/{p.room}/attention")[1]["items"] if i["category"] == "task_assigned"]
    p.clock.t += 1000
    p.sweep()
    owner = p.c("alice", "GET", f"{BASE}/rooms/{p.room}/attention")[1]["items"]
    over = [i for i in owner if i["category"] == "task_overdue"]
    assert len(over) == 1 and over[0]["for_you"] is True and over[0]["detail"]["task_id"] == tid
    assert "secret wording" not in json.dumps(over)
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/attention")[0] == 404             # a guest has my-tasks, not attention


# ============================================================== (e) what an external person sees
def test_an_external_person_sees_only_their_own_tasks_and_what_is_shared_in(rp):
    p = rp
    team, (a1, _) = crew(p)
    p.assign(team, "tester", "user:dave")
    p.assign(team, "tester", "user:erin")
    p.make("alice", target_agent_id=a1["agent_id"], text="internal chore")
    t1 = p.make("alice", target_user_id="dave", text="for dave")[1]["task_id"]
    t2 = p.make("alice", target_user_id="erin", text="for erin")[1]["task_id"]
    assert [t["task_id"] for t in p.mine("dave")[1]["tasks"]] == [t1]
    assert [t["task_id"] for t in p.mine("erin")[1]["tasks"]] == [t2]
    assert p.tasks("dave")[1]["tasks"] == []
    kinds = p.feed_kinds("dave")
    assert "task_created" not in kinds and "task_updated" not in kinds
    st = p.c("dave", "GET", f"{BASE}/rooms/{p.room}/state")[1]
    assert st["tasks"] == [] and st["people"] == [] and st["agents"] == [] and st["flow"] is None
    blob = json.dumps(st) + json.dumps(p.c("dave", "GET", f"{BASE}/rooms/{p.room}/events?cursor=0")[1])
    assert "internal chore" not in blob and "for erin" not in blob
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/agents")[0] == 403
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/flow")[0] in (403, 404)
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/teams")[0] == 404
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/teams/{team}/roles")[0] == 404


def test_member_visibility_restores_exactly_the_task_list_and_task_events_and_never_another_persons_task(rp):
    p = rp
    team, (a1, _) = crew(p)
    p.assign(team, "tester", "user:dave")
    p.assign(team, "tester", "user:erin")
    p.make("alice", target_agent_id=a1["agent_id"], text="internal chore")
    p.make("alice", target_user_id="erin", text="for erin")
    assert p.visibility("alice", True)[1]["enabled"] is True
    assert p.visibility("alice", True)[1]["replayed"] is True
    assert p.c("dave", "GET", f"{BASE}/rooms/{p.room}/member-visibility")[1]["enabled"] is True
    assert [t["text"] for t in p.tasks("dave")[1]["tasks"]] == ["internal chore"]       # the agent task, not erin's
    kinds = p.feed_kinds("dave")
    assert "task_created" in kinds
    assert "for erin" not in json.dumps(p.c("dave", "GET", f"{BASE}/rooms/{p.room}/events?cursor=0")[1])
    st = p.c("dave", "GET", f"{BASE}/rooms/{p.room}/state")[1]
    assert st["people"] == [] and st["agents"] == [] and st["flow"] is None             # roster, agents and graph stay hidden
    assert "for erin" not in json.dumps(st)
    assert p.visibility("alice", False)[1]["enabled"] is False
    assert p.tasks("dave")[1]["tasks"] == []


# ============================================================== (d) independent review across agents and people
def test_a_person_who_did_the_work_or_handed_it_out_cannot_review_it(rp):
    p = rp
    team, _ = crew(p)
    for who in ("bob", "carol"):
        p.assign(team, "reviewer", f"user:{who}")
    p.assign(team, "implementer", "user:bob")
    work = p.make("alice", target_user_id="bob", text="implement")[1]["task_id"]
    # routed review: bob did the work, so only carol qualifies
    s, out = p.make("alice", target_role="reviewer", reviews_task_id=work, text="review")
    assert s == 201 and out["routed_to"] == "user:carol"
    # explicit target: bob reviewing his own work is refused
    s, out = p.make("alice", target_user_id="bob", reviews_task_id=work, text="review")
    assert s == 400 and "did the work" in out["error"]
    # the person who handed the work out cannot review it: carol creates the work, so she is out and bob is too
    work2 = p.make("alice", target_user_id="bob", text="more")[1]["task_id"]
    s, out = p.make("alice", target_user_id="alice", reviews_task_id=work2, text="r")
    assert s in (400, 404)


def test_the_person_who_assigned_the_work_is_not_an_independent_reviewer(rp):
    p = rp
    team, (a1, _) = crew(p)
    p.assign(team, "reviewer", "user:bob")
    p.assign(team, "reviewer", "user:carol")
    work = p.make("bob", target_agent_id=a1["agent_id"], text="bob hands this out")
    assert work[0] == 201
    wid = work[1]["task_id"]
    s, out = p.make("alice", target_user_id="bob", reviews_task_id=wid)
    assert s == 400 and "handed out" in out["error"]
    s, out = p.make("alice", target_role="reviewer", reviews_task_id=wid)
    assert s == 201 and out["routed_to"] == "user:carol"
    p.clear(team, "reviewer", "user:carol")
    s, out = p.make("alice", target_role="reviewer", reviews_task_id=wid)
    assert s == 409 and out["code"] == "no_role_holder"                                  # fail closed: never fall through


def test_an_agent_who_did_the_work_still_cannot_review_it_and_other_agents_still_can(rp):
    p = rp
    team, (a1, a2) = crew(p)
    work = p.make("alice", target_agent_id=a1["agent_id"], text="agent work")[1]["task_id"]
    s, out = p.make("alice", target_agent_id=a1["agent_id"], reviews_task_id=work)
    assert s == 400 and "did the work" in out["error"]
    assert p.make("alice", target_agent_id=a2["agent_id"], reviews_task_id=work)[0] == 201
    # a person reviewing an agent's work is fine, and an agent reviewing a person's work too
    assert p.make("alice", target_user_id="carol", reviews_task_id=work)[0] == 201
    pwork = p.make("alice", target_user_id="bob", text="person work")[1]["task_id"]
    assert p.make("alice", target_agent_id=a1["agent_id"], reviews_task_id=pwork)[0] == 201


def test_a_configured_reviewer_group_counts_once(rp):
    p = rp
    p.svc.team_roles = normalize_team_roles({**ROLES, "reviewer_groups": [["user:bob", "user:carol"]]})
    team, _ = crew(p)
    work = p.make("alice", target_user_id="bob", text="work")[1]["task_id"]
    s, out = p.make("alice", target_user_id="carol", reviews_task_id=work)
    assert s == 400 and "same reviewer group" in out["error"]
    assert p.make("alice", target_user_id="erin", reviews_task_id=work)[0] == 201


def test_with_the_flag_off_the_old_review_rule_is_unchanged(rp):
    p = rp
    team, (a1, a2) = crew(p)
    p.svc.team_roles = normalize_team_roles(None)
    work = p.make("alice", target_agent_id=a1["agent_id"], text="w")[1]["task_id"]
    s, out = p.make("alice", target_agent_id=a1["agent_id"], reviews_task_id=work)
    assert s == 400 and "did the work" in out["error"]
    assert p.make("alice", target_agent_id=a2["agent_id"], reviews_task_id=work)[0] == 201


# ============================================================== the projection never leaks a person task
def test_the_work_graph_projection_of_a_person_task_carries_no_text_and_no_assignee(rp):
    p = rp
    team, _ = crew(p)
    p.assign(team, "tester", "user:dave")
    tid = p.make("alice", target_role="tester", text="private wording here")[1]["task_id"]
    p.clock.t += 1000
    p.sweep()
    p.sync_graph()                                                                      # the projection must accept every new event
    from tests.control_plane.test_work_graph_api import call as keyed_call
    s, out, _ = keyed_call(p.srv, f"/api/v1/graph/work/entity?ref=task:{tid}", method="GET")
    assert s == 200, out
    assert "private wording here" not in json.dumps(out) and "dave" not in json.dumps(out)
