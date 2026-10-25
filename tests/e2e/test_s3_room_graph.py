"""ACCEPTANCE SCENARIO 3, server-contract half (no Pi): membership-scoped graph reads, workspace-link grants,
mission links and the T07 primary/multi-room rules, over real HTTP against a real server process.

The Pi-driven half (two hosts emitting spawn/handoff/consultation lineage into one room) lives in the Pi repo:
test/e2e/s3-room-two-workspaces.e2e.test.ts. Raw ingest here stands in for an emitter ONLY to place a node at an exact
scope; it is labelled as server-contract evidence, never as proof that Pi emits it.

Every refusal below is paired with a positive control (the same caller or object succeeds in the allowed case).
"""
from __future__ import annotations

from urllib.parse import quote

import pytest

NOT_FOUND = {"error": "not found"}


@pytest.fixture
def world(planes):
    plane = planes(keys={"em": "ingest"}, users=["alice", "bob", "viewer", "guest", "mallory", "dave"],
                   scopes={"em": {"rooms": ["*"], "missions": ["*"]}})
    plane.verify_features("alice")
    _, r1 = plane.make_room("alice", "p", "r1")
    _, r2 = plane.make_room("dave", "q", "r2")
    for user, role in (("bob", "contributor"), ("viewer", "viewer"), ("guest", "guest")):
        plane.add_member("alice", r1, user, role)

    class W:
        pass
    w = W()
    w.plane, w.r1, w.r2, w.seq = plane, r1, r2, 0
    return w


def put(w, node_id, scope="room", state="running", **ids):
    w.seq += 1
    env = w.plane.envelope(source="e2e-s3-em", seq=w.seq, entity_id=node_id, event_type="node.upsert",
                           payload={"node_type": node_id.split(":")[0], "node_id": node_id, "state": state},
                           scope=scope, **ids)
    r = w.plane.ingest("em", env)
    assert r.status == 200, r.body
    return r


def read(w, user, node_id):
    return w.plane.c("GET", f"/graph/nodes/{quote(node_id, safe='')}", user=user)


def listing(w, user, room, node_type="agent"):
    return w.plane.c("GET", f"/graph/nodes?node_type={node_type}&room_id={room}", user=user)


def listed(resp):
    return [n["id"] for n in resp.body.get("nodes", [])]


def test_membership_matrix_for_a_room_scoped_node(world):
    w = world
    put(w, "agent:lead", room_id=w.r1)
    # positive controls: every non-guest member reads the node, the listing and the lineage view
    for user in ("alice", "bob", "viewer"):
        assert read(w, user, "agent:lead").status == 200, user
        assert "agent:lead" in listed(listing(w, user, w.r1)), user
        assert w.plane.c("GET", f"/graph/lineage?room_id={w.r1}", user=user).status == 200
    # dave has a graph (his own room) - so the 404s below are authorization, not "no graph"
    assert listing(w, "dave", w.r2).status == 200
    missing = read(w, "mallory", "agent:never-existed")
    for user in ("guest", "mallory", "dave"):
        r = read(w, user, "agent:lead")
        assert r.status == 404 and r.body == NOT_FOUND, user
        assert r.body == read(w, user, "agent:never-existed").body == missing.body        # existing == missing
        for path in (f"/graph/nodes?node_type=agent&room_id={w.r1}", f"/graph/lineage?room_id={w.r1}",
                     f"/graph/lineage/timeline?room_id={w.r1}", f"/graph/lineage/events?room_id={w.r1}"):
            r = w.plane.c("GET", path, user=user)
            assert r.status == 404 and r.body == NOT_FOUND, (user, path)


def test_bare_room_scope_names_nobody_so_only_operator_keys_read_it(world):
    w = world
    # no room_id on the envelope: the label names nothing
    w.seq += 1
    env = w.plane.envelope(source="e2e-s3-em", seq=w.seq, entity_id="agent:bare", event_type="node.upsert",
                           payload={"node_type": "agent", "node_id": "agent:bare", "state": "running"}, scope="room")
    assert w.plane.ingest("em", env).status == 200
    assert w.plane.node("agent:bare", key="operator").status == 200          # positive control
    for user in ("alice", "bob"):
        assert read(w, user, "agent:bare").status == 404
        assert "agent:bare" not in listed(listing(w, user, w.r1))
    assert w.plane.node("agent:bare", key="viewer").status == 404


def test_workspace_scoped_nodes_need_a_link_that_grants_a_view_and_follow_grant_changes(world):
    w = world
    put(w, "agent:ws-agent", room_id=w.r1, workspace_id="ws-x")
    assert read(w, "alice", "agent:ws-agent").status == 404                  # no link yet
    link = w.plane.c("POST", f"/rooms/{w.r1}/workspaces", {"host": "hostx", "workspace_id": "ws-x", "grants": ["status"]}, user="alice")
    assert link.status == 201
    lid = link["link_id"]
    assert read(w, "alice", "agent:ws-agent").status == 200
    assert read(w, "bob", "agent:ws-agent").status == 200
    # a link grants nothing -> hidden at the very next read; restoring brings it back
    assert w.plane.c("POST", f"/rooms/{w.r1}/workspaces/{lid}/grants", {"grants": []}, user="alice").status == 200
    assert read(w, "alice", "agent:ws-agent").status == 404
    assert w.plane.c("POST", f"/rooms/{w.r1}/workspaces/{lid}/grants", {"grants": ["panes"]}, user="alice").status == 200
    assert read(w, "alice", "agent:ws-agent").status == 200
    # unlinking hides it too
    assert w.plane.c("POST", f"/rooms/{w.r1}/workspaces/{lid}/unlink", {}, user="alice").status == 200
    assert read(w, "alice", "agent:ws-agent").status == 404
    # only the room's owner manages links (positive: alice did above)
    assert w.plane.c("POST", f"/rooms/{w.r1}/workspaces", {"host": "hostx", "workspace_id": "ws-x"}, user="bob").status in (403, 404)


def test_one_workspace_may_be_linked_to_two_rooms_and_each_room_only_sees_its_own_lineage(world):
    w = world
    body = {"host": "hostshared", "workspace_id": "ws-shared", "grants": ["status", "tabs", "panes"]}
    assert w.plane.c("POST", f"/rooms/{w.r1}/workspaces", body, user="alice").status == 201
    assert w.plane.c("POST", f"/rooms/{w.r2}/workspaces", body, user="dave").status == 201          # multi-room: allowed
    assert w.plane.c("POST", f"/rooms/{w.r1}/workspaces", body, user="alice").status == 409         # but not twice in one room
    put(w, "agent:in-r1", room_id=w.r1, workspace_id="ws-shared")
    put(w, "agent:in-r2", room_id=w.r2, workspace_id="ws-shared")
    assert read(w, "alice", "agent:in-r1").status == 200 and read(w, "dave", "agent:in-r2").status == 200
    assert read(w, "dave", "agent:in-r1").status == 404 and read(w, "alice", "agent:in-r2").status == 404
    assert listed(listing(w, "alice", w.r1)) == ["agent:in-r1"]
    assert listed(listing(w, "dave", w.r2)) == ["agent:in-r2"]


def test_primary_room_claims_fail_closed_with_one_generic_answer(world):
    w = world
    # The combined server has no registered-host path for user tokens (the host registry is operator-listener only),
    # so every primary claim is refused with the SAME generic 409 whether or not a slot is taken: no existence oracle.
    a = w.plane.c("POST", f"/rooms/{w.r1}/workspaces", {"host": "hostp", "workspace_id": "ws-p", "is_primary": True}, user="alice")
    b = w.plane.c("POST", f"/rooms/{w.r2}/workspaces", {"host": "hostp2", "workspace_id": "ws-other", "is_primary": True}, user="dave")
    assert a.status == 409 and b.status == 409 and a.body == b.body, (a.body, b.body)
    # positive control: the same workspace links fine when not claiming primary, and metadata-only grants need no host
    ok = w.plane.c("POST", f"/rooms/{w.r1}/workspaces", {"host": "hostp", "workspace_id": "ws-p"}, user="alice")
    assert ok.status == 201 and ok["is_primary"] is False
    # asking for it afterwards fails closed the same way
    again = w.plane.c("POST", f"/rooms/{w.r1}/workspaces/{ok['link_id']}/primary", {"primary": True}, user="alice")
    assert again.status == 409 and again.body == a.body
    # a terminal grant needs a host registered to the caller: refused, and nothing was created
    t = w.plane.c("POST", f"/rooms/{w.r1}/workspaces", {"host": "hostq", "workspace_id": "ws-q", "grants": ["terminal"], "terminal_principals": ["user:alice"]}, user="alice")
    assert t.status == 403
    listing_ = w.plane.c("GET", f"/rooms/{w.r1}/workspaces", user="alice")
    assert all(x["workspace_id"] != "ws-q" for x in listing_.body.get("links", listing_.body.get("workspaces", [])))


def test_room_access_never_implies_a_terminal(world):
    w = world
    link = w.plane.c("POST", f"/rooms/{w.r1}/workspaces", {"host": "hostt", "workspace_id": "ws-t", "grants": ["status", "tabs"]}, user="alice")
    lid = link["link_id"]
    auth = lambda user, action: w.plane.c("POST", f"/rooms/{w.r1}/workspaces/{lid}/authorize", {"action": action}, user=user)   # noqa: E731
    for user in ("alice", "bob", "viewer"):
        assert auth(user, "status").status == 200, user                       # positive control: granted metadata
        assert auth(user, "tabs").status == 200
        assert auth(user, "panes").status == 403                              # not granted on this link
        assert auth(user, "terminal").status == 403                           # never implied by membership, even for the owner
    assert auth("guest", "status").status == 403                              # guests get no workspace metadata
    assert auth("mallory", "status").status in (403, 404)
    assert auth("dave", "status").status in (403, 404)


def test_mission_scope_follows_the_mission_link_member_list(world):
    w = world
    put(w, "agent:m-agent", scope="mission", room_id=w.r1, mission_id="MSN-x")
    assert read(w, "alice", "agent:m-agent").status == 404                    # mission not linked to the room yet
    lk = w.plane.c("POST", f"/rooms/{w.r1}/missions", {"mission_id": "MSN-x", "members": ["alice"]}, user="alice")
    assert lk.status == 201
    assert read(w, "alice", "agent:m-agent").status == 200
    assert read(w, "bob", "agent:m-agent").status == 404                      # a room member that the link does not list
    assert w.plane.c("POST", f"/rooms/{w.r1}/missions/MSN-x/unlink", {}, user="alice").status == 200
    assert read(w, "alice", "agent:m-agent").status == 404
    # the same mission id linked by ANOTHER room gains nothing for that room's members
    assert w.plane.c("POST", f"/rooms/{w.r2}/missions", {"mission_id": "MSN-x", "members": ["dave"]}, user="dave").status == 201
    assert read(w, "dave", "agent:m-agent").status == 404


def test_membership_changes_take_effect_on_the_next_read(world):
    w = world
    put(w, "agent:rev", room_id=w.r1)
    assert read(w, "bob", "agent:rev").status == 200
    # a role cannot be changed in place (409), so demotion is remove + re-add as guest; both take effect at once
    assert w.plane.c("POST", f"/rooms/{w.r1}/members", {"user_id": "bob", "role": "guest"}, user="alice").status == 409
    assert w.plane.c("POST", f"/rooms/{w.r1}/members/bob/remove", {}, user="alice").status == 200
    r = read(w, "bob", "agent:rev")
    assert r.status == 404 and r.body == NOT_FOUND
    w.plane.add_member("alice", w.r1, "bob", "guest")
    assert read(w, "bob", "agent:rev").status == 404                          # a guest never reads the graph
    assert read(w, "alice", "agent:rev").status == 200                       # the owner is unaffected
