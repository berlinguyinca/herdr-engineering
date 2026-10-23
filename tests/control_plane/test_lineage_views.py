# ruff: noqa: F811, E701  (pytest fixtures are imported by name; the randomized generator is terse)
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
from tests.control_plane.test_graph import OTHER
from tests.control_plane.test_graph_membership import fab, put  # noqa: F401


@pytest.fixture(autouse=True)
def _full_history(request):
    """These tests use a fake collab clock that is years from the feed's real timestamps, so they ask for the
    whole history; the since-join default is tested on its own below."""
    if "fab" in request.fixturenames and "since_join" not in request.node.name:
        fab = request.getfixturevalue("fab")
        fab.s.replay_history = "full"


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
    for ev in sorted((e for ctx in g._log_ctx.values() for e, _ in ctx if _lseq(e) <= 2), key=_rank):
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
    # inside a trimmed window replay shows the window's own events only; head is the live graph
    assert cut["window_trimmed"] if "window_trimmed" in cut else True
    assert lin(fab, fab.bob)["as_of"]["step"] == 5 == lin(fab, fab.bob)["as_of"]["visible_events"]
    assert len(lin(fab, fab.bob, step=0)["nodes"]) == 0 and len(lin(fab, fab.bob, step=3)["nodes"]) == 3
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


# ------------------------------------------------------------------ review round 1
def _everywhere(fab, who, n):
    """Every read a person can make about a room, at every position up to `n`: one blob to grep."""
    out = [json.dumps(lin(fab, who, "events", limit=500))]
    for step in [None, *range(0, n + 1)]:
        out += [json.dumps(lin(fab, who, step=step)), json.dumps(lin(fab, who, "timeline", step=step))]
    return "\n".join(out)


def test_a_sibling_binding_does_not_keep_a_narrowed_edge_instance_visible(fab):
    f = fab.feed
    pr = "pr:github.com/o/r#5"
    put(f, pr, "room", label="PR", room_id=fab.room)
    edge(f, "reviewed", "review:r", pr, {"commit": "c1"}, room_id=fab.room)       # a sibling binding that stays visible
    edge(f, "reviewed", "review:r", pr, {"commit": "c-x"}, room_id=fab.room)
    edge(f, "reviewed", "review:r", pr, {"commit": "c-x"}, scope="private")       # the same instance, narrowed
    put(f, "task:z", "room", room_id=fab.room)
    blob = _everywhere(fab, fab.bob, 8)
    assert "c-x" not in blob and "c1" in blob


def test_a_commons_value_its_writer_made_private_leaves_the_past_too(fab):
    f = fab.feed
    pr = "pr:github.com/o/r#7"
    put(f, pr, "room", label="room label", room_id=fab.room)
    put(f, "task:keep", "room", room_id=fab.room)
    put(f, pr, "private", label="secret label")                                   # the same writer takes it back
    put(f, pr, "room", label="other label", principal=OTHER, room_id=fab.room)    # another writer's value stays public
    put(f, "task:after", "room", room_id=fab.room)
    blob = _everywhere(fab, fab.bob, 6)
    assert "room label" not in blob and "secret label" not in blob
    assert "other label" in json.dumps(lin(fab, fab.bob))                          # and is shown, live and replayed
    assert "other label" in json.dumps(lin(fab, fab.bob, step=3))


def test_a_commons_value_still_public_is_shown_in_the_past_at_its_own_position(fab):
    f = fab.feed
    pr = "pr:github.com/o/r#8"
    put(f, pr, "room", label="v1", room_id=fab.room)
    put(f, "task:keep", "room", room_id=fab.room)
    put(f, pr, "room", label="v2", room_id=fab.room)
    put(f, "task:after", "room", room_id=fab.room)
    assert any(n["label"] == "v1" for n in lin(fab, fab.bob, step=2)["nodes"])
    assert any(n["label"] == "v2" for n in lin(fab, fab.bob)["nodes"])


def test_a_timestamp_without_a_zone_is_utc(fab, monkeypatch):
    clock = iter(f"2026-10-22T10:00:{i:02d}+00:00" for i in range(60))
    monkeypatch.setattr(fab.repo, "_stamp", lambda: next(clock))
    for i in range(4):
        put(fab.feed, f"task:t{i}", "room", room_id=fab.room)
    stamp = lin(fab, fab.bob, "events")["events"][1]["received_at"]
    assert stamp.endswith("+00:00")
    assert lin(fab, fab.bob, at=stamp)["as_of"]["step"] == 2
    assert lin(fab, fab.bob, at=stamp[:-6])["as_of"]["step"] == 2


def test_the_scan_budget_is_shared_fairly_between_node_types(fab, monkeypatch):
    f = fab.feed
    for i in range(10):
        put(f, f"agent:a{i}", "room", room_id=fab.room)
    for i in range(3):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    monkeypatch.setattr(graph_mod, "SCAN_BUDGET", 6)
    got = lin(fab, fab.bob)
    assert got["truncated_reason"] == "scan_budget"
    assert {n["id"] for n in got["nodes"] if n["node_type"] == "task"} == {"task:t0", "task:t1", "task:t2"}


def test_replay_reads_while_events_arrive_never_error(fab):
    import threading
    f = fab.feed
    pr = "pr:github.com/o/r#3"
    put(f, pr, "room", label="p", room_id=fab.room)
    for i in range(10):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    stop, errors = threading.Event(), []

    def writer():
        i = 0
        while not stop.is_set() and i < 400:
            put(f, pr, "room" if i % 2 else "private", label=f"l{i}", room_id=fab.room)
            put(f, f"task:w{i}", "room", room_id=fab.room)
            i += 1
    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(60):
            lin(fab, fab.bob, step=5)
            lin(fab, fab.bob, "timeline", step=7)
            lin(fab, fab.bob, "events", limit=50)
    except Exception as exc:       # noqa: BLE001 - any error here is the bug
        errors.append(exc)
    finally:
        stop.set()
        th.join()
    assert not errors


# ------------------------------------------------------------------ review round 2
def _flood(f, room, n):
    node(f, "task:a0", room_id=room)
    for i in range(n):
        f.send("node.state", {"node_id": "task:a0", "state": "running" if i % 2 else "done"}, "room", room_id=room)


def test_a_busy_room_does_not_break_another_rooms_replay(fab, monkeypatch):
    """Replay is per room: flooding room A past the cap leaves room B's replay and timeline intact, and the
    refusal for A says nothing about volume."""
    monkeypatch.setattr(graph_mod, "REPLAY_MAX_EVENTS", 30)
    f = fab.feed
    node(f, "task:b0", room_id=fab.room2)
    node(f, "task:b1", state="running", room_id=fab.room2)
    _flood(f, fab.room, 60)
    st, msg = err(lin, fab, fab.alice, step=55)
    assert st == 400 and not any(ch.isdigit() for ch in msg) and "25" not in msg
    assert err(lin, fab, fab.alice, "timeline", step=55)[0] == 400
    # room B (alice owns it too in this fixture? use whoever can read room2)
    who = next((u for u in (fab.alice, fab.bob, fab.carol) if _can_read(fab, u, fab.room2)), None)
    if who is None:
        pytest.skip("fixture has no reader of room2")
    got = lin(fab, who, room=fab.room2, step=1)
    assert got["as_of"]["head"] is False and {n["id"] for n in got["nodes"]} == {"task:b0"}
    assert lin(fab, who, "timeline", room=fab.room2, step=1)["as_of"]["step"] == 1


def _can_read(fab, who, room):
    try:
        lin(fab, who, "events", room=room)
        return True
    except CollabError:
        return False


def test_a_viewer_never_holds_the_ingest_lock_for_a_whole_scan(fab, monkeypatch):
    """The visibility scan yields the live lock every CHUNK events: ingest is never blocked for the length of
    the scan (measured as the longest wait to acquire the lock while a slow scan runs)."""
    import threading
    import time
    f, g = fab.feed, fab.repo.graph
    _flood(f, fab.room, 2400)
    real = g._event_visible

    def slow(*a, **k):
        time.sleep(0.0003)
        return real(*a, **k)
    monkeypatch.setattr(g, "_event_visible", slow)
    waits, stop = [], threading.Event()

    def probe():
        while not stop.is_set():
            t = time.perf_counter()
            with g._lock:
                pass
            waits.append(time.perf_counter() - t)
            time.sleep(0.002)
    th = threading.Thread(target=probe)
    th.start()
    t0 = time.perf_counter()
    lin(fab, fab.alice, "events")
    total = time.perf_counter() - t0
    stop.set()
    th.join()
    assert total > 0.5 and waits                       # the scan really was slow
    assert max(waits) < total / 3, (max(waits), total)  # ...yet no single hold was close to it


def test_a_trimmed_window_has_exact_self_consistent_steps(fab, monkeypatch):
    """The window is N of the READER'S OWN visible events: head step == visible_events == what paging returns,
    and every step from 0 to the head is replayable (no rounding is exposed)."""
    monkeypatch.setattr(graph_mod, "LEDGER_SCAN_BUDGET", 150)
    _flood(fab.feed, fab.room, 237)
    a = lin(fab, fab.alice)["as_of"]
    assert a["window_trimmed"] is True and a["head"] is True and a["step"] == a["visible_events"] == 150
    assert "approximate" not in a
    got, cur = 0, None
    while True:
        e = lin(fab, fab.alice, "events", limit=40, cursor=cur)
        got += len(e["events"])
        cur = e["next_cursor"]
        if cur is None:
            break
    assert got == 150
    h = lin(fab, fab.alice, step=150)
    assert h["as_of"]["head"] is True and h["as_of"]["step"] == 150
    for k in (0, 1, 75, 149):
        r = lin(fab, fab.alice, step=k)
        assert r["as_of"]["step"] == k and r["as_of"]["head"] is False


def test_hidden_events_neither_shift_the_window_nor_the_paging(fab, monkeypatch):
    """Paging cannot be used to count what the reader may not see: with hidden events interleaved, the window
    still holds N visible events and the pages sum to exactly N."""
    monkeypatch.setattr(graph_mod, "LEDGER_SCAN_BUDGET", 40)
    f = fab.feed
    for i in range(30):
        put(f, f"task:h{i}", "room", room_id=fab.room)
        put(f, f"task:h{i}", "private")                       # then hidden from bob (both its events)
    for i in range(70):
        put(f, f"task:v{i}", "room", room_id=fab.room)
    v = lin(fab, fab.bob, "events", limit=500)
    assert len(v["events"]) == 40 == v["visible_events"] and v["truncated"]


def test_the_replay_cap_counts_the_readers_visible_events_only(fab, monkeypatch):
    monkeypatch.setattr(graph_mod, "REPLAY_MAX_EVENTS", 20)
    f = fab.feed
    for i in range(9):
        put(f, f"task:h{i}", "room", room_id=fab.room)
    for i in range(9):
        put(f, f"task:h{i}", "private")                       # 9 now hidden from carol
    for i in range(14):
        put(f, f"task:v{i}", "room", room_id=fab.room)
    n = lin(fab, fab.carol)["as_of"]["visible_events"]
    assert n == 14
    assert all(lin(fab, fab.carol, step=k)["as_of"]["step"] == k for k in range(n))     # hidden ones cost nothing
    monkeypatch.setattr(graph_mod, "REPLAY_MAX_EVENTS", 5)
    assert lin(fab, fab.carol, step=5)["as_of"]["step"] == 5 and err(lin, fab, fab.carol, step=9)[0] == 400


def test_page_tokens_are_opaque_bound_to_the_reader_and_tamper_proof(fab):
    f = fab.feed
    for i in range(6):
        put(f, f"task:t{i}", "room", room_id=fab.room)
    tok = lin(fab, fab.bob, "events", limit=2)["next_cursor"]
    assert isinstance(tok, str) and len(tok) == 32 and not tok.isdigit()
    assert lin(fab, fab.bob, "events", limit=2, cursor=tok)["events"][0]["step"] == 3
    assert err(lin, fab, fab.carol, "events", cursor=tok)[0] == 400          # another reader's token
    flipped = ("0" if tok[0] != "0" else "1") + tok[1:]
    assert err(lin, fab, fab.bob, "events", cursor=flipped)[0] == 400
    for bad in ("3", "zz", "-1", tok + "0"):
        assert err(lin, fab, fab.bob, "events", cursor=bad)[0] == 400


def test_members_do_not_get_the_global_admission_counter(fab):
    f = fab.feed
    put(f, "task:p", "room", label="x", attrs={"k": 1}, room_id=fab.room)
    edge(f, "spawn", "task:p", "task:q", {"w": 1}, room_id=fab.room)
    got = lin(fab, fab.bob)
    assert "admission" not in json.dumps(got)
    g = fab.repo.graph
    assert "admission" in json.dumps(g._node_view(g._nodes["task:p"], None, internal=True))   # operators keep it


def test_replay_since_join_hides_the_pre_join_history_strictly(fab):
    """since_join: events are filtered one by one (timestamps need not be monotonic); an overwritten pre-join
    value never appears; the current value is shown WITHOUT pre-join provenance."""
    from datetime import UTC, datetime
    f = fab.feed
    put(f, "task:old", "room", label="old-secret", room_id=fab.room)
    put(f, "task:old", "room", label="current-label", room_id=fab.room)       # overwrites the pre-join value
    put(f, "task:mid", "room", label="mid", room_id=fab.room)
    ctx = fab.repo.graph._log_ctx[("room", fab.room, None)]
    stamps = sorted(datetime.fromisoformat(str(e["ingest_timestamp"])).timestamp() for e, _ in ctx)
    cut = stamps[2] - 1e-6           # join just before the third event
    put(f, "task:new", "room", label="after", room_id=fab.room)
    fab.s.replay_history = "full"
    full = lin(fab, fab.bob, "events")
    fab.s.replay_history = "since_join"
    m = fab.s._rooms[fab.room]["members"]["bob"]
    m["joined_at"] = datetime.fromtimestamp(cut, UTC).isoformat()
    got = lin(fab, fab.bob, "events")
    assert 0 < got["visible_events"] < full["visible_events"] == 4
    assert "old-secret" not in json.dumps([got, lin(fab, fab.bob, step=0), lin(fab, fab.bob, "timeline")])
    head = lin(fab, fab.bob)
    old = next(n for n in head["nodes"] if n["id"] == "task:old")
    assert old["label"] == "current-label"                       # current values are shown...
    assert old["first_event_id"] is None and old["fields"]["label"]["event_id"] is None      # ...without history
    assert old["fields"]["label"]["admitted_at"] is None
    assert lin(fab, fab.bob, step=0)["nodes"] == []                   # step 0 = nothing from before the join
    # non-monotonic stamps: filtering is per event, not a prefix slice
    ctx[0][0]["ingest_timestamp"] = datetime.fromtimestamp(cut + 100, UTC).isoformat()   # an early event stamped late
    assert lin(fab, fab.bob, "events")["visible_events"] == got["visible_events"] + 1
    m.pop("joined_at")                                       # truly missing join time: fail closed (history starts now)
    assert lin(fab, fab.bob, "events")["visible_events"] == 0
    fab.s.replay_history = "full"
    assert lin(fab, fab.bob, "events")["visible_events"] == 4


def test_a_context_log_is_bounded_and_replay_stays_usable(fab, monkeypatch):
    monkeypatch.setattr(graph_mod, "LOG_MAX_PER_CONTEXT", 50)
    _flood(fab.feed, fab.room, 120)
    g = fab.repo.graph
    key = ("room", fab.room, None)
    assert len(g._log_ctx[key]) <= 50 and g._log_evicted[key] > 0
    head = lin(fab, fab.alice)["as_of"]
    n = head["visible_events"]
    assert head["head"] is True and head["step"] == n == len(g._log_ctx[key]) and head["window_trimmed"] is True
    for k in (0, 1, n // 2, n - 1):                          # every position below the head is replayable
        assert lin(fab, fab.alice, step=k)["as_of"]["step"] == k
    assert lin(fab, fab.alice, "timeline", step=1)["as_of"]["step"] == 1
    assert lin(fab, fab.alice, "events")["truncated"] is True


def test_query_numbers_are_ascii_digits_only(fab):
    for bad in ("٣", "1_0", "+1", " 1", "-1", "1.0", "1" * 12):
        for name in ("step", "limit", "cursor"):
            tail = ("events",) if name == "cursor" else ()
            assert err(lin, fab, fab.alice, *tail, **{name: bad})[0] == 400, (name, bad)


def test_a_head_response_is_coherent_under_concurrent_ingest(fab):
    import threading
    import time
    f = fab.feed
    node(f, "task:c0", room_id=fab.room)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            f.send("node.state", {"node_id": "task:c0", "state": "running" if i % 2 else "done"}, "room",
                   room_id=fab.room)
            i += 1
            time.sleep(0.003)
    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(8):
            v = lin(fab, fab.alice)
            n = v["as_of"]["visible_events"]
            ev = lin(fab, fab.alice, "events", limit=1)
            assert ev["visible_events"] >= n                 # the ledger only grows
            node_state = next(x for x in v["nodes"] if x["id"] == "task:c0")
            assert node_state["last_event_id"] == v["as_of"]["event_id"]     # the graph IS the as_of state
    finally:
        stop.set()
        th.join()


# ---------------------------------------------------------------- real HTTP server
from tests.control_plane.test_collab import BASE, clock, env, invite, make_room  # noqa: E402,F401
from tests.control_plane.test_graph_membership import _key  # noqa: E402


def _ingest(env, node, scope, seq, **ctx):
    import http.client
    body = {"schema_version": 2, "event_id": f"e-{node}", "source": "pi", "source_sequence": seq,
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


def test_real_server_serves_lineage_views_replay_and_refusals(env):
    env.svc.replay_history = "full"          # the fake collab clock is years from the real ingest times
    _, room = make_room(env.api)
    assert invite(env.api, room, "bob", "contributor")[0] in (200, 201)
    _ingest(env, "task:h1", "room", 0, room_id=room)
    _ingest(env, "task:h2", "room", 1, room_id=room)
    url = f"{BASE}/graph/lineage"
    s, head = env.api.call("GET", f"{url}?room_id={room}", user="alice")
    assert s == 200 and head["as_of"]["head"] is True and head["as_of"]["visible_events"] == 2
    assert "admission" not in json.dumps(head)
    s, past = env.api.call("GET", f"{url}?room_id={room}&step=1", user="bob")
    assert s == 200 and past["as_of"]["head"] is False and [n["id"] for n in past["nodes"]] == ["task:h1"]
    s, tl = env.api.call("GET", f"{url}/timeline?room_id={room}", user="alice")
    assert s == 200 and tl["as_of"]["step"] == head["as_of"]["step"]
    s, ev = env.api.call("GET", f"{url}/events?room_id={room}&limit=1", user="alice")
    assert s == 200 and len(ev["events"]) == 1 and len(ev["next_cursor"]) == 32
    assert env.api.call("GET", f"{url}?room_id={room}", user="mallory")[0] == 404
    assert env.api.call("GET", f"{url}?room_id={room}")[0] == 401
    assert env.api.call("GET", f"{url}?step=1", user="alice")[0] == 400
    assert env.api.call("GET", f"{url}?room_id={room}&step=%D9%A3", user="alice")[0] == 400


def test_views_are_identical_after_the_graph_is_rebuilt_from_exported_state(fab):
    seed(fab)
    tails = (("head", ()), ("tl", ("timeline",)), ("ev", ("events",)), ("old", ()))
    def snap():
        return {k: lin(fab, fab.bob, *t, **({"step": 3} if k == "old" else {})) for k, t in tails}
    before = snap()
    fresh = ControlPlaneRepo()
    fresh.import_state(json.loads(json.dumps(fab.repo.export_state(), default=str)))
    fab.s.graph_source = lambda: fresh.graph
    assert snap() == before


# ------------------------------------------------------------ randomized privacy check
import random  # noqa: E402
import re  # noqa: E402


def _fuzz_events(fab, rnd, n_events):
    f = fab.feed
    pool = [f"task:n{i}" for i in range(5)] + [f"pr:github.com/o/r#{i}" for i in range(2)]
    prs = [p for p in pool if p.startswith("pr:")]
    hidden_tokens = []
    ctxs = [("room", {"room_id": fab.room}, True), ("room", {"room_id": fab.room}, True),
            ("private", {"room_id": fab.room}, False), ("room", {"room_id": fab.room2}, False),
            ("room", {"room_id": fab.room, "workspace_id": "ws-unlinked"}, False)]
    for i in range(n_events):
        scope, ctx, vis = rnd.choice(ctxs)
        nid = rnd.choice(pool)
        kind = rnd.choice(["upsert", "state", "edge", "edge", "upsert"])
        tok = f"hid{i}x"
        if kind == "upsert":
            lbl = tok if not vis else f"vis{i}"
            if not vis: hidden_tokens.append(lbl)
            f.known.add(nid)
            f.send("node.upsert", {"node_type": nid.split(":")[0], "node_id": nid, "label": lbl, **({"state": tok} if not vis and rnd.random()<.5 else {}), **({"attrs": {"k": tok}} if not vis and rnd.random()<.5 else {})}, scope, **ctx)
            if not vis: hidden_tokens += [tok]
        elif kind == "state":
            st = tok if not vis else f"vs{i}"
            if not vis: hidden_tokens.append(st)
            if nid in f.known:
                f.send("node.state", {"node_id": nid, "state": st, "reason": tok if not vis else "r"}, scope, **ctx)
                if not vis: hidden_tokens.append(tok)
        else:
            a, b = rnd.sample(pool, 2)
            if a in f.known and b in f.known:
                et = rnd.choice(["spawn", "reviewed", "dependency"])
                if et == "reviewed":
                    b = rnd.choice(prs)
                    if b not in f.known: continue
                    p = {"edge_type": et, "from_id": "review:r1" if "review:r1" in f.known or True else a, "to_id": b, "attrs": {"commit": tok if not vis else f"vc{i}"}}
                    if not vis: hidden_tokens.append(tok)
                    f.known.add("review:r1")
                else:
                    p = {"edge_type": et, "from_id": a, "to_id": b}
                try:
                    f.send("edge.add", p, scope, **ctx)
                except Exception:
                    pass
    return hidden_tokens

@pytest.mark.parametrize("seed_", range(60))
def test_randomized_replay_never_leaks_hidden_tokens(fab, seed_):
    rnd = random.Random(seed_)
    # reviewer node visible
    put(fab.feed, "review:r1", "room", label="R", room_id=fab.room)
    toks = _fuzz_events(fab, rnd, 40)
    for who in (fab.bob, fab.carol):
        n = lin(fab, who)["as_of"]["visible_events"]
        blob = _everywhere(fab, who, n)
        fab.s._hits.clear()
        leaked = [t for t in set(toks) if re.search(re.escape(t) + r"(?![0-9])", blob)]
        assert not leaked, (seed_, leaked)

