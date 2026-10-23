# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""T09: room lineage graph, Gantt timeline, ledger and replay (read-only, membership-scoped).

Cast as in test_graph_membership (alice owner, bob contributor, carol viewer, dave guest, mallory in no room)."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane import graph as graph_mod
from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_links import link, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401
from tests.control_plane.test_graph_membership import fab, put  # noqa: F401


def lin(w, who, *tail, room=None, **q):
    q = {"room_id": room or w.room, **q}
    return w.s.graph_read(who, ["lineage", *tail], {k: str(v) for k, v in q.items() if v is not None})


def body(view):
    """The part of a lineage read that must be identical live and replayed."""
    return {"nodes": view["nodes"], "edges": view["edges"], "total_nodes": view["total_nodes"]}


def edge(f, etype, a, b, attrs=None, scope="room", nodes=True, **ctx):
    """An edge whose endpoints are defined first in the SAME room context (an edge to an undefined node is
    invisible by design)."""
    for n in (a, b):
        if nodes and n not in f.known:
            put(f, n, scope, **ctx)
    return f.edge(etype, a, b, attrs, scope=scope, nodes=False, **ctx)


def node(f, nid, scope="room", state=None, label=None, attrs=None, **ctx):
    """Upsert with the room/mission context in the ENVELOPE (where scopes are resolved)."""
    f.known.add(nid)
    body = {"node_type": nid.split(":")[0], "node_id": nid, "label": label, "state": state, "attrs": attrs}
    return f.send("node.upsert", {k: v for k, v in body.items() if v is not None}, scope, **ctx)


def err(fn, *a, **k):
    with pytest.raises(CollabError) as exc:
        fn(*a, **k)
    return exc.value.status, exc.value.message


def seed(fab, n=12):
    """Record, after every event the reader can see, the live graph; return [(step, live body)]."""
    f, snaps = fab.feed, {}
    room = fab.room

    def emit(kind, *a, **k):
        {"edge": edge, "node": node}.get(kind, lambda f, *x, **y: getattr(f, kind)(*x, **y))(f, *a, **k)
        for name, who in (("alice", fab.alice), ("bob", fab.bob), ("carol", fab.carol)):
            v = lin(fab, who)
            snaps.setdefault(name, {})[v["as_of"]["step"]] = body(v)
    emit("node", "agent:lead", label="Lead", state="running", room_id=room)
    emit("node", "agent:kid", label="Kid", room_id=room)
    emit("edge", "spawn", "agent:lead", "agent:kid", room_id=room)
    emit("node", "attempt:a1", state="running", room_id=room)
    emit("send", "node.state", {"node_id": "attempt:a1", "state": "done", "reason": "ok"}, "room", room_id=room)
    emit("node", "pr:github.com/o/r#1", label="PR", state="open", room_id=room)
    emit("edge", "reviewed", "review:r1", "pr:github.com/o/r#1", {"commit": "c1"}, scope="room", room_id=room)
    emit("edge", "reviewed", "review:r1", "pr:github.com/o/r#1", {"commit": "c2"}, scope="room", room_id=room)
    # another room's activity interleaves; nobody here may see it, and it must not disturb steps
    node(f, "task:other", room_id=fab.room2)
    emit("edge", "produced", "attempt:a1", "artifact:x", scope="room", room_id=room)
    return snaps


# ------------------------------------------------------------------ replay == live
def test_replay_at_every_step_equals_the_live_graph_at_that_point(fab):
    snaps = seed(fab)
    for name, who in (("alice", fab.alice), ("bob", fab.bob), ("carol", fab.carol)):
        head = lin(fab, who)
        assert head["as_of"]["head"] is True
        total = head["as_of"]["visible_events"]
        assert total >= 9 and set(snaps[name]) >= {1, total}
        for step, live in snaps[name].items():
            past = lin(fab, who, step=step)
            assert body(past) == live, (name, step)
            assert past["as_of"]["step"] == step and past["as_of"]["head"] == (step == total)
        assert body(lin(fab, who, step=total + 50)) == body(head)        # past the end clamps to now
        assert lin(fab, who, step=0)["nodes"] == []


def test_replay_survives_a_restart_rebuild(fab):
    seed(fab)
    before = {s: body(lin(fab, fab.bob, step=s)) for s in (1, 4, 7)}
    fresh = ControlPlaneRepo()
    fresh.import_state(json.loads(json.dumps(fab.repo.export_state(), default=str)))
    fab.s.graph_source = lambda: fresh.graph
    assert {s: body(lin(fab, fab.bob, step=s)) for s in (1, 4, 7)} == before
    assert body(lin(fab, fab.bob)) == body(lin(fab, fab.bob, step=999))


def test_replay_is_deterministic_and_cached_reads_equal_cold_reads(fab):
    seed(fab)
    a = lin(fab, fab.bob, step=5)
    fab.repo.graph._replay_cache.clear()
    assert lin(fab, fab.bob, step=5) == a
    lin(fab, fab.bob, step=3)
    assert lin(fab, fab.bob, step=5) == a


# ------------------------------------------------------------------ no widening
def test_a_later_narrowing_hides_the_past_too(fab):
    f = fab.feed
    put(f, "task:t", "room", label="visible then", room_id=fab.room)
    put(f, "task:keep", "room", label="stays", room_id=fab.room)
    assert {n["id"] for n in lin(fab, fab.bob)["nodes"]} == {"task:t", "task:keep"}
    put(f, "task:t", "private", label="private now")                   # the writer narrows it later
    put(f, "task:keep2", "room", label="after", room_id=fab.room)
    for step in (0, 1):                                                  # strictly before the head
        got = lin(fab, fab.bob, step=step)
        assert got["as_of"]["head"] is False and "task:t" not in json.dumps(got), step
        assert "task:t" not in json.dumps(lin(fab, fab.bob, "timeline", step=step))
    assert {n["id"] for n in lin(fab, fab.bob, step=1)["nodes"]} == {"task:keep"}
    ledger = lin(fab, fab.bob, "events")
    assert "task:t" not in json.dumps(ledger) and "visible then" not in json.dumps(ledger)
    assert "task:t" not in json.dumps(lin(fab, fab.bob, "timeline"))
    # the same replay without the live gate WOULD show it (this is what the gate is for)
    from herdr_engineering.control_plane.graph import GraphProjection, _lseq, _rank
    g = fab.repo.graph
    ungated = GraphProjection(keep_log=False)
    for ev in sorted((e for e, _ in g._log if _lseq(e) <= 2), key=_rank):
        ungated.apply(ev)
    assert "task:t" in json.dumps(ungated.room_graph(
        {"kind": "member", "id": "user:bob", "authenticated_by": "collab", "access": fab.s.graph_access(fab.bob)},
        frozenset({("room", fab.room, None)})))


def test_a_later_narrowing_of_an_edge_hides_it_in_the_past(fab):
    f = fab.feed
    put(f, "task:a", "room", room_id=fab.room)
    put(f, "task:b", "room", room_id=fab.room)
    edge(f, "dependency", "task:a", "task:b", scope="room", room_id=fab.room)
    put(f, "task:c", "room", room_id=fab.room)
    edge(f, "dependency", "task:a", "task:b", scope="private")            # same edge, re-scoped narrower
    put(f, "task:d", "room", room_id=fab.room)
    assert lin(fab, fab.bob)["edges"] == []
    for step in (2, 3):
        got = lin(fab, fab.bob, step=step)
        assert got["as_of"]["head"] is False and got["edges"] == []
        assert lin(fab, fab.bob, "timeline", step=step)["markers"] == []


def test_rescoping_into_another_room_hides_the_past_from_both_rooms(fab):
    f = fab.feed
    put(f, "task:x", "room", room_id=fab.room)
    put(f, "task:y", "room", room_id=fab.room)
    put(f, "task:x", "room", room_id=fab.room2)
    put(f, "task:z", "room", room_id=fab.room)
    for who, room in ((fab.alice, fab.room), (fab.alice, fab.room2), (fab.bob, fab.room)):
        for step in (0, 1, 2, None):
            assert "task:x" not in json.dumps(lin(fab, who, room=room, step=step))


def test_removed_edges_are_history_not_narrowing(fab):
    f = fab.feed
    put(f, "task:a", "room", room_id=fab.room)
    put(f, "task:b", "room", room_id=fab.room)
    edge(f, "dependency", "task:a", "task:b", scope="room", room_id=fab.room)
    f.send("edge.remove", {"edge_type": "dependency", "from_id": "task:a", "to_id": "task:b", "reason": "r"},
           "room", room_id=fab.room)
    assert lin(fab, fab.bob)["edges"] == []
    assert len(lin(fab, fab.bob, step=3)["edges"]) == 1          # still shown as it was at that point
    assert lin(fab, fab.bob, step=4)["edges"] == []


def test_a_hidden_binding_never_leaks_through_stale_or_bindings(fab):
    f = fab.feed
    pr = "pr:github.com/o/r#9"
    put(f, pr, "room", room_id=fab.room)
    edge(f, "reviewed", "review:r", pr, {"commit": "c1"}, scope="room", room_id=fab.room)
    edge(f, "reviewed", "review:r", pr, {"commit": "c-secret"}, scope="private")
    for step in (None, 2, 3):
        blob = json.dumps(lin(fab, fab.bob, step=step)) + json.dumps(lin(fab, fab.bob, "timeline", step=step))
        assert "c-secret" not in blob


# ------------------------------------------------------------------ access
def test_non_members_guests_and_agents_get_one_404_and_revocation_is_immediate(fab):
    seed(fab)
    agent, _ = fab.agent("alice")
    miss = err(lin, fab, fab.mallory, room="room-never-existed")
    for who in (fab.mallory, fab.dave, agent):
        for tail in ((), ("timeline",), ("events",)):
            assert err(lin, fab, who, *tail) == miss
    assert err(lin, fab, fab.alice, room="room-never-existed") == miss
    assert err(lin, fab, fab.bob, room=fab.room2) == miss        # a member of ANOTHER room only
    steps = range(0, 12)
    fab.s.remove_member(fab.alice, fab.room, "carol")
    for step in steps:
        assert err(lin, fab, fab.carol, step=step) == miss
        assert err(lin, fab, fab.carol, "timeline", step=step) == miss
    fab.s.add_member(fab.alice, fab.room, {"user_id": "carol", "role": "guest"})
    assert err(lin, fab, fab.carol) == miss


def test_replay_uses_the_readers_current_membership_not_the_pasts(fab):
    f = fab.feed
    put(f, "task:old", "room", room_id=fab.room)
    fab.s.add_member(fab.alice, fab.room, {"user_id": "mallory", "role": "viewer"})
    # she joins AFTER the event and can replay it: membership is evaluated now, like every other read
    assert [n["id"] for n in lin(fab, fab.mallory, step=1)["nodes"]] == ["task:old"]
    fab.s.remove_member(fab.alice, fab.room, "mallory")
    assert err(lin, fab, fab.mallory, step=1)[0] == 404


def test_missions_and_workspace_gates_apply_to_events_and_replay(fab):
    fab.s.link_mission(fab.alice, fab.room, {"mission_id": "m-1", "members": ["bob"]})
    put(fab.feed, "task:m", "mission", room_id=fab.room, mission_id="m-1")
    put(fab.feed, "task:w", "room", room_id=fab.room, workspace_id="ws-1")
    assert [n["id"] for n in lin(fab, fab.bob)["nodes"]] == ["task:m"]
    assert lin(fab, fab.carol)["nodes"] == []                    # room member, not a mission member
    assert [e["node_id"] for e in lin(fab, fab.bob, "events")["events"]] == ["task:m"]
    link(fab, ws="ws-1")
    assert {n["id"] for n in lin(fab, fab.bob)["nodes"]} == {"task:m", "task:w"}
    fab.s.unlink_mission(fab.alice, fab.room, "m-1")
    assert {n["id"] for n in lin(fab, fab.bob, step=2)["nodes"]} == {"task:w"}


def test_other_rooms_events_never_appear_in_the_ledger_or_timeline(fab):
    f = fab.feed
    put(f, "task:mine", "room", room_id=fab.room)
    for i in range(30):
        put(f, f"task:r2-{i}", "room", room_id=fab.room2)
    edge(f, "dependency", "task:mine", "task:r2-0", scope="room", room_id=fab.room2)
    ledger = lin(fab, fab.bob, "events")
    assert [e["node_id"] for e in ledger["events"]] == ["task:mine"] and ledger["visible_events"] == 1
    tl = lin(fab, fab.bob, "timeline")
    assert [x["node_id"] for x in tl["lanes"]] == ["task:mine"] and tl["markers"] == []
    assert "r2-" not in json.dumps([ledger, tl])
    # steps number the reader's own events, so hidden traffic cannot be counted
    assert ledger["events"][0]["step"] == 1


def test_operator_key_scopes_do_not_matter_here_agents_and_keys_have_no_lineage_route(fab):
    st, _ = fab.s.dispatch("GET", ["graph", "lineage"], {"room_id": fab.room}, {}, "Bearer nope", None)
    assert st in (401, 403, 404)


# ------------------------------------------------------------------ budgets and validation
def test_invalid_parameters_are_400_and_unknown_routes_404(fab):
    seed(fab)
    for q in ({"step": "x"}, {"step": "-1"}, {"at": "yesterday"}, {"step": "1", "at": "2026-10-22T10:00:00Z"},
              {"limit": "0"}, {"limit": "5000"}, {"offset": "-1"}, {"node_types": "robot"},
              {"edge_types": "friendship"}, {"node_types": ","}):
        assert err(lin, fab, fab.bob, **q)[0] == 400, q
    assert err(lin, fab, fab.bob, "events", cursor="-1")[0] == 400
    assert err(lin, fab, fab.bob, "events", limit="501")[0] == 400
    assert err(lin, fab, fab.bob, "nope")[0] == 404
    assert err(fab.s.graph_read, fab.bob, ["lineage"], {})[0] == 400          # room_id is required


def test_pagination_walks_every_node_once_and_edges_between_pages_are_kept(fab):
    f = fab.feed
    for i in range(7):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    for i in range(6):
        edge(f, "dependency", f"task:t{i}", f"task:t{i + 1}", scope="room", room_id=fab.room)
    seen, edges, off = [], {}, 0
    while off is not None:
        page = lin(fab, fab.bob, limit=3, offset=off)
        seen += [n["id"] for n in page["nodes"]]
        edges.update({(e["from_id"], e["to_id"]): e for e in page["edges"]})
        off = page["next_offset"]
    assert seen == [f"task:t{i}" for i in range(7)] and len(edges) == 6
    whole = lin(fab, fab.bob)
    assert {(e["from_id"], e["to_id"]) for e in whole["edges"]} == set(edges)
    first = lin(fab, fab.bob, limit=3)
    assert first["truncated"] and first["truncated_reason"] == "limit" and first["total_nodes"] == 7


def test_ledger_pages_and_scan_budget(fab, monkeypatch):
    f = fab.feed
    for i in range(9):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    p1 = lin(fab, fab.bob, "events", limit=4)
    p2 = lin(fab, fab.bob, "events", limit=4, cursor=p1["next_cursor"])
    p3 = lin(fab, fab.bob, "events", limit=4, cursor=p2["next_cursor"])
    assert [e["step"] for e in p1["events"] + p2["events"] + p3["events"]] == list(range(1, 10))
    assert p3["next_cursor"] is None and not p1["truncated"]
    monkeypatch.setattr(graph_mod, "LEDGER_SCAN_BUDGET", 5)
    cut = lin(fab, fab.bob, "events", limit=50)
    assert cut["truncated"] and cut["truncated_reason"] == "scan_budget" and len(cut["events"]) == 5
    assert [e["node_id"] for e in cut["events"]] == [f"task:t{i}" for i in range(4, 9)]     # the newest window
    # replay inside a trimmed window: step 0 is the state just before the window, head is now
    assert len(lin(fab, fab.bob, step=0)["nodes"]) == 4
    assert len(lin(fab, fab.bob)["nodes"]) == 9


def test_replay_prefix_cap_and_graph_budgets(fab, monkeypatch):
    f = fab.feed
    for i in range(6):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    monkeypatch.setattr(graph_mod, "REPLAY_MAX_EVENTS", 3)
    assert err(lin, fab, fab.bob, step=5)[0] == 400
    assert len(lin(fab, fab.bob)["nodes"]) == 6                    # now is never capped (it is the live graph)
    assert len(lin(fab, fab.bob, step=2)["nodes"]) == 2
    monkeypatch.setattr(graph_mod, "SCAN_BUDGET", 4)
    cut = lin(fab, fab.bob)
    assert cut["truncated_reason"] == "scan_budget" and len(cut["nodes"]) <= 4
    monkeypatch.setattr(graph_mod, "MAX_EDGES", 1)
    edge(f, "dependency", "task:t0", "task:t1", scope="room", room_id=fab.room)
    edge(f, "dependency", "task:t1", "task:t2", scope="room", room_id=fab.room)
    monkeypatch.setattr(graph_mod, "SCAN_BUDGET", 3000)
    big = lin(fab, fab.bob)
    assert len(big["edges"]) == 1 and big["truncated_reason"] == "limit"


def test_response_byte_budget(fab, monkeypatch):
    for i in range(6):
        put(fab.feed, f"task:t{i}", "room", room_id=fab.room, label="x" * 400)
    monkeypatch.setattr(graph_mod, "MAX_RESPONSE_BYTES", 2500)
    cut = lin(fab, fab.bob)
    assert cut["truncated_reason"] == "response_bytes" and 0 < len(cut["nodes"]) < 6
    assert cut["next_offset"] == len(cut["nodes"])


def test_lineage_reads_are_rate_limited_per_person(fab):
    put(fab.feed, "task:t", "room", room_id=fab.room)
    for _ in range(240):
        lin(fab, fab.bob)
    assert err(lin, fab, fab.bob)[0] == 429
    assert lin(fab, fab.carol)["nodes"]                            # someone else is unaffected


# ------------------------------------------------------------------ timeline
def test_timeline_spans_markers_head_change_and_stale(fab):
    f = fab.feed
    room = fab.room
    pr = "pr:github.com/o/r#1"
    put(f, "agent:lead", "room", room_id=room)
    node(f, "attempt:a1", state="running", scope="room", room_id=room)
    f.send("node.state", {"node_id": "attempt:a1", "state": "blocked", "reason": "waiting"}, "room", room_id=room)
    f.send("node.state", {"node_id": "attempt:a1", "state": "done"}, "room", room_id=room)
    node(f, "session:s1", state="running", scope="room", room_id=room)
    edge(f, "spawn", "agent:lead", "attempt:a1", scope="room", room_id=room)
    edge(f, "handoff", "agent:lead", "agent:peer", scope="room", room_id=room)
    node(f, pr, label="PR", scope="room", room_id=room)
    edge(f, "reviewed", "review:r1", pr, {"commit": "c1"}, scope="room", room_id=room)
    edge(f, "reviewed", "review:r1", pr, {"commit": "c2"}, scope="room", room_id=room)
    tl = lin(fab, fab.bob, "timeline")
    lanes = {x["node_id"]: x for x in tl["lanes"]}
    spans = lanes["attempt:a1"]["spans"]
    assert [(s["state"], s["open"], s["terminal"]) for s in spans] == [
        ("running", False, False), ("blocked", False, False), ("done", False, True)]
    assert spans[0]["end"] == spans[1]["start"] and spans[1]["reason"] == "waiting"
    assert lanes["session:s1"]["spans"][0]["open"] is True and lanes["session:s1"]["spans"][0]["end"] is None
    kinds = {(m["kind"], m["edge_type"]) for m in tl["markers"]}
    assert {("edge", "spawn"), ("edge", "handoff"), ("edge", "reviewed"), ("head_change", "reviewed")} <= kinds
    rev = [m for m in tl["markers"] if m["edge_type"] == "reviewed"]
    assert [(m["kind"], m["binding"], m["stale"]) for m in rev] == [
        ("edge", {"commit": "c1"}, True), ("head_change", {"commit": "c2"}, False)]
    # as of the moment before the head changed, the first review was still current
    steps = [m["step"] for m in rev]
    before = lin(fab, fab.bob, "timeline", step=steps[1] - 1)
    assert [m["stale"] for m in before["markers"] if m["edge_type"] == "reviewed"] == [False]
    assert before["as_of"]["step"] == steps[1] - 1 and "head_change" not in {m["kind"] for m in before["markers"]}
    # a replayed Gantt shows the lane in the state it had
    mid = lin(fab, fab.bob, "timeline", step=2)
    assert mid["lanes"][-1]["spans"][-1]["state"] == "running" and mid["lanes"][-1]["spans"][-1]["open"]
    assert tl["range"]["start"] and tl["range"]["end"]


def test_timeline_caps_lanes_and_markers(fab, monkeypatch):
    f = fab.feed
    for i in range(6):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    for i in range(5):
        edge(f, "dependency", f"task:t{i}", f"task:t{i + 1}", scope="room", room_id=fab.room)
    monkeypatch.setattr(graph_mod, "TIMELINE_MAX_LANES", 4)
    monkeypatch.setattr(graph_mod, "TIMELINE_MAX_MARKERS", 2)
    tl = lin(fab, fab.bob, "timeline")
    assert len(tl["lanes"]) <= 4 and len(tl["markers"]) == 2 and tl["truncated"]
    assert tl["truncated_reason"] in ("marker_limit", "lane_limit")


def test_position_by_time_is_a_prefix_of_receipt_order(fab, monkeypatch):
    clock = iter(f"2026-10-22T10:00:{i:02d}+00:00" for i in range(60))
    monkeypatch.setattr(fab.repo, "_stamp", lambda: next(clock))
    for i in range(5):
        put(fab.feed, f"task:t{i}", "room", room_id=fab.room)
    stamps = [e["received_at"] for e in lin(fab, fab.bob, "events")["events"]]
    assert len(set(stamps)) == 5
    for k, at in enumerate(stamps, 1):
        got = lin(fab, fab.bob, at=at)
        assert got["as_of"]["step"] == k and body(got) == body(lin(fab, fab.bob, step=k))
    assert lin(fab, fab.bob, at="2000-01-01T00:00:00Z")["nodes"] == []
    assert lin(fab, fab.bob, at="2999-01-01T00:00:00Z")["as_of"]["head"] is True


def test_event_views_are_allow_listed_and_hostile_ids_stay_data(fab):
    hostile = '<img src=x onerror=alert(1)>"onmouseover="x'
    put(fab.feed, "task:evil", "room", room_id=fab.room, label=hostile)
    fab.feed.send("node.upsert", {"node_type": "task", "node_id": "task:evil", "attrs": {"secret_attr": "S3CR3T"}},
                  "room", room_id=fab.room)
    ledger = lin(fab, fab.bob, "events")
    assert ledger["events"][0]["label"] == hostile
    assert "S3CR3T" not in json.dumps([ledger, lin(fab, fab.bob, "timeline")])
    assert set(ledger["events"][0]) <= {"step", "event_id", "event_type", "source", "received_at", "occurred_at",
                                        "node_id", "node_type", "state", "label", "reason"}


def test_http_route_wiring_matches_graph_read(fab):
    seed(fab)
    st, got = fab.s.dispatch("GET", ["graph", "lineage"], {"room_id": fab.room, "step": "3"}, {},
                             f"Bearer {USERS['bob']}", None)
    assert st == 200 and got["as_of"]["step"] == 3
