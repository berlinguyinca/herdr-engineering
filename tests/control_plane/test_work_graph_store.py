"""Work Graph on real PostgreSQL: parity with the pure core, idempotence, ACL, paging, history, layouts."""
from __future__ import annotations

import random

import pytest

from herdr_engineering.control_plane import work_graph_core as core
from herdr_engineering.control_plane import workspace_contracts as wc
from herdr_engineering.control_plane.work_graph_store import WorkGraphStore

from .wg_pg import wg, wg_pg  # noqa: F401
from .work_graph_events import scenario

OP = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}


def member(uid, rooms, missions=()):
    return {"kind": "member", "id": f"user:{uid}", "authenticated_by": "collab",
            "access": {"rooms": frozenset(rooms), "room_ws": frozenset(), "room_missions": frozenset(missions)}}


def store(wg, hook=None):
    return WorkGraphStore(wg.pool, artifact_ok=hook)


def test_postgres_projection_equals_the_pure_projection(wg):
    events = scenario().events
    st = store(wg)
    shuffled = events[:]
    random.Random(5).shuffle(shuffled)
    noisy = shuffled + events[:20]                                    # reordered AND repeated
    res = wg.run(st.apply_events(noisy, batch=17))
    assert res["applied"] == len(events) and res["duplicates"] == 20 and res["poison"] == 0
    pure = core.Projection()
    pure.apply_all(events)
    assert wg.run(st.dump()) == pure.dump()                           # tables hold exactly the fold
    again = wg.run(st.apply_events(events))
    assert again["applied"] == 0 and again["duplicates"] == len(events)


def test_every_row_links_to_a_ledgered_source_event(wg):
    st = store(wg)
    wg.run(st.apply_events(scenario().events))
    d = wg.run(st.dump())
    assert core.evidence_violations(d, wg.run(st.ledger_keys())) == []
    bad = wg.run(wg.pool.fetchval(
        "SELECT count(*) FROM wg_assertion a LEFT JOIN wg_event e ON e.event_key = a.event_key WHERE e.event_key IS NULL"))
    assert bad == 0


def test_edge_uniqueness_and_no_deletes(wg):
    st = store(wg)
    ev = scenario().events
    wg.run(st.apply_events(ev))
    n = wg.run(wg.pool.fetchval("SELECT count(*) FROM wg_edge"))
    wg.run(st.apply_events([{**e, "event_id": e["event_id"] + "-again"} for e in ev if e["entity_type"] == "collab"]))
    assert wg.run(wg.pool.fetchval("SELECT count(*) FROM wg_edge")) == n          # same (type, from, to): one row
    with pytest.raises(Exception):
        wg.run(wg.pool.execute("INSERT INTO wg_edge (edge_id, edge_type, from_id, to_id, live, source_event_id) "
                               "SELECT 'dup', edge_type, from_id, to_id, true, source_event_id FROM wg_edge LIMIT 1"))


def test_a09_member_sees_only_their_rooms_and_nothing_leaks_through_neighbors_counts_or_search(wg):
    st = store(wg)
    wg.run(st.apply_events(scenario().events))
    alice = member("alice", ["p1-r0", "p1-r1"])
    carol_p2 = member("carol", ["p2-r0"])
    outsider = member("zed", ["nowhere"])
    # lists: only own rooms' tasks
    mine = wg.run(st.list_entities(alice, kinds=["task"], limit=200))["items"]
    assert {t["native_id"] for t in mine} == {f"p1-r{r}-t{t}" for r in (0, 1) for t in range(3)}
    # search never reveals the other project even by exact name; a hit total is not exposed at all
    assert wg.run(st.search(alice, "p2-r0-t1"))["hits"] == []
    assert [h["native_id"] for h in wg.run(st.search(carol_p2, "p2-r0-t1"))["hits"]] == ["p2-r0-t1"]
    # snapshot from the project: no foreign room, task, agent or count
    snap = wg.run(st.snapshot(alice, "project:p1", depth=3, max_nodes=500))
    names = {n["native_id"] for n in snap["nodes"]}
    assert "p1" in names and not any(n.startswith("p2") for n in names)
    assert all(n["degree"] <= len(snap["edges"]) for n in snap["nodes"])
    # an invisible root and a missing root are the same error
    for ref in ("project:p2", "project:nope", "task:p2-r0-t0"):
        with pytest.raises(wc.NotFoundOrForbidden):
            wg.run(st.snapshot(alice, ref))
        with pytest.raises(wc.NotFoundOrForbidden):
            wg.run(st.get_entity(outsider, ref))
    # a revoked / unauthenticated principal sees nothing
    assert wg.run(st.list_entities({"kind": "unknown", "id": None, "authenticated_by": "none"}))["items"] == []
    # degrees are over the VISIBLE subgraph: the shared agent p1-agent is in p1 only
    agent = [n for n in snap["nodes"] if n["kind"] == "agent"]
    assert all(n["degree"] >= 1 for n in agent)


def test_a09_org_scope_and_private_scope(wg):
    st = store(wg)
    wg.run(st.apply_events([
        {"event_id": "o1", "entity_type": "session", "entity_id": "s-pub", "event_type": "SessionStarted", "sequence": 1,
         "source_timestamp": "2026-10-02T00:00:00Z", "payload": {}, "actor": {"id": "key:a"}, "visibility_scope": "org"},
        {"event_id": "o2", "entity_type": "session", "entity_id": "s-priv", "event_type": "SessionStarted", "sequence": 2,
         "source_timestamp": "2026-10-02T00:00:01Z", "payload": {}, "actor": {"id": "key:a"}, "visibility_scope": "private"}]))
    viewer = {"kind": "viewer", "id": "key:v", "authenticated_by": "api_key"}
    got = {i["native_id"] for i in wg.run(st.list_entities(viewer, kinds=["agent_session"]))["items"]}
    assert got == {"s-pub"}
    assert {i["native_id"] for i in wg.run(st.list_entities(member("alice", ["p1-r0"]), kinds=["agent_session"]))["items"]} == set()
    owner = {"kind": "viewer", "id": "key:a", "authenticated_by": "api_key"}
    assert {i["native_id"] for i in wg.run(st.list_entities(owner, kinds=["agent_session"]))["items"]} == {"s-pub", "s-priv"}
    # operators do not see another writer's private rows (same rule as graph.can_see)
    assert {i["native_id"] for i in wg.run(st.list_entities(OP, kinds=["agent_session"]))["items"]} == {"s-pub"}


def test_cursor_paging_is_stable_and_complete(wg):
    st = store(wg)
    wg.run(st.apply_events(scenario().events))
    seen, cur = [], None
    for _ in range(30):
        page = wg.run(st.list_entities(OP, kinds=["task"], limit=5, cursor=cur))
        seen += [i["native_id"] for i in page["items"]]
        cur = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == 12 and len(set(seen)) == 12
    with pytest.raises(ValueError):
        wg.run(st.list_entities(OP, kinds=["task"], cursor="garbage"))
    with pytest.raises(ValueError):
        wg.run(st.list_entities(OP, kinds=["task"], limit=5, cursor=wg.run(st.list_entities(OP, kinds=["room"], limit=1))["next_cursor"]))


def test_filtered_search_and_project_filter(wg):
    st = store(wg)
    wg.run(st.apply_events(scenario().events))
    hits = wg.run(st.search(OP, "failure p1-r1", kinds=["task"]))["hits"]
    assert sorted(h["native_id"] for h in hits) == ["p1-r1-t0", "p1-r1-t1", "p1-r1-t2"]
    assert wg.run(st.search(OP, "100%"))["hits"] == []                     # wildcard characters are literal
    by_proj = wg.run(st.list_entities(OP, kinds=["task"], project_id="p2", limit=50))["items"]
    assert len(by_proj) == 6 and all(t["project_id"] == "p2" for t in by_proj)
    done = wg.run(st.list_entities(OP, kinds=["task"], status="done", limit=50))["items"]
    assert len(done) == 12


def test_history_as_of_returns_the_graph_of_that_time(wg):
    st = store(wg)
    s = scenario()
    wg.run(st.apply_events(s.events))
    early = "2026-10-01T08:00:30Z"                                         # before any room exists (project p1 at +1m)
    with pytest.raises(wc.NotFoundOrForbidden):
        wg.run(st.get_entity(OP, "project:p1", as_of=early))
    tid = "task:p1-r0-t0"
    # created at ~ +5m (accepted +5.3, done +5.6): in between the status was accepted
    t = wg.run(st.get_entity(OP, tid, as_of="2026-10-01T08:05:20Z"))
    assert t["status"] == "pending" or t["status"] == "accepted"
    assert wg.run(st.get_entity(OP, tid))["status"] == "done"
    snap_then = wg.run(st.snapshot(OP, "project:p1", depth=3, as_of="2026-10-01T08:03:00Z"))
    snap_now = wg.run(st.snapshot(OP, "project:p1", depth=3))
    assert len(snap_then["nodes"]) < len(snap_now["nodes"]) and snap_then["as_of"].startswith("2026-10-01T08:03")
    # A07: after the member left and the agent was revoked, history before that still shows them
    facts_then = wg.run(st.facts(OP, "room:p1-r0", fact_kind="team_role", as_of="2026-10-01T09:00:00Z"))
    facts_now = wg.run(st.facts(OP, "room:p1-r0", fact_kind="team_role"))
    assert {f["subject"] for f in facts_then["items"]} == {"user:alice", "user:carol"}
    assert {f["subject"] for f in facts_now["items"]} == {"user:alice", "user:carol"}      # closed segments are kept
    assert any(not f["open"] for f in facts_now["items"] if f["subject"] == "user:carol")
    with pytest.raises(ValueError):
        wg.run(st.get_entity(OP, tid, as_of="not-a-time"))


def test_centric_views_return_authorized_linked_artifacts(wg):
    allowed = {"art-1"}
    st = store(wg, hook=lambda p, aid: aid in allowed and p.get("id") == "user:alice")
    wg.run(st.apply_events(scenario().events))
    alice = member("alice", ["p1-r0"])
    for ref in ("task:p1-r0-t0", "issue:acme/app#7", "project:p1"):
        v = wg.run(st.centric(alice, ref, depth=3))
        assert [a["native_id"] for a in v["groups"].get("artifact", [])] == ["art-1"], ref
        assert sorted(x["native_id"] for x in v["groups"].get("artifact_version", [])) == ["ver-1", "ver-2"], ref
    # the registry's gate says no -> the artifact AND its versions vanish from every lookup
    allowed.clear()
    for ref in ("task:p1-r0-t0", "issue:acme/app#7", "project:p1"):
        v = wg.run(st.centric(alice, ref, depth=3))
        assert "artifact" not in v["groups"] and "artifact_version" not in v["groups"], ref
    with pytest.raises(wc.NotFoundOrForbidden):
        wg.run(st.get_entity(alice, "artifact:art-1"))


def test_layout_documents_cas_and_isolation(wg):
    st = store(wg)
    wg.run(st.apply_events(scenario().events))
    alice, bob = member("alice", ["p1-r0", "p1-r1"]), member("bob", ["p2-r0", "p2-r1"])
    pid = wc.urn_for("project", "p1")
    t_ok, t_other = wc.urn_for("task", "p1-r0-t0"), wc.urn_for("task", "p2-r0-t0")
    first = wg.run(st.get_layout(alice, "project:p1", which="default"))
    assert first["version"] == 0 and first["document"] == {"nodes": {}}
    with pytest.raises(wc.PreconditionRequired):
        wg.run(st.put_layout(alice, "project:p1", {"nodes": {}}, shared=True, if_match=None))
    doc = {"nodes": {t_ok: {"x": 1, "y": 2}, t_other: {"x": 9, "y": 9}, pid: {"x": 0, "y": 0, "collapsed": True}}}
    saved = wg.run(st.put_layout(alice, "project:p1", doc, shared=True, if_match=first["etag"]))
    assert saved["version"] == 1 and t_other not in saved["document"]["nodes"]          # hidden id stripped on PUT
    with pytest.raises(wc.PreconditionFailed):
        wg.run(st.put_layout(alice, "project:p1", doc, shared=True, if_match=first["etag"]))      # stale
    # per-user override is independent of the shared default and of other users
    mine0 = wg.run(st.get_layout(alice, "project:p1", which="mine"))
    wg.run(st.put_layout(alice, "project:p1", {"nodes": {t_ok: {"x": 50, "y": 50}}}, shared=False, if_match=mine0["etag"]))
    assert wg.run(st.get_layout(alice, "project:p1"))["document"]["nodes"][t_ok]["x"] == 50
    assert wg.run(st.get_layout(alice, "project:p1", which="default"))["document"]["nodes"][t_ok]["x"] == 1
    other = member("alice2", ["p1-r0"])
    assert wg.run(st.get_layout(other, "project:p1"))["document"]["nodes"][t_ok]["x"] == 1
    with pytest.raises(wc.NotFoundOrForbidden):
        wg.run(st.get_layout(bob, "project:p1"))
    # a shared layout whose visible set shrinks no longer names the hidden node
    narrow = member("alice3", ["p1-r1"])
    got = wg.run(st.get_layout(narrow, "project:p1", which="default"))
    assert t_ok not in got["document"]["nodes"]
    # graph rebuild does not touch layouts
    wg.run(st.clear_projection())
    assert wg.run(wg.pool.fetchval("SELECT count(*) FROM wg_layout")) == 2
    with pytest.raises(ValueError):
        wg.run(st.put_layout(alice, "project:p1", {"nodes": {"x": 5}}, shared=True, if_match="x"))
