# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""T08: `room` / `mission` lineage scopes resolve to HCN membership (T07 links), fail closed.

Cast as in test_collab_links: alice owns room and room2; bob contributor; carol viewer; dave guest;
mallory is in no room. The lineage is written by an ingest-level key (WRITER) and read by people
through the HCN-authenticated route `GET /api/v1/collab/graph/...`."""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.graph import can_see, scope_label
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_links import link, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, W, code, svc, user  # noqa: F401
from tests.control_plane.test_graph import (
    OPERATOR,
    OTHER,
    VIEWER,
    WRITER,
    Feed,
    ids,
    make,
)

HOSTILE = {"kind": "member", "id": "user:bob", "authenticated_by": "api_key",
           "access": {"rooms": frozenset({"x"}), "room_ws": frozenset(), "room_missions": frozenset()}}


@pytest.fixture
def fab(w):
    repo, svc, feed = make()
    w.s.graph_source = lambda: repo.graph
    w.repo, w.svc, w.feed = repo, svc, feed
    return w


def read(w, who, node, *, hood=False, **q):
    parts = ["graph", "nodes", node] + (["neighborhood"] if hood else [])
    return w.s.graph_read(who, parts[1:], {k: str(v) for k, v in q.items()})


def visible(w, who, node):
    try:
        read(w, who, node)
        return True
    except CollabError as exc:
        assert exc.status == 404, exc.status
        return False


def put(feed, node, scope, *, principal=None, label=None, **ctx):
    """Upsert a node whose room/mission/workspace context rides in the ENVELOPE (not the payload)."""
    feed.known.add(node)
    return feed.send("node.upsert", {"node_type": node.split(":")[0], "node_id": node,
                                     "label": label or node.split(":")[1]}, scope,
                     principal=principal, **ctx)


# --------------------------------------------------------------------------------- rooms
def test_room_member_reads_room_scoped_lineage_and_non_members_get_404(fab):
    put(fab.feed, "task:t1", "room", room_id=fab.room)
    for ok in (fab.alice, fab.bob, fab.carol):
        assert read(fab, ok, "task:t1")["node"]["id"] == "task:t1"
    assert read(fab, fab.carol, "task:t1")["node"]["visibility_scope"] == "room"
    for denied in (fab.dave, fab.mallory):          # a guest and a person in no room
        assert not visible(fab, denied, "task:t1")
    agent, _ = fab.agent("alice")
    assert not visible(fab, agent, "task:t1")        # an agent never reads the graph
    # the same lineage looks exactly like a node that does not exist
    with pytest.raises(CollabError) as hidden:
        read(fab, fab.mallory, "task:t1")
    with pytest.raises(CollabError) as missing:
        read(fab, fab.mallory, "task:nope")
    assert (hidden.value.status, hidden.value.message) == (missing.value.status, missing.value.message)


def test_other_rooms_lineage_stays_hidden(fab):
    fab.s.add_member(fab.alice, fab.room2, {"user_id": "bob", "role": "viewer"})
    fab.s.remove_member(fab.alice, fab.room2, "bob")
    put(fab.feed, "task:r2", "room", room_id=fab.room2)
    assert visible(fab, fab.alice, "task:r2")
    assert not visible(fab, fab.bob, "task:r2")      # member of `room` only
    assert not visible(fab, fab.carol, "task:r2")


def test_membership_grant_and_revoke_take_effect_on_the_next_read(fab):
    put(fab.feed, "task:t1", "room", room_id=fab.room)
    assert not visible(fab, fab.mallory, "task:t1")
    fab.s.add_member(fab.alice, fab.room, {"user_id": "mallory", "role": "viewer"})
    assert visible(fab, fab.mallory, "task:t1")
    fab.s.remove_member(fab.alice, fab.room, "mallory")
    assert not visible(fab, fab.mallory, "task:t1")
    # a role change to guest also ends it
    assert visible(fab, fab.carol, "task:t1")
    fab.s.remove_member(fab.alice, fab.room, "carol")
    fab.s.add_member(fab.alice, fab.room, {"user_id": "carol", "role": "guest"})
    assert not visible(fab, fab.carol, "task:t1")


def test_a_bare_room_scope_without_a_room_id_stays_operator_only(fab):
    put(fab.feed, "task:bare", "room")
    for who in (fab.alice, fab.bob, fab.carol):
        assert not visible(fab, who, "task:bare")
    assert fab.repo.graph.get_node(OPERATOR, "task:bare")["node"]["id"] == "task:bare"
    with pytest.raises(KeyError):
        fab.repo.graph.get_node(VIEWER, "task:bare")


def test_a_room_that_does_not_exist_grants_nothing(fab):
    put(fab.feed, "task:ghost", "room", room_id="room-that-never-existed")
    for who in (fab.alice, fab.bob, fab.mallory):
        assert not visible(fab, who, "task:ghost")


# ------------------------------------------------------------------------------ missions
def test_mission_scope_needs_the_explicit_mission_member_list(fab):
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    put(fab.feed, "task:m1", "mission", room_id=fab.room, mission_id="m-1")
    assert visible(fab, fab.bob, "task:m1")
    assert not visible(fab, fab.carol, "task:m1")    # room member, not a mission member
    assert not visible(fab, fab.alice, "task:m1")    # the owner is not on the list either
    assert not visible(fab, fab.mallory, "task:m1")
    fab.s.unlink_mission(fab.alice, fab.room, "m-1")
    assert not visible(fab, fab.bob, "task:m1")
    # an unlinked mission id names nobody
    put(fab.feed, "task:m2", "mission", room_id=fab.room, mission_id="m-unlinked")
    assert not visible(fab, fab.bob, "task:m2")


def test_losing_the_room_ends_mission_visibility(fab):
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    put(fab.feed, "task:m1", "mission", room_id=fab.room, mission_id="m-1")
    fab.s.remove_member(fab.alice, fab.room, "bob")
    assert not visible(fab, fab.bob, "task:m1")


def test_a_mission_id_linked_in_two_rooms_stays_bound_to_each_room(fab):
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    fab.s.link_mission(fab.alice, fab.room2, {"mission_id": "m-1", "members": ["alice"]})
    put(fab.feed, "task:m1", "mission", room_id=fab.room, mission_id="m-1")
    put(fab.feed, "task:m2", "mission", room_id=fab.room2, mission_id="m-1")
    assert visible(fab, fab.bob, "task:m1") and not visible(fab, fab.bob, "task:m2")
    assert visible(fab, fab.alice, "task:m2") and not visible(fab, fab.alice, "task:m1")   # not on room 1's list
    assert not visible(fab, fab.carol, "task:m1")


def test_a_mission_id_linked_by_someone_elses_room_grants_nothing(fab):
    """Review M1: any room owner can link any mission id; the label is bound to the linking room."""
    put(fab.feed, "task:secret", "mission", room_id=fab.room, mission_id="m-secret")
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-secret", "members": ["bob"]})
    assert visible(fab, fab.bob, "task:secret") and not visible(fab, fab.mallory, "task:secret")
    pid = fab.s.create_project(fab.mallory, {"name": "evil"})["project_id"]
    rid = fab.s.create_room(fab.mallory, pid, {"name": "evil"})["room_id"]
    fab.s.link_mission(fab.mallory, rid, {"mission_id": "m-secret", "members": ["mallory"]})
    assert not visible(fab, fab.mallory, "task:secret")
    # her own room can only ever see events that name HER room
    put(fab.feed, "task:hers", "mission", room_id=rid, mission_id="m-secret")
    assert visible(fab, fab.mallory, "task:hers") and not visible(fab, fab.bob, "task:hers")
    # a mission event without a room is the bare operator-only scope
    put(fab.feed, "task:norom", "mission", mission_id="m-secret")
    assert not visible(fab, fab.bob, "task:norom") and not visible(fab, fab.mallory, "task:norom")
    assert fab.repo.graph.get_node(OPERATOR, "task:norom")["node"]["visibility_scope"] == "mission"


# ---------------------------------------------------------------------- workspace links
def test_an_event_naming_a_workspace_needs_a_viewing_link_from_that_room(fab):
    put(fab.feed, "task:w1", "room", room_id=fab.room, workspace_id="ws-1")
    assert not visible(fab, fab.bob, "task:w1")      # no link yet
    lk = link(fab, ws="ws-1", grants=["status"])
    assert visible(fab, fab.bob, "task:w1")
    assert not visible(fab, fab.dave, "task:w1")
    # the link of ANOTHER workspace does not help
    put(fab.feed, "task:w2", "room", room_id=fab.room, workspace_id="ws-2")
    assert not visible(fab, fab.bob, "task:w2")
    # linked to a different room only: this room still sees nothing of ws-9
    link(fab, room=fab.room2, ws="ws-9", grants=["status"])
    put(fab.feed, "task:w9", "room", room_id=fab.room, workspace_id="ws-9")
    assert not visible(fab, fab.bob, "task:w9")
    # revoking the grant, then unlinking, hides it again
    fab.s.set_workspace_grants(fab.alice, fab.room, lk["link_id"], {"grants": []})
    assert not visible(fab, fab.bob, "task:w1")
    fab.s.set_workspace_grants(fab.alice, fab.room, lk["link_id"], {"grants": ["tabs"]})
    assert visible(fab, fab.bob, "task:w1")
    fab.s.unlink_workspace(fab.alice, fab.room, lk["link_id"])
    assert not visible(fab, fab.bob, "task:w1")


def test_mission_events_with_a_workspace_need_the_mission_room_link(fab):
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    put(fab.feed, "task:mw", "mission", room_id=fab.room, mission_id="m-1", workspace_id="ws-1")
    assert not visible(fab, fab.bob, "task:mw")
    link(fab, ws="ws-1")
    assert visible(fab, fab.bob, "task:mw")


# --------------------------------------------------------------------- graph-level rules
def test_re_scoping_a_node_into_another_context_hides_it_from_both_audiences(fab):
    put(fab.feed, "task:x", "room", room_id=fab.room)
    put(fab.feed, "task:x", "room", room_id=fab.room2)          # same writer re-scopes it elsewhere
    for who in (fab.alice, fab.bob):
        assert not visible(fab, who, "task:x")                   # neither room's members
    # operators keep seeing it (a bare room label), exactly as for any unresolvable room scope
    assert fab.repo.graph.get_node(OPERATOR, "task:x")["node"]["visibility_scope"] == "room"
    put(fab.feed, "task:y", "room", room_id=fab.room)
    put(fab.feed, "task:y", "org")                               # a later org event cannot widen it
    assert visible(fab, fab.bob, "task:y") and not visible(fab, fab.mallory, "task:y")


def test_one_room_across_two_workspaces_needs_a_link_to_each_and_never_vanishes(fab):
    link(fab, ws="ws-1")
    put(fab.feed, "agent:x", "room", room_id=fab.room, workspace_id="ws-1")
    assert visible(fab, fab.bob, "agent:x")
    put(fab.feed, "agent:x", "room", room_id=fab.room)                       # later event names no workspace
    assert visible(fab, fab.bob, "agent:x")
    put(fab.feed, "agent:x", "room", room_id=fab.room, workspace_id="ws-2")  # and one from the second workspace
    assert not visible(fab, fab.bob, "agent:x")          # ws-2 is not linked to this room yet: fail closed
    assert fab.repo.graph.get_node(OPERATOR, "agent:x")["node"]["id"] == "agent:x"   # but never "private": operators still see it
    link(fab, ws="ws-2")
    assert visible(fab, fab.bob, "agent:x")
    assert not visible(fab, fab.mallory, "agent:x")


def test_a_member_never_reads_org_lineage_and_neighborhoods_show_only_their_own_room(fab):
    f = fab.feed
    put(f, "task:org", "org")
    put(f, "task:mine", "room", room_id=fab.room)
    put(f, "task:mine2", "room", room_id=fab.room)
    put(f, "task:theirs", "room", room_id=fab.room2)
    f.edge("dependency", "task:mine", "task:mine2", scope="room", room_id=fab.room, nodes=False)
    f.edge("dependency", "task:mine", "task:theirs", scope="room", room_id=fab.room2, nodes=False)
    f.edge("dependency", "task:mine", "task:org", scope="org", nodes=False)
    bob = read(fab, fab.bob, "task:mine", hood=True)
    assert ids(bob) == {"task:mine", "task:mine2"} and not bob["truncated"]
    alice = read(fab, fab.alice, "task:mine", hood=True)
    assert ids(alice) == {"task:mine", "task:mine2", "task:theirs"}      # her other room, still not org
    for who in (fab.alice, fab.bob, fab.carol, fab.mallory):
        assert not visible(fab, who, "task:org")                         # org is fleet data, not people data
    # the fleet keys still read org lineage exactly as before
    assert fab.repo.graph.get_node(VIEWER, "task:org")["node"]["visibility_scope"] == "org"
    d = fab.s.dispatch
    st, _ = d("GET", ["graph", "nodes", "task:org"], {}, {}, f"Bearer {USERS['bob']}", None)
    assert st == 404


def test_commons_fields_stay_per_writer_under_membership(fab):
    f = fab.feed
    put(f, "pr:github.com/o/r#1", "room", label="room label", room_id=fab.room)
    put(f, "pr:github.com/o/r#1", "room", label="other label", room_id=fab.room2, principal=OTHER)
    put(f, "pr:github.com/o/r#1", "org", label="org label", principal=OPERATOR)
    bob = json.dumps(read(fab, fab.bob, "pr:github.com/o/r#1")["node"])
    assert "room label" in bob and "other label" not in bob and "org label" not in bob
    alice = json.dumps(read(fab, fab.alice, "pr:github.com/o/r#1")["node"])
    assert "room label" in alice and "other label" in alice and "org label" not in alice
    assert not visible(fab, fab.mallory, "pr:github.com/o/r#1")          # no writer of it is visible to her


def test_a_hand_built_member_principal_is_not_trusted(fab):
    put(fab.feed, "task:t1", "room", room_id="x")
    graph = fab.repo.graph
    assert can_see(HOSTILE, scope_label({"visibility_scope": "room", "room_id": "x"}), None) is False
    with pytest.raises(KeyError):
        graph.get_node(HOSTILE, "task:t1")
    with pytest.raises(KeyError):
        graph.get_node(VIEWER, "task:t1")                        # nor does a viewer key see it
    assert graph.get_node(OPERATOR, "task:t1")["node"]["id"] == "task:t1"


def test_scope_label_is_derived_from_validated_event_fields_only():
    assert scope_label({"visibility_scope": "org", "room_id": "r"}) == "org"
    assert scope_label({"visibility_scope": "private", "room_id": "r"}) == "private"
    assert scope_label({"visibility_scope": "room", "room_id": "r"}) == ("room", "r", None, ())
    assert scope_label({"visibility_scope": "room", "mission_id": "m"}) == "room"
    assert scope_label({"visibility_scope": "mission", "mission_id": "m", "room_id": "r", "workspace_id": "w"}) == ("mission", "m", "r", ("w",))
    assert scope_label({"visibility_scope": "mission", "mission_id": "m"}) == "mission"      # needs its room
    assert scope_label({"visibility_scope": "bogus", "room_id": "r"}) == "private"
    assert scope_label({"visibility_scope": "room", "room_id": ""}) == "room"
    assert scope_label({"visibility_scope": "room", "room_id": 7}) == "room"


# --------------------------------------------------------------------- front doors
def test_collab_route_requires_a_signed_in_person_and_a_combined_server(fab):
    put(fab.feed, "task:t1", "room", room_id=fab.room)
    d = fab.s.dispatch
    tok = f"Bearer {USERS['bob']}"
    st, body = d("GET", ["graph", "nodes", "task:t1"], {}, {}, tok, None)
    assert st == 200 and body["node"]["id"] == "task:t1"
    st, body = d("GET", ["graph", "nodes", "task:t1", "neighborhood"], {"depth": "1"}, {}, tok, None)
    assert st == 200 and ids(body) == {"task:t1"}
    st, _ = d("GET", ["graph", "nodes", "task:t1"], {}, {}, "", None)
    assert st == 401
    st, _ = d("GET", ["graph", "nodes", "task:t1"], {}, {}, "Bearer not-a-token", None)
    assert st == 401
    st, body = d("GET", ["graph", "nodes", "task:t1", "neighborhood"], {"depth": "banana"}, {}, tok, None)
    assert st == 400
    st, _ = d("GET", ["graph", "nodes", "task:t1"], {}, {}, f"Bearer {USERS['mallory']}", None)
    assert st == 404
    st, _ = d("GET", ["graph", "elsewhere"], {}, {}, tok, None)
    assert st == 404
    st, _ = d("POST", ["graph", "nodes", "task:t1"], {}, {}, tok, None)
    assert st == 404                                              # read-only: nothing here writes
    fab.s.graph_source = None                                     # collab-only process: no fabric data
    st, _ = d("GET", ["graph", "nodes", "task:t1"], {}, {}, tok, None)
    assert st == 404


def test_the_keyed_fabric_api_is_unchanged_for_room_scopes(fab):
    put(fab.feed, "task:t1", "room", room_id=fab.room)
    g = fab.repo.graph
    assert g.get_node(OPERATOR, "task:t1")["node"]["visibility_scope"] == "room"
    with pytest.raises(KeyError):
        g.get_node(VIEWER, "task:t1")
    with pytest.raises(KeyError):
        g.get_node(OTHER, "task:t1")


# ----------------------------------------------------------------- rebuild == live
def test_rebuild_equals_live_for_every_reader_across_grant_and_revoke(fab):
    f = fab.feed
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    link(fab, ws="ws-1")
    put(f, "task:org", "org")
    put(f, "task:r1", "room", room_id=fab.room)
    put(f, "task:r2", "room", room_id=fab.room2)
    put(f, "task:m1", "mission", room_id=fab.room, mission_id="m-1")
    put(f, "task:w1", "room", room_id=fab.room, workspace_id="ws-1")
    put(f, "task:w2", "room", room_id=fab.room, workspace_id="ws-2")
    put(f, "task:x", "room", room_id=fab.room)
    put(f, "task:x", "room", room_id=fab.room2)
    put(f, "pr:github.com/o/r#1", "room", label="a", room_id=fab.room)
    put(f, "pr:github.com/o/r#1", "org", label="b", principal=OTHER)
    for n in ("task:r1", "task:r2", "task:m1", "task:w1", "task:w2"):
        f.edge("dependency", "task:org", n, scope="org", nodes=False)

    def fresh_repo():
        fresh = ControlPlaneRepo()
        fresh.import_state(json.loads(json.dumps(fab.repo.export_state(), default=str)))
        return fresh

    nodes = ["task:org", "task:r1", "task:r2", "task:m1", "task:w1", "task:w2", "task:x",
             "pr:github.com/o/r#1"]

    def views(repo):
        fab.s.graph_source = lambda: repo.graph
        out = {}
        for name in ("alice", "bob", "carol", "dave", "mallory"):
            who = getattr(fab, name)
            for node in nodes:
                try:
                    out[name, node] = json.dumps(read(fab, who, node), sort_keys=True, default=str)
                    out[name, node, "hood"] = json.dumps(read(fab, who, node, hood=True), sort_keys=True, default=str)
                except CollabError as exc:
                    out[name, node] = out[name, node, "hood"] = exc.status
            for room in (None, fab.room, fab.room2):
                try:
                    q = {"node_type": "task", "edge_type": "dependency", **({"room_id": room} if room else {})}
                    out[name, "list", room] = json.dumps(fab.s.graph_read(who, ["nodes"], q), sort_keys=True, default=str)
                except CollabError as exc:
                    out[name, "list", room] = exc.status
        for key in (OPERATOR, VIEWER, OTHER, WRITER):
            for node in nodes:
                try:
                    out[key["id"], node] = json.dumps(repo.graph.get_node(key, node), sort_keys=True, default=str)
                except KeyError:
                    out[key["id"], node] = 404
        out["snapshot"] = json.dumps(repo.graph.snapshot(), sort_keys=True, default=str)
        return out

    def same(label):
        live = views(fab.repo)
        rebuilt = views(fresh_repo())
        assert live == rebuilt, label
        return live

    first = same("initial")
    # the matrix is not vacuous: members see some of it, others none
    assert first["bob", "task:r1"] != 404 and first["mallory", "task:r1"] == 404
    assert first["bob", "task:m1"] != 404 and first["carol", "task:m1"] == 404
    assert first["bob", "task:w1"] != 404 and first["bob", "task:w2"] == 404
    assert first["bob", "task:x"] == 404 and first["bob", "task:r2"] == 404
    assert first["bob", "list", fab.room] != 404 and first["mallory", "list", fab.room] == 404
    # membership changes flow through both projections identically
    fab.s.add_member(fab.alice, fab.room, {"user_id": "mallory", "role": "viewer"})
    fab.s.remove_member(fab.alice, fab.room, "carol")
    fab.s.unlink_mission(fab.alice, fab.room, "m-1")
    changed = same("after grant/revoke")
    assert changed["mallory", "task:r1"] != 404 and changed["carol", "task:r1"] == 404
    assert changed["bob", "task:m1"] == 404
    fab.s.graph_source = lambda: fab.repo.graph
    # collab state itself replays to the same decisions
    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    from tests.control_plane.test_collab import _digest
    twin = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    twin.hydrate(fab.s._log)
    for who in (fab.alice, fab.bob, fab.carol, fab.dave, fab.mallory):
        assert twin.graph_access(who) == fab.s.graph_access(who)


def test_percent_encoded_node_ids_round_trip_through_the_route(fab):
    node = "issue:github.com/o/r#7"
    put(fab.feed, node, "room", label="seven", room_id=fab.room)
    assert quote(node, safe="") != node
    assert read(fab, fab.bob, node)["node"]["id"] == node


# ------------------------------------------------------------------- real HTTP server
from tests.control_plane.test_collab import BASE, clock, env, invite, make_room  # noqa: E402,F401


def _key(env, role):
    """The configured key for a role, read from the running server (no key literal in this file)."""
    return next(k for k, r in env.srv._keys.items() if r == role)


def _ingest(env, node, scope, **ctx):
    import http.client
    body = {"schema_version": 2, "event_id": f"e-{node}", "source": "pi", "source_sequence": 0,
            "entity_type": "lineage", "entity_id": node, "event_type": "node.upsert",
            "payload": {"node_type": node.split(":")[0], "node_id": node, "label": "L"},
            "visibility_scope": scope, "occurred_at": "2026-10-22T10:00:00Z", **ctx}
    conn = http.client.HTTPConnection("127.0.0.1", env.api.port, timeout=5)
    conn.request("POST", "/api/v1/ingest", json.dumps(body),
                 {"X-API-Key": _key(env, "operator"), "Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 200
    conn.close()


def test_real_server_serves_membership_scoped_lineage_over_the_collab_route(env):
    _, room = make_room(env.api)                                   # alice owns the room
    assert invite(env.api, room, "bob", "contributor")[0] in (200, 201)
    _ingest(env, "task:http", "room", room_id=room)
    path = f"{BASE}/graph/nodes/{quote('task:http', safe='')}"
    s, body = env.api.call("GET", path, user="alice")
    assert s == 200 and body["node"]["id"] == "task:http"
    assert env.api.call("GET", path, user="bob")[0] == 200
    assert env.api.call("GET", path, user="mallory")[0] == 404     # signed in, in no room
    assert env.api.call("GET", path)[0] == 401                      # not signed in
    # leaving the room ends access on the very next request
    assert env.api.call("POST", f"{BASE}/rooms/{room}/members/bob/remove", {}, user="alice")[0] == 200
    assert env.api.call("GET", path, user="bob")[0] == 404
    # the legacy keyed API still treats room scope as operator-only
    import http.client
    for key, want in ((_key(env, "operator"), 200), (_key(env, "viewer"), 404)):
        conn = http.client.HTTPConnection("127.0.0.1", env.api.port, timeout=5)
        conn.request("GET", f"/api/v1/graph/nodes/{quote('task:http', safe='')}", headers={"X-API-Key": key})
        assert conn.getresponse().status == want
        conn.close()


# ----------------------------------------------------------------------- label algebra
def test_the_label_meet_is_a_semilattice_and_never_widens_anyone():
    import itertools
    import random

    from herdr_engineering.control_plane.graph import _narrow
    labels = ["private", "mission", "room", "org",
              ("room", "R1", None, ()), ("room", "R1", None, ("a",)), ("room", "R1", None, ("b",)),
              ("room", "R1", None, ("a", "b")), ("room", "R2", None, ()),
              ("mission", "M1", "R1", ()), ("mission", "M1", "R1", ("a",)), ("mission", "M1", "R2", ()),
              ("mission", "M2", "R1", ())]
    for a, b in itertools.product(labels, labels):
        assert _narrow(a, b) == _narrow(b, a)
        assert _narrow(a, a) == a
    for a, b, c in itertools.product(labels, repeat=3):
        assert _narrow(_narrow(a, b), c) == _narrow(a, _narrow(b, c)), (a, b, c)
    rng = random.Random(7)
    for _ in range(300):
        access = {"rooms": frozenset(x for x in ("R1", "R2") if rng.random() < .5),
                  "room_missions": frozenset((r, m) for r in ("R1", "R2") for m in ("M1", "M2") if rng.random() < .5),
                  "room_ws": frozenset((r, w) for r in ("R1", "R2") for w in "ab" if rng.random() < .5)}
        member = {"kind": "member", "id": "user:x", "authenticated_by": "collab", "access": access}
        for who in (member, OPERATOR, VIEWER):
            for a, b in itertools.product(labels, labels):
                if who is member and "org" in (a, b):
                    continue          # `org` is fleet data: above every label for KEYS, not for people
                if can_see(who, _narrow(a, b), frozenset()):
                    assert can_see(who, a, frozenset()) and can_see(who, b, frozenset()), (who["kind"], a, b)
    # a member never reads fleet `org`, whatever else they belong to
    full = {"kind": "member", "id": "user:x", "authenticated_by": "collab",
            "access": {"rooms": frozenset({"R1"}), "room_missions": frozenset(), "room_ws": frozenset()}}
    assert can_see(full, "org", None) is False and can_see(VIEWER, "org", None) is True


# ------------------------------------------------------------------------- listing one room
def test_listing_agents_is_scoped_to_one_room_and_its_missions(fab):
    f = fab.feed
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    put(f, "agent:lead", "room", room_id=fab.room)
    put(f, "agent:kid", "mission", room_id=fab.room, mission_id="m-1")                   # a mission-scoped event, bound to the room that linked it
    put(f, "agent:elsewhere", "room", room_id=fab.room2)
    put(f, "agent:fleet", "org")
    put(f, "agent:bare", "room")
    f.edge("spawn", "agent:lead", "agent:kid", scope="mission", room_id=fab.room, mission_id="m-1", nodes=False)
    f.edge("spawn", "agent:lead", "agent:elsewhere", scope="room", room_id=fab.room2, nodes=False)

    def listing(who, room=None):
        q = {"node_type": "agent", "edge_type": "spawn", **({"room_id": room} if room else {})}
        return fab.s.graph_read(who, ["nodes"], q)

    bob = listing(fab.bob, fab.room)
    assert ids(bob) == {"agent:lead", "agent:kid"}
    assert [(e["from_id"], e["to_id"]) for e in bob["edges"]] == [("agent:lead", "agent:kid")]
    # carol is in the room but not on the mission list: only the room's own agents
    assert ids(listing(fab.carol, fab.room)) == {"agent:lead"}
    # alice belongs to both rooms; asking for room2 gives room2's agents only, and no org or bare ones
    assert ids(listing(fab.alice, fab.room2)) == {"agent:elsewhere"}
    with pytest.raises(CollabError) as e:
        listing(fab.alice)                                             # a listing is always ONE room's view
    assert e.value.status == 400
    for who in (fab.mallory, fab.dave):
        with pytest.raises(CollabError) as e:
            listing(who, fab.room)
        assert e.value.status == 404
    with pytest.raises(CollabError) as e:
        listing(fab.bob, fab.room2)                                    # not a member of that room
    assert e.value.status == 404
    with pytest.raises(CollabError) as e:
        fab.s.graph_read(fab.bob, ["nodes"], {"node_type": "banana"})
    assert e.value.status == 400
    # the keyed API lists with its own rules (operators everything, viewers org only) and cannot take `contexts`
    g = fab.repo.graph
    assert ids(g.list_nodes(OPERATOR, "agent")) == {"agent:lead", "agent:kid", "agent:elsewhere", "agent:fleet", "agent:bare"}
    assert ids(g.list_nodes(VIEWER, "agent")) == {"agent:fleet"}
    # revoking the mission list takes the mission's agents out of the next listing
    fab.s.unlink_mission(fab.alice, fab.room, "m-1")
    assert ids(listing(fab.bob, fab.room)) == {"agent:lead"}


def test_listing_is_bounded_and_does_not_count_hidden_nodes(fab):
    for i in range(6):
        put(fab.feed, f"agent:a{i}", "room", room_id=fab.room)
        put(fab.feed, f"agent:h{i}", "room", room_id=fab.room2)
    out = fab.repo.graph.list_nodes(OPERATOR, "agent", limit=4)
    assert len(out["nodes"]) == 4 and out["truncated"] is True
    mem = {"kind": "member", "id": "user:x", "authenticated_by": "collab",
           "access": {"rooms": frozenset({fab.room}), "room_missions": frozenset(), "room_ws": frozenset()}}
    small = fab.repo.graph.list_nodes(mem, "agent", limit=6)
    assert len(small["nodes"]) == 6 and small["truncated"] is False      # the six hidden ones are not counted


# ------------------------------------------------------- review M2: listing cost is per room
def test_a_flood_in_other_contexts_cannot_blank_a_rooms_listing(fab, monkeypatch):
    from herdr_engineering.control_plane import graph as G
    monkeypatch.setattr(G, "SCAN_BUDGET", 40)
    evil = Feed(fab.svc, source="evil")
    for i in range(60):                                   # private, and addressed to another room
        evil.send("node.upsert", {"node_type": "agent", "node_id": f"agent:p{i}", "label": "x"}, "private", principal=OTHER)
        put(evil, f"agent:o{i}", "room", room_id=fab.room2, principal=OTHER)
    put(fab.feed, "agent:real", "room", room_id=fab.room, label="real")
    out = fab.s.graph_read(fab.bob, ["nodes"], {"node_type": "agent", "room_id": fab.room})
    assert ids(out) == {"agent:real"} and out["truncated"] is False and out["truncated_reason"] is None


def test_a_listing_that_hits_a_budget_says_which_and_counts_nothing_hidden(fab, monkeypatch):
    from herdr_engineering.control_plane import graph as G
    monkeypatch.setattr(G, "SCAN_BUDGET", 5)
    for i in range(8):
        put(fab.feed, f"agent:a{i}", "room", room_id=fab.room)
    out = fab.s.graph_read(fab.bob, ["nodes"], {"node_type": "agent", "room_id": fab.room})
    assert out["truncated"] is True and out["truncated_reason"] == "scan_budget" and len(out["nodes"]) == 5
    assert [n["id"] for n in out["nodes"]] == [f"agent:a{i}" for i in range(5)]    # admission order, no whole-set sort
    monkeypatch.setattr(G, "SCAN_BUDGET", 3000)
    monkeypatch.setattr(G, "EDGE_SCAN_TOTAL", 3)
    for i in range(7):
        fab.feed.edge("spawn", f"agent:a{i}", f"agent:a{i + 1}", scope="room", room_id=fab.room, nodes=False)
    out = fab.s.graph_read(fab.bob, ["nodes"], {"node_type": "agent", "edge_type": "spawn", "room_id": fab.room})
    assert out["truncated_reason"] == "edge_budget"


# ------------------------------------------------- review M3: keys address only listed rooms
def _restricted(rooms=(), missions=()):
    from herdr_engineering.control_plane.ingest import IngestService
    repo = ControlPlaneRepo()
    svc = IngestService(repo, key_scopes={WRITER["id"]: {"rooms": frozenset(rooms), "missions": frozenset(missions)}})
    return repo, svc, Feed(svc)


def test_a_key_without_a_list_writes_only_org_and_private(fab):
    _, _, f = _restricted()
    put(f, "task:ok1", "org")
    put(f, "task:ok2", "private")
    for scope, ctx in (("room", {"room_id": fab.room}), ("room", {}), ("mission", {"room_id": fab.room, "mission_id": "m"}),
                       ("mission", {"mission_id": "m"})):
        with pytest.raises(PermissionError):
            put(f, "task:no", scope, **ctx)
    # operators are not restricted
    put(f, "task:op", "room", principal=OPERATOR, room_id=fab.room)


def test_a_key_may_address_only_its_listed_rooms_and_missions(fab):
    repo, _, f = _restricted(rooms=["room-a"], missions=["m-1"])
    put(f, "agent:a", "room", room_id="room-a")
    with pytest.raises(PermissionError):
        put(f, "agent:b", "room", room_id="room-b")
    with pytest.raises(PermissionError):
        put(f, "agent:c", "room")                                       # bare room scope is not on the list either
    put(f, "agent:m", "mission", room_id="room-a", mission_id="m-1")
    for ctx in ({"room_id": "room-a", "mission_id": "m-2"}, {"room_id": "room-b", "mission_id": "m-1"},
                {"mission_id": "m-1"}):
        with pytest.raises(PermissionError):
            put(f, "agent:x", "mission", **ctx)
    assert set(repo.graph._nodes) == {"agent:a", "agent:m"}              # refused events leave no trace
    # a forged agent in a victim room by a key that is not allowed there
    with pytest.raises(PermissionError):
        f.edge("spawn", "agent:a", "agent:m", scope="room", room_id="room-b", nodes=False)


def test_key_scopes_come_from_operator_yaml_config_and_the_http_refusal_is_typed():
    import http.client
    import threading

    from herdr_engineering.control_plane.api import ControlPlaneServer
    keys = {"in-" + "a": "ingest", "in-" + "b": "ingest"}
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys,
                                lineage_key_scopes={"in-" + "a": {"rooms": ["room-a"], "missions": []}})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        def post(key, n, scope, **ctx):
            body = {"schema_version": 2, "event_id": f"x{n}", "source": "pi", "source_sequence": n, "entity_type": "lineage",
                    "entity_id": f"task:t{n}", "event_type": "node.upsert",
                    "payload": {"node_type": "task", "node_id": f"task:t{n}"}, "visibility_scope": scope, **ctx}
            c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            c.request("POST", "/api/v1/ingest", json.dumps(body), {"X-API-Key": key, "Content-Type": "application/json"})
            r = c.getresponse()
            out = json.loads(r.read() or b"{}")
            c.close()
            return r.status, out
        assert post("in-a", 0, "room", room_id="room-a")[0] == 200
        status, out = post("in-a", 1, "room", room_id="room-z")
        assert status == 403 and out.get("reason") == "lineage_forbidden"
        assert post("in-b", 2, "room", room_id="room-a")[0] == 403          # no entry: fail closed
        assert post("in-b", 3, "org")[0] == 200
    finally:
        server.shutdown()
