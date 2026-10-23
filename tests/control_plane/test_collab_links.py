# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Room <-> workspace links, the primary-room rule, mission links and the terminal boundary.

Cast as in test_collab_tasks: alice owns room and room2; bob contributor; carol viewer; dave guest;
mallory is in no room."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import CollabError, CollabService, StaticTokenAuthenticator
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_tasks import USERS, W, code, svc, user  # noqa: F401


@pytest.fixture
def w(svc):
    w = W(svc)
    w.op = user(svc, "alice")                  # the dashboard operator is a separate session of the same person
    w.op.admin = True
    svc.register_host(w.op, "bender", {"owners": ["alice", "bob"]})
    return w


def link(w, room=None, ws="ws-1", host="bender", who=None, **kw):
    return w.s.link_workspace(who or w.alice, room or w.room, {"host": host, "workspace_id": ws, **kw})


def test_only_owner_links_and_default_grants_are_minimal(w):
    for who in (w.bob, w.carol, w.dave):
        assert code(w.s.link_workspace, who, w.room, {"host": "bender", "workspace_id": "ws-1"}) == 403
    assert code(w.s.link_workspace, w.mallory, w.room, {"host": "bender", "workspace_id": "ws-1"}) == 404
    out = link(w)
    assert out["grants"] == ["status"] and out["is_primary"] is False and out["can_terminal"] is False
    assert out["linked_by"] == "alice" and out["host"] == "bender"
    assert code(w.s.link_workspace, w.alice, w.room, {"host": "bender", "workspace_id": "ws-1"}) == 409
    for bad in ({"host": "Bad Host", "workspace_id": "x"}, {"host": "bender", "workspace_id": "a b"},
                {"host": "bender", "workspace_id": "x", "grants": ["root"]},
                {"host": "bender", "workspace_id": "x", "grants": ["terminal"]},
                {"host": "bender", "workspace_id": "x", "is_primary": "yes"}):
        assert code(w.s.link_workspace, w.alice, w.room, bad) == 400


def test_agents_cannot_link(w):
    agent, _ = w.agent("alice", ("read", "post", "execute", "instruct"))
    assert code(w.s.link_workspace, agent, w.room, {"host": "bender", "workspace_id": "ws-1"}) == 403


def test_primary_rule_conflict_and_explicit_demote(w):
    a = link(w, is_primary=True)
    assert a["is_primary"] is True
    b = link(w, room=w.room2)                                   # same workspace in a second room
    assert b["is_primary"] is False
    assert code(w.s.set_workspace_primary, w.alice, w.room2, b["link_id"], {}) == 409
    assert code(w.s.link_workspace, w.alice, w.room2, {"host": "bender", "workspace_id": "ws-1",
                                                      "is_primary": True}) == 409
    out = w.s.set_workspace_primary(w.alice, w.room2, b["link_id"], {"demote_other": True})
    assert out["is_primary"] is True
    assert w.s.list_workspace_links(w.alice, w.room)["links"][0]["is_primary"] is False
    assert len([k for k in w.s._links.values() if k["is_primary"]]) == 1
    # a different workspace is independent
    c = link(w, ws="ws-2", is_primary=True)
    assert c["is_primary"] is True
    # explicit demote by the owner, then unlink frees the slot
    assert w.s.set_workspace_primary(w.alice, w.room, c["link_id"], {"primary": False})["is_primary"] is False


def test_demoting_another_rooms_primary_needs_that_rooms_owner(w):
    s = w.s
    pid = s.create_project(w.bob, {"name": "bobs"})["project_id"]
    bob_room = s.create_room(w.bob, pid, {"name": "b"})["room_id"]
    first = s.link_workspace(w.bob, bob_room, {"host": "bender", "workspace_id": "ws-9", "is_primary": True})
    mine = link(w, ws="ws-9")
    with pytest.raises(CollabError) as e:
        s.set_workspace_primary(w.alice, w.room, mine["link_id"], {"demote_other": True})
    err = e.value
    assert err.status == 409 and bob_room not in str(err) and first["link_id"] not in str(err)
    # the operator may
    assert s.set_workspace_primary(w.op, w.room, mine["link_id"], {"demote_other": True})["is_primary"]
    assert s._links[first["link_id"]]["is_primary"] is False


def test_unlink_clears_primary_and_is_audited(w):
    a = link(w, is_primary=True)
    assert code(w.s.unlink_workspace, w.bob, w.room, a["link_id"]) == 403
    w.s.unlink_workspace(w.alice, w.room, a["link_id"])
    assert w.s.list_workspace_links(w.alice, w.room)["links"] == []
    assert code(w.s.unlink_workspace, w.alice, w.room, a["link_id"]) == 404
    assert link(w, room=w.room2, is_primary=True)["is_primary"] is True
    pid = next(iter(w.s._projects))
    actions = [e["action"] for e in w.s.audit(w.alice, pid, 0, 500)["events"]]
    assert "workspace.link" in actions and "workspace.unlink" in actions


def test_every_change_is_audited_including_denials(w):
    a = link(w)
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["status", "tabs"]})
    w.s.set_workspace_primary(w.alice, w.room, a["link_id"], {})
    w.s.set_workspace_primary(w.alice, w.room, a["link_id"], {"primary": False})
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-1"})
    w.s.unlink_mission(w.alice, w.room, "m-1")
    code(w.s.link_workspace, w.bob, w.room, {"host": "bender", "workspace_id": "ws-5"})
    pid = next(iter(w.s._projects))
    rows = w.s.audit(w.alice, pid, 0, 500)["events"]
    seen = {(e["action"], e["outcome"]) for e in rows}
    for act in ("workspace.link", "workspace.grants", "workspace.primary", "workspace.demote",
                "mission.link", "mission.unlink"):
        assert (act, "allowed") in seen
    assert ("workspace.link", "denied") in seen


def test_room_membership_alone_never_grants_terminal(w):
    a = link(w, grants=["status", "tabs", "panes"])
    for who in (w.alice, w.bob, w.carol):
        for act in ("status", "tabs", "panes"):
            assert w.s.authorize_workspace(who, w.room, a["link_id"], act)["allowed"]
        assert code(w.s.authorize_workspace, who, w.room, a["link_id"], "terminal") == 403
    assert code(w.s.authorize_workspace, w.dave, w.room, a["link_id"], "status") == 403   # guest sees nothing
    assert code(w.s.authorize_workspace, w.mallory, w.room, a["link_id"], "status") == 404
    assert code(w.s.authorize_workspace, w.alice, w.room, a["link_id"], "shell") == 400
    assert code(w.s.authorize_workspace, w.alice, w.room2, a["link_id"], "status") == 404  # wrong room
    assert code(w.s.authorize_workspace, w.alice, w.room, "nope", "status") == 404


def test_terminal_needs_explicit_grant_intersected_with_role_and_named_principals(w):
    a = link(w, grants=["status", "terminal"], terminal_principals=["user:alice", "user:bob"])
    assert w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")["allowed"]
    assert w.s.authorize_workspace(w.bob, w.room, a["link_id"], "terminal")["allowed"]
    assert code(w.s.authorize_workspace, w.carol, w.room, a["link_id"], "terminal") == 403   # not named
    # a viewer cannot even be named
    assert code(w.s.set_workspace_grants, w.alice, w.room, a["link_id"],
                {"grants": ["terminal"], "terminal_principals": ["user:carol"]}) == 400
    # removing the grant removes the terminal for everyone
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["status"]})
    assert code(w.s.authorize_workspace, w.alice, w.room, a["link_id"], "terminal") == 403
    # a named user who loses room membership loses the terminal
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"],
                             {"grants": ["terminal"], "terminal_principals": ["user:bob"]})
    w.s.remove_member(w.alice, w.room, "bob")
    assert code(w.s.authorize_workspace, w.bob, w.room, a["link_id"], "terminal") == 404


def test_agent_terminal_is_the_intersection_of_room_grant_link_grant_and_owner(w):
    ag, info = w.agent("bob", ("read", "post", "execute"))
    key = "agent:" + info["agent_id"]
    a = link(w, grants=["terminal"], terminal_principals=[key, "user:alice"])
    assert code(w.s.authorize_workspace, ag, w.room, a["link_id"], "terminal") == 403    # owner bob not named
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"],
                             {"grants": ["terminal"], "terminal_principals": [key, "user:bob"]})
    assert w.s.authorize_workspace(ag, w.room, a["link_id"], "terminal")["allowed"]
    # without the room's execute right the agent has no terminal even though it is named
    ag2, info2 = w.agent("bob", ("read", "post"))
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {
        "grants": ["terminal"], "terminal_principals": ["agent:" + info2["agent_id"], "user:bob"]})
    assert code(w.s.authorize_workspace, ag2, w.room, a["link_id"], "terminal") == 403
    # revoking the agent ends it
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"],
                             {"grants": ["terminal"], "terminal_principals": [key, "user:bob"]})
    w.s.revoke_agent(w.bob, info["agent_id"])
    with pytest.raises(Exception):      # noqa: B017  - credential no longer valid / no grant
        w.s.authorize_workspace(ag, w.room, a["link_id"], "terminal")


def test_cross_room_leakage(w):
    a = link(w, ws="secret-ws", grants=["status", "tabs"])
    link(w, room=w.room2, ws="other-ws")
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-secret", "members": ["bob"]})
    w.s.add_member(w.alice, w.room2, {"user_id": "mallory", "role": "contributor"})
    mal = w.mallory
    got = w.s.list_workspace_links(mal, w.room2)["links"]
    assert [k["workspace_id"] for k in got] == ["other-ws"]
    assert code(w.s.list_workspace_links, mal, w.room) == 404
    assert code(w.s.list_room_missions, mal, w.room) == 404
    assert code(w.s.authorize_workspace, mal, w.room, a["link_id"], "status") == 404
    assert code(w.s.unlink_workspace, mal, w.room, a["link_id"]) == 404
    st = w.s.room_state(mal, w.room2, -1, 0, 0)
    blob = repr(st)
    assert "secret-ws" not in blob and "m-secret" not in blob and a["link_id"] not in blob
    # a link id from another room is a plain 404 for its own room's owner too
    assert code(w.s.unlink_workspace, w.alice, w.room2, a["link_id"]) == 404


def test_room_state_carries_links_only_for_authorised_members(w):
    a = link(w, grants=["status"])
    link(w, ws="hidden", grants=[])
    st = w.s.room_state(w.carol, w.room, -1, 0, 0)
    assert [k["workspace_id"] for k in st["links"]] == ["ws-1"] and "terminal_principals" not in st["links"][0]
    owner = w.s.room_state(w.alice, w.room, -1, 0, 0)
    assert {k["workspace_id"] for k in owner["links"]} == {"ws-1", "hidden"}
    assert "terminal_principals" in owner["links"][0]
    assert w.s.room_state(w.dave, w.room, -1, 0, 0)["links"] is None
    ag, _ = w.agent("alice", ("read", "post"))
    assert w.s.room_state(ag, w.room, -1, 0, 0)["links"] is None
    # a change wakes the room's long-poll
    seq = w.s.room_state(w.carol, w.room, -1, 0, 0)["seq"]
    assert w.s.room_state(w.carol, w.room, seq, 0, 0)["changed"] is False
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["status", "panes"]})
    st = w.s.room_state(w.carol, w.room, seq, 0, 0)
    assert st["changed"] and "panes" in st["links"][0]["grants"]


def test_mission_links_have_scoped_membership(w):
    assert code(w.s.link_mission, w.bob, w.room, {"mission_id": "m-1"}) == 403
    assert code(w.s.link_mission, w.alice, w.room, {"mission_id": "bad id"}) == 400
    assert code(w.s.link_mission, w.alice, w.room, {"mission_id": "m-1", "members": ["dave"]}) == 400   # guest
    assert code(w.s.link_mission, w.alice, w.room, {"mission_id": "m-1", "members": ["mallory"]}) == 400
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-1", "members": ["bob"]})
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-2"})
    assert code(w.s.link_mission, w.alice, w.room, {"mission_id": "m-1"}) == 409
    assert {m["mission_id"] for m in w.s.list_room_missions(w.alice, w.room)["missions"]} == {"m-1", "m-2"}
    assert [m["mission_id"] for m in w.s.list_room_missions(w.bob, w.room)["missions"]] == ["m-1"]
    assert w.s.list_room_missions(w.carol, w.room)["missions"] == []
    assert code(w.s.list_room_missions, w.dave, w.room) == 403
    # losing room membership ends mission visibility
    w.s.remove_member(w.alice, w.room, "bob")
    assert code(w.s.list_room_missions, w.bob, w.room) == 404
    assert code(w.s.unlink_mission, w.bob, w.room, "m-1") == 404
    w.s.unlink_mission(w.alice, w.room, "m-1")
    assert code(w.s.unlink_mission, w.alice, w.room, "m-1") == 404


def test_links_survive_hydrate_and_replay_primary(w):
    a = link(w, is_primary=True)
    b = link(w, room=w.room2)
    w.s.set_workspace_primary(w.alice, w.room2, b["link_id"], {"demote_other": True})
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-1", "members": ["bob"]})
    w.s.unlink_workspace(w.alice, w.room, a["link_id"])
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    fresh.hydrate(w.s._log)
    assert set(fresh._links) == {b["link_id"]} and fresh._links[b["link_id"]]["is_primary"] is True
    assert fresh._primary == {("bender", "ws-1"): b["link_id"]}
    assert ("%s" % w.room, "m-1") in fresh._missions


def test_http_dispatch_routes_and_conflict_status(w):
    d = w.s.dispatch
    tok = f"Bearer {USERS['alice']}"
    st, out = d("POST", ["rooms", w.room, "workspaces"], {}, {"host": "bender", "workspace_id": "ws-1",
                                                             "is_primary": True}, tok, None)
    assert st == 201 and out["is_primary"]
    st, out2 = d("POST", ["rooms", w.room2, "workspaces"], {}, {"host": "bender", "workspace_id": "ws-1"}, tok, None)
    st, body = d("POST", ["rooms", w.room2, "workspaces", out2["link_id"], "primary"], {}, {}, tok, None)
    assert st == 409
    st, body = d("GET", ["rooms", w.room, "workspaces"], {}, {}, tok, None)
    assert st == 200 and len(body["links"]) == 1
    st, body = d("POST", ["rooms", w.room, "workspaces", out["link_id"], "authorize"], {}, {"action": "terminal"}, tok, None)
    assert st == 403
    st, body = d("POST", ["rooms", w.room, "missions"], {}, {"mission_id": "m-1"}, tok, None)
    assert st == 201
    st, body = d("POST", ["rooms", w.room, "workspaces", out["link_id"], "unlink"], {}, {}, tok, None)
    assert st == 200


# ---------------------------------------------------------------- review fixes: host proof
def audit_actions(w, outcome=None):
    pid = next(iter(w.s._projects))
    return [e["action"] for e in w.s.audit(w.alice, pid, 0, 500)["events"] if outcome in (None, e["outcome"])]


def test_terminal_and_primary_need_a_registered_host_and_metadata_does_not(w):
    # unregistered host: metadata links are fine but marked unverified; terminal and primary are refused
    meta = link(w, host="ghost", ws="victim-ws", grants=["status", "tabs"])
    assert meta["host_verified"] is False
    assert code(w.s.link_workspace, w.alice, w.room, {
        "host": "ghost", "workspace_id": "x", "grants": ["terminal"], "terminal_principals": ["user:alice"]}) == 403
    assert code(w.s.link_workspace, w.alice, w.room, {"host": "ghost", "workspace_id": "x", "is_primary": True}) == 409
    # the operator cannot vouch for an unregistered host either (fail closed)
    assert code(w.s.link_workspace, w.op, w.room2, {"host": "ghost", "workspace_id": "x", "is_primary": True}) == 409
    assert code(w.s.set_workspace_grants, w.alice, w.room, meta["link_id"],
                {"grants": ["terminal"], "terminal_principals": ["user:alice"]}) == 403
    # a registered host: owner proves control
    ok = link(w, ws="mine", grants=["terminal"], terminal_principals=["user:alice"])
    assert ok["host_verified"] is True and w.s.authorize_workspace(w.alice, w.room, ok["link_id"], "terminal")["allowed"]


def test_a_room_owner_cannot_take_a_host_they_do_not_own(w):
    w.s.register_host(w.op, "beast", {"owners": ["bob"]})
    assert code(w.s.link_workspace, w.alice, w.room, {
        "host": "beast", "workspace_id": "victim-ws", "grants": ["terminal"],
        "terminal_principals": ["user:alice"]}) == 403
    with pytest.raises(CollabError) as taken:
        w.s.link_workspace(w.alice, w.room, {"host": "beast", "workspace_id": "w", "is_primary": True})
    pid = w.s.create_project(w.bob, {"name": "bp"})["project_id"]
    bob_room = w.s.create_room(w.bob, pid, {"name": "b"})["room_id"]
    w.s.link_workspace(w.bob, bob_room, {"host": "beast", "workspace_id": "w2", "is_primary": True})
    with pytest.raises(CollabError) as other:
        w.s.link_workspace(w.alice, w.room, {"host": "beast", "workspace_id": "w2", "is_primary": True})
    # 'not permitted' and 'already taken' are the same answer
    assert (taken.value.status, str(taken.value)) == (other.value.status, str(other.value)) == (409, str(other.value))


def test_only_the_operator_edits_the_registry(w):
    for who in (w.alice, w.bob):
        assert code(w.s.register_host, who, "beast", {"owners": ["bob"]}) == 403
        assert code(w.s.list_host_registry, who) == 403
    assert code(w.s.register_host, w.op, "beast", {"owners": ["nobody"]}) == 404
    assert code(w.s.register_host, w.op, "Bad Host", {"owners": ["bob"]}) == 400


def test_host_proof_is_rechecked_and_ends_when_the_host_changes(w):
    a = link(w, grants=["terminal"], terminal_principals=["user:alice"], is_primary=True)
    assert w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")["allowed"]
    w.s.register_host(w.op, "bender", {"owners": ["bob"]})           # alice no longer owns it
    assert code(w.s.authorize_workspace, w.alice, w.room, a["link_id"], "terminal") == 403
    cur = w.s.list_workspace_links(w.alice, w.room)["links"][0]
    assert cur["host_verified"] is False and cur["is_primary"] is False and cur["can_terminal"] is False
    w.s.register_host(w.op, "bender", {"owners": ["alice"]})         # even re-owning needs an explicit re-grant
    assert code(w.s.authorize_workspace, w.alice, w.room, a["link_id"], "terminal") == 403
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["terminal"], "terminal_principals": ["user:alice"]})
    assert w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")["allowed"]
    w.s.register_host(w.op, "bender", {"owners": []})                # unregistered
    assert code(w.s.authorize_workspace, w.alice, w.room, a["link_id"], "terminal") == 403


# ---------------------------------------------------------------- review fixes: log growth
def test_every_mutating_call_is_throttled_per_person_and_per_room(w):
    a = link(w, grants=["status", "tabs"])
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-x"})
    seen = set()
    for i in range(130):
        try:
            w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["status", "tabs"] if i % 2 else ["status"]})
        except CollabError as e:
            seen.add(e.status)
    assert 429 in seen
    # the other calls draw on the same budget
    for fn, args in ((w.s.unlink_mission, (w.alice, w.room, "m-x")),
                     (w.s.set_workspace_primary, (w.alice, w.room, a["link_id"], {"primary": True})),
                     (w.s.unlink_workspace, (w.alice, w.room, a["link_id"]))):
        assert code(fn, *args) == 429
    n = len(w.s._log)
    for _ in range(50):
        code(w.s.set_workspace_grants, w.alice, w.room, a["link_id"], {"grants": ["status"]})
    assert len(w.s._log) == n                                   # refused calls write nothing


def test_one_room_cannot_exceed_its_hourly_change_budget(w):
    w.s._hits["wsroom:" + w.room] = [w.s.clock.t] * 299
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-1"})
    assert code(w.s.link_mission, w.alice, w.room, {"mission_id": "m-2"}) == 429
    w.s.clock.t += 3601
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-2"})


# ---------------------------------------------------------------- review fixes: atomic primary
def test_demote_and_claim_is_one_event_and_a_failed_write_changes_nothing(w):
    a = link(w, is_primary=True)
    b = link(w, room=w.room2)
    n = len(w.s._log)
    w.s.set_workspace_primary(w.alice, w.room2, b["link_id"], {"demote_other": True})
    assert len(w.s._log) == n + 1                                # demotion and claim share one event

    class Down:
        def record_required(self, ev):
            raise OSError("disk")
    w.s._sink = Down()
    assert code(w.s.set_workspace_primary, w.alice, w.room, a["link_id"], {"demote_other": True}) == 503
    assert w.s._links[b["link_id"]]["is_primary"] is True and w.s._links[a["link_id"]]["is_primary"] is False


# ---------------------------------------------------------------- review fixes: stale principals
def test_removed_members_are_pruned_and_readding_does_not_restore_the_terminal(w):
    a = link(w, grants=["terminal"], terminal_principals=["user:alice", "user:bob"])
    w.s.remove_member(w.alice, w.room, "bob")
    assert w.s._links[a["link_id"]]["terminal_principals"] == ["user:alice"]
    w.s.add_member(w.alice, w.room, {"user_id": "bob", "role": "contributor"})
    assert code(w.s.authorize_workspace, w.bob, w.room, a["link_id"], "terminal") == 403
    # when the last named principal goes the terminal grant goes with it
    b = link(w, ws="solo", grants=["terminal"], terminal_principals=["user:bob"])
    w.s.remove_member(w.alice, w.room, "bob")
    assert "terminal" not in w.s._links[b["link_id"]]["grants"]


def test_a_revoked_agent_is_pruned_from_terminal_principals(w):
    ag, info = w.agent("bob", ("read", "post", "execute"))
    a = link(w, grants=["terminal"], terminal_principals=["agent:" + info["agent_id"], "user:bob"])
    w.s.remove_agent_from_room(w.alice, w.room, info["agent_id"])
    assert w.s._links[a["link_id"]]["terminal_principals"] == ["user:bob"]


# ---------------------------------------------------------------- review fixes: audit and wakeups
def test_allowed_terminal_use_is_audited_at_a_bounded_rate(w):
    a = link(w, grants=["terminal"], terminal_principals=["user:alice"])

    def count():
        return audit_actions(w, "allowed").count("workspace.terminal")
    for _ in range(20):
        w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")
    assert count() == 1
    w.s.clock.t += 601
    w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")
    assert count() == 2
    code(w.s.authorize_workspace, w.carol, w.room, a["link_id"], "terminal")   # a denial resets nothing for alice
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["terminal", "tabs"], "terminal_principals": ["user:alice"]})
    w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")          # grant changed: audited at once
    assert count() == 3
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["status"]})
    code(w.s.authorize_workspace, w.alice, w.room, a["link_id"], "terminal")
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["terminal"], "terminal_principals": ["user:alice"]})
    w.s.authorize_workspace(w.alice, w.room, a["link_id"], "terminal")          # first allow after a deny
    assert count() == 4


def test_link_changes_do_not_wake_guests_or_agents_and_agents_see_no_workspace(w):
    a = link(w)
    ag, _ = w.agent("alice", ("read", "post"))
    seqs = {n: w.s.room_state(p, w.room, -1, 0, 0)["seq"] for n, p in (("dave", w.dave), ("agent", ag), ("carol", w.carol))}
    w.s.set_workspace_grants(w.alice, w.room, a["link_id"], {"grants": ["status", "tabs"]})
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-1"})
    assert w.s.room_state(w.dave, w.room, seqs["dave"], 0, 0)["changed"] is False
    assert w.s.room_state(ag, w.room, seqs["agent"], 0, 0)["changed"] is False
    assert w.s.room_state(w.carol, w.room, seqs["carol"], 0, 0)["changed"] is True
    for act in ("status", "tabs", "panes"):
        assert code(w.s.authorize_workspace, ag, w.room, a["link_id"], act) == 403
