"""Lineage graph projection (docs/contracts/lineage-events.md, T02)."""
import json
import random
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.graph import MAX_DEPTH, GraphProjection, can_see, from_stored
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

WRITER = {"kind": "agent", "id": "key:writer", "authenticated_by": "api_key"}
OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
VIEWER = {"kind": "viewer", "id": "key:view", "authenticated_by": "api_key"}
OTHER = {"kind": "agent", "id": "key:other", "authenticated_by": "api_key"}
ANON = {"kind": "unknown", "id": None, "authenticated_by": "none"}


class Feed:
    """Emits lineage envelopes with a monotonically increasing source_sequence."""

    def __init__(self, svc, source="pi"):
        self.svc, self.source, self.n = svc, source, 0
        self.known = set()
        self.principal = WRITER

    def send(self, event_type, payload, scope="org", entity_id=None, seq=None, principal=None, **extra):
        principal = principal or self.principal
        seq = self.n if seq is None else seq
        self.n = max(self.n, seq) + 1
        body = {"schema_version": 2, "event_id": f"{self.source}-{seq}", "source": self.source,
                "source_sequence": seq, "entity_type": "lineage",
                "entity_id": entity_id or payload.get("node_id") or payload.get("from_id"),
                "event_type": event_type, "payload": payload, "visibility_scope": scope,
                "occurred_at": "2026-10-22T10:00:00Z", **extra}
        return self.svc.ingest(body, principal)

    def node(self, node_id, **kw):
        scope = kw.pop("scope", "org")
        principal = kw.pop("principal", None)
        self.known.add(node_id)
        return self.send("node.upsert", {"node_type": node_id.split(":")[0], "node_id": node_id, **kw}, scope,
                         principal=principal)

    def edge(self, etype, a, b, attrs=None, scope="org", nodes=True, **kw):
        for nid in (a, b):          # nodes the edge needs must be defined to be visible
            if nodes and nid not in self.known and "seq" not in kw:
                self.node(nid, scope=scope, principal=kw.get("principal"))
        p = {"edge_type": etype, "from_id": a, "to_id": b}
        if attrs is not None:
            p["attrs"] = attrs
        return self.send("edge.add", p, scope, **kw)


def make():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    return repo, svc, Feed(svc)


def ids(view):
    return {n["id"] for n in view["nodes"]}


def test_upsert_edge_and_idempotent_replay():
    repo, svc, f = make()
    f.node("agent:a1", label="Main")
    f.edge("spawn", "agent:a1", "agent:a2")
    before = repo.graph.snapshot()
    again = f.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "label": "Main"}, seq=0)
    assert again["envelope"]["duplicate"] is True
    assert repo.graph.snapshot() == before
    # applying the same stored event straight to the projection twice is a no-op too
    ev = {"event_id": "pi-0", "source": "pi", "source_sequence": 0, "event_type": "node.upsert",
          "payload": {"node_type": "agent", "node_id": "agent:a1", "label": "Main"},
          "visibility_scope": "org", "actor": WRITER}
    repo.graph.apply(ev)
    repo.graph.apply(ev)
    assert repo.graph.snapshot()["nodes"]["agent:a1"]["label"] == "Main"


def test_out_of_order_last_writer_wins_per_field():
    def run(order):
        g = GraphProjection()
        evs = [
            (1, "node.upsert", {"node_type": "task", "node_id": "task:t", "label": "old", "state": "running",
                                "attrs": {"a": 1, "b": 1}}),
            (3, "node.state", {"node_id": "task:t", "state": "done", "reason": "ok"}),
            (2, "node.upsert", {"node_type": "task", "node_id": "task:t", "label": "new", "attrs": {"a": 2}}),
        ]
        for seq, et, pl in (evs[i] for i in order):
            g.apply({"event_id": f"e{seq}", "source": "pi", "source_sequence": seq, "event_type": et,
                     "payload": pl, "visibility_scope": "org", "actor": WRITER})
        return g.snapshot()

    ref = run([0, 1, 2])
    for order in ([2, 1, 0], [1, 0, 2], [2, 0, 1]):
        assert run(order) == ref
    node = ref["nodes"]["task:t"]
    assert (node["label"], node["state"], node["attrs"]) == ("new", "done", {"a": 2, "b": 1})
    assert (node["first_event_id"], node["last_event_id"]) == ("e1", "e3")


def test_rebuild_from_events_equals_live_state():
    repo, svc, f = make()
    f.node("agent:a1", label="A", scope="room")
    f.node("task:t1", state="running")
    f.edge("delegation", "agent:a1", "task:t1")
    f.send("node.state", {"node_id": "task:t1", "state": "done"})
    f.edge("reviewed", "review:r1", "commit:c1", {"commit": "c1"})
    f.send("edge.remove", {"edge_type": "delegation", "from_id": "agent:a1", "to_id": "task:t1", "reason": "x"})
    live = repo.graph.snapshot()
    state = repo.export_state()
    fresh = ControlPlaneRepo()
    fresh.import_state(json.loads(json.dumps(state, default=str)))
    assert fresh.graph.snapshot() == live
    # envelope-only: lineage never reaches the global feed
    assert not [t for t in fresh._event_order if t[0] == "lineage"]
    assert not [t for t in repo._event_order if t[0] == "lineage"]


def test_rebuild_is_order_independent():
    repo, svc, f = make()
    for i in range(6):
        f.node(f"task:t{i}", label=f"l{i}")
        f.edge("dependency", f"task:t{i}", f"task:t{(i + 1) % 6}")
    f.send("edge.remove", {"edge_type": "dependency", "from_id": "task:t1", "to_id": "task:t2", "reason": "r"})
    events = [e for e in repo.stream("lineage", "task:t0")]
    events = [e for (k, _), s in repo._events.items() if k == "lineage" for e in s]
    ref = repo.graph.snapshot()
    for seed in range(5):
        random.Random(seed).shuffle(events)
        g = GraphProjection()
        for e in events:
            g.apply(from_stored(e))
        assert g.snapshot() == ref


def test_spawn_handoff_consultation_stay_distinct():
    repo, svc, f = make()
    for t in ("spawn", "handoff", "consultation"):
        f.edge(t, "agent:a", "agent:b")
    view = repo.graph.neighborhood(OPERATOR, "agent:a", depth=1)
    assert sorted(e["edge_type"] for e in view["edges"]) == ["consultation", "handoff", "spawn"]
    f.send("edge.remove", {"edge_type": "handoff", "from_id": "agent:a", "to_id": "agent:b", "reason": "r"})
    view = repo.graph.neighborhood(OPERATOR, "agent:a", depth=1)
    assert sorted(e["edge_type"] for e in view["edges"]) == ["consultation", "spawn"]


def test_edge_remove_tombstone_keeps_history_and_readd_resurrects():
    repo, svc, f = make()
    f.edge("dependency", "task:a", "task:b")
    f.send("edge.remove", {"edge_type": "dependency", "from_id": "task:a", "to_id": "task:b", "reason": "dropped"})
    assert repo.graph.neighborhood(OPERATOR, "task:a")["edges"] == []
    shown = repo.graph.neighborhood(OPERATOR, "task:a", include_removed=True)["edges"]
    assert shown[0]["removed"] and shown[0]["removal"]["reason"] == "dropped"
    prov = repo.graph.get_edge(OPERATOR, "dependency", "task:a", "task:b")["edges"][0]
    assert prov["provenance"]["event_id"] == "pi-2" and prov["removal"]["event_id"] == "pi-3"
    f.edge("dependency", "task:a", "task:b")
    assert len(repo.graph.neighborhood(OPERATOR, "task:a")["edges"]) == 1


def _raw(eid, seq, lseq, et, payload, actor=WRITER, source="pi", scope="org"):
    return {"event_id": eid, "source": source, "source_sequence": seq, "lineage_seq": lseq,
            "event_type": et, "payload": payload, "visibility_scope": scope, "actor": actor,
            "ingest_timestamp": "2026-10-22T10:00:00+00:00"}


def test_replay_remove_before_add_stays_removed_and_placeholders_stay_invisible():
    """Replay (not live admission) may see a remove before its add; the merge converges."""
    edge = {"edge_type": "dependency", "from_id": "task:a", "to_id": "task:b"}
    evs = [_raw("n1", 0, 1, "node.upsert", {"node_type": "task", "node_id": "task:a"}),
           _raw("n2", 1, 2, "node.upsert", {"node_type": "task", "node_id": "task:b"}),
           _raw("e1", 2, 3, "edge.add", edge),
           _raw("e2", 3, 4, "edge.remove", {**edge, "reason": "r"})]
    for order in ([3, 2, 1, 0], [2, 3, 0, 1], [0, 1, 2, 3]):
        g = GraphProjection()
        for i in order:
            g.apply(evs[i])
        assert g.neighborhood(OPERATOR, "task:a")["edges"] == []
    g = GraphProjection()
    g.apply(evs[2])                                    # edge before either node exists
    assert g.snapshot()["nodes"]["task:a"]["visibility_scope"] == "private"
    with pytest.raises(KeyError):
        g.get_node(VIEWER, "task:a")                   # an undefined endpoint is invisible


def test_reviewed_and_validated_require_binding():
    repo, svc, f = make()
    for et in ("reviewed", "validated"):
        with pytest.raises(ValueError):
            f.edge(et, "review:r", "commit:c", {})
        with pytest.raises(ValueError):
            f.edge(et, "review:r", "commit:c")
    f.edge("reviewed", "review:r", "plan:p", {"plan_digest": "sha256:aa"})
    f.edge("validated", "ci:1", "commit:c", {"commit": "abc"})
    assert repo.stream("lineage", "review:r")  # only the valid ones stored


def test_stale_reviewed_edge_on_new_commit():
    repo, svc, f = make()
    f.edge("reviewed", "review:r1", "pr:github.com/o/r#1", {"commit": "c1"})
    f.edge("reviewed", "review:r1", "pr:github.com/o/r#1", {"commit": "c2"})
    f.edge("reviewed", "review:r2", "pr:github.com/o/r#9", {"commit": "c1"})
    f.edge("validated", "ci:1", "pr:github.com/o/r#1", {"commit": "c1"})
    edges = {(e["edge_type"], e["attrs"]["commit"], e["to_id"]): e["stale"]
             for e in repo.graph.neighborhood(OPERATOR, "pr:github.com/o/r#1", depth=1)["edges"]}
    assert edges[("reviewed", "c1", "pr:github.com/o/r#1")] is True     # kept, flagged
    assert edges[("reviewed", "c2", "pr:github.com/o/r#1")] is False
    assert edges[("validated", "c1", "pr:github.com/o/r#1")] is False   # different edge type
    # same commit re-asserted is not stale; unrelated subject untouched
    f.edge("reviewed", "review:r3", "pr:github.com/o/r#1", {"commit": "c2"})
    e = repo.graph.get_edge(OPERATOR, "reviewed", "review:r3", "pr:github.com/o/r#1")["edges"][0]
    assert e["stale"] is False
    # removing the newer review makes the older one current again
    f.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:r1", "to_id": "pr:github.com/o/r#1",
                           "reason": "retracted", "attrs": {"commit": "c2"}})
    f.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:r3", "to_id": "pr:github.com/o/r#1",
                           "reason": "retracted"})
    old = repo.graph.get_edge(OPERATOR, "reviewed", "review:r1", "pr:github.com/o/r#1", commit="c1")["edges"][0]
    assert old["stale"] is False


def test_unknown_types_rejected_and_not_stored():
    repo, svc, f = make()
    bad = [("node.upsert", {"node_type": "robot", "node_id": "robot:1"}),
           ("node.upsert", {"node_type": "agent", "node_id": "task:1"}),
           ("node.upsert", {"node_type": "agent", "node_id": "agent"}),
           ("edge.add", {"edge_type": "friendship", "from_id": "agent:a", "to_id": "agent:b"}),
           ("edge.add", {"edge_type": "spawn", "from_id": "alien:a", "to_id": "agent:b"}),
           ("edge.add", {"edge_type": "spawn", "from_id": "agent:a", "to_id": "agent:a"}),
           ("edge.add", {"edge_type": "issue_link", "from_id": "task:a", "to_id": "agent:b"}),
           ("edge.remove", {"edge_type": "spawn", "from_id": "agent:a", "to_id": "agent:b"}),
           ("node.explode", {"node_id": "agent:a"}),
           ("node.state", {"node_id": "agent:a", "state": "Not Valid!"})]
    for et, pl in bad:
        with pytest.raises(ValueError):
            f.send(et, pl, entity_id="x")
    assert not [k for k in repo._events if k[0] == "lineage"]
    assert repo.graph.snapshot() == {"nodes": {}, "edges": {}, "wildcard_removes": {}, "owned": {}}
    # the sequence was not consumed by a rejected event
    assert f.send("node.upsert", {"node_type": "agent", "node_id": "agent:a"}, seq=0)["envelope"]["duplicate"] is False


def test_v1_lineage_rejected():
    repo, svc, f = make()
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "lineage", "entity_id": "agent:a", "event_type": "node.upsert",
                    "payload": {"node_type": "agent", "node_id": "agent:a"}}, WRITER)
    with pytest.raises(ValueError):
        svc.ingest({"schema_version": 1, "entity_type": "lineage", "entity_id": "agent:a",
                    "event_type": "node.upsert", "payload": {"node_type": "agent", "node_id": "agent:a"}}, WRITER)
    assert repo.graph.snapshot()["nodes"] == {}


def test_session_rollover_keeps_agent_id_stable():
    repo, svc, f = make()
    f.node("agent:a1", label="Logical")
    f.node("attempt:t1", attrs={"agent_id": "agent:a1"})
    f.node("session:s1", attrs={"agent_id": "agent:a1", "attempt_id": "attempt:t1"})
    f.edge("spawn", "agent:a1", "attempt:t1")
    f.send("node.state", {"node_id": "session:s1", "state": "done", "reason": "rollover"})
    f.node("session:s2", attrs={"agent_id": "agent:a1", "attempt_id": "attempt:t1"})
    snap = repo.graph.snapshot()["nodes"]
    assert snap["agent:a1"]["label"] == "Logical" and snap["agent:a1"]["state"] is None
    assert {n for n in snap if n.startswith("session:")} == {"session:s1", "session:s2"}
    assert all(snap[s]["attrs"]["agent_id"] == "agent:a1" for s in ("session:s1", "session:s2"))
    assert snap["session:s1"]["state"] == "done"


def test_closing_a_pane_or_ending_a_session_changes_nothing_on_the_agent():
    repo, svc, f = make()
    f.node("agent:a1", state="running")
    f.node("pane:p1", attrs={"agent_id": "agent:a1"})
    before = repo.graph.snapshot()["nodes"]["agent:a1"]
    # legacy lifecycle traffic for the pane's session never touches the graph
    svc.ingest({"entity_type": "session", "entity_id": "p1", "event_type": "session.stop",
                "payload": {"status": "stopped"}}, WRITER)
    assert repo.graph.snapshot()["nodes"]["agent:a1"] == before
    # an explicit pane state is only that pane's state
    f.send("node.state", {"node_id": "pane:p1", "state": "closed"})
    snap = repo.graph.snapshot()["nodes"]
    assert snap["agent:a1"] == before and snap["pane:p1"]["state"] == "closed"


# ------------------------------------------------------------ visibility
def seed_scopes(f):
    f.node("task:org", scope="org")
    f.node("task:room", scope="room")
    f.node("task:priv", scope="private")
    f.node("task:hub", scope="org")
    f.node("task:far", scope="org")
    for n in ("org", "room", "priv"):
        f.edge("dependency", "task:hub", f"task:{n}")
    f.edge("dependency", "task:priv", "task:far")      # far only reachable through the hidden node


def test_traversal_never_shows_or_leaks_hidden_nodes():
    repo, svc, f = make()
    seed_scopes(f)
    view = repo.graph.neighborhood(VIEWER, "task:hub", depth=3)
    assert ids(view) == {"task:hub", "task:org"}
    assert "task:far" not in ids(view)          # hop through a hidden node is cut
    blob = json.dumps(view)
    assert "task:priv" not in blob and "task:room" not in blob
    assert all(e["to_id"] in ids(view) and e["from_id"] in ids(view) for e in view["edges"])
    assert not view["truncated"]
    # operator sees room but still not a private node it did not author
    op = repo.graph.neighborhood(OPERATOR, "task:hub", depth=3)
    assert "task:room" in ids(op) and "task:priv" not in ids(op)
    # the author sees its own private node, and so the path through it
    own = repo.graph.neighborhood(WRITER, "task:hub", depth=3)
    assert {"task:priv", "task:far"} <= ids(own)
    # same shape for viewers regardless of how much hidden data exists
    for _ in range(30):
        f.node(f"task:p{_}", scope="private")
        f.edge("dependency", "task:hub", f"task:p{_}")
    again = repo.graph.neighborhood(VIEWER, "task:hub", depth=3)
    assert again == view


def test_hidden_root_node_detail_and_edge_are_404_like_missing():
    repo, svc, f = make()
    seed_scopes(f)
    for call in (lambda: repo.graph.get_node(VIEWER, "task:priv"),
                 lambda: repo.graph.get_node(VIEWER, "task:nope"),
                 lambda: repo.graph.neighborhood(VIEWER, "task:priv"),
                 lambda: repo.graph.get_node(ANON, "task:org"),
                 lambda: repo.graph.get_edge(VIEWER, "dependency", "task:hub", "task:priv")):
        with pytest.raises(KeyError):
            call()
    detail = repo.graph.get_node(VIEWER, "task:hub")
    assert {e["to_id"] for e in detail["edges"]} == {"task:org"}


def test_scope_only_narrows_and_edge_needs_visible_endpoints():
    repo, svc, f = make()
    f.node("task:a", scope="org")
    f.node("task:a", scope="private")           # a later, narrower claim wins (never widens)
    f.node("task:a", scope="org")
    f.node("task:b", scope="org")
    f.edge("dependency", "task:b", "task:a", scope="org")
    assert ids(repo.graph.neighborhood(VIEWER, "task:b")) == {"task:b"}
    f.edge("dependency", "task:b", "task:c", scope="private")   # private edge between org nodes
    assert ids(repo.graph.neighborhood(VIEWER, "task:b")) == {"task:b"}


def test_can_see_fails_closed():
    assert not can_see(None, "org", frozenset())
    assert not can_see(ANON, "org", frozenset())
    assert not can_see(VIEWER, "galaxy", frozenset())
    assert not can_see(VIEWER, "room", frozenset())
    assert not can_see(VIEWER, "private", None)
    assert not can_see(VIEWER, "private", frozenset({"key:other"}))
    assert not can_see(VIEWER, "private", frozenset())
    assert can_see(VIEWER, "private", frozenset({"key:view", "key:other"}))   # a writer
    assert can_see(VIEWER, "private", frozenset({"key:view"}))


def test_depth_and_size_caps():
    repo, svc, f = make()
    for i in range(10):
        f.edge("dependency", f"task:n{i}", f"task:n{i + 1}")
    assert ids(repo.graph.neighborhood(VIEWER, "task:n0", depth=2)) == {"task:n0", "task:n1", "task:n2"}
    assert ids(repo.graph.neighborhood(VIEWER, "task:n0", depth=0)) == {"task:n0"}
    with pytest.raises(ValueError):
        repo.graph.neighborhood(VIEWER, "task:n0", depth=MAX_DEPTH + 1)
    with pytest.raises(ValueError):
        repo.graph.neighborhood(VIEWER, "task:n0", max_nodes=0)
    capped = repo.graph.neighborhood(VIEWER, "task:n0", depth=5, max_nodes=3)
    assert len(capped["nodes"]) == 3 and capped["truncated"] is True
    capped = repo.graph.neighborhood(VIEWER, "task:n0", depth=5, max_edges=1)
    assert len(capped["edges"]) == 1 and capped["truncated"] is True
    star_f = Feed(svc, source="star")
    for i in range(30):
        star_f.edge("spawn", "agent:root", f"agent:c{i}")
    star = repo.graph.neighborhood(VIEWER, "agent:root", depth=1, max_nodes=10)
    assert len(star["nodes"]) == 10 and star["truncated"]


def test_attrs_size_bounded():
    repo, svc, f = make()
    with pytest.raises(ValueError):
        f.node("task:big", attrs={"blob": "x" * 20000})


# ------------------------------------------------------------------- HTTP
@pytest.fixture
def http():
    keys = {"op-" + "k": "operator", "vw-" + "k": "viewer", "in-" + "k": "ingest"}
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys=keys)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    base = f"http://127.0.0.1:{server.port}"

    def call(path, key=None, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-API-Key": key or "", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    yield server, call, keys
    server.shutdown()


def test_http_graph_reads_are_authorized_and_scoped(http):
    server, call, keys = http
    op, vw, ing = keys and list(keys)
    n = [0]

    def post(et, payload, scope, entity):
        n[0] += 1
        return call("/api/v1/ingest", ing, {
            "schema_version": 2, "event_id": f"h{n[0]}", "source": "pi", "source_sequence": n[0],
            "entity_type": "lineage", "entity_id": entity, "event_type": et, "payload": payload,
            "visibility_scope": scope})

    assert post("node.upsert", {"node_type": "task", "node_id": "task:a", "label": "A"}, "org", "task:a")[0] == 200
    assert post("node.upsert", {"node_type": "task", "node_id": "task:r", "label": "R"}, "room", "task:r")[0] == 200
    assert post("edge.add", {"edge_type": "dependency", "from_id": "task:a", "to_id": "task:r"}, "org", "task:a")[0] == 200
    assert post("edge.add", {"edge_type": "spawn", "from_id": "task:a", "to_id": "task:a"}, "org", "task:a")[0] == 400
    assert post("node.upsert", {"node_type": "robot", "node_id": "robot:1"}, "org", "robot:1")[0] == 400

    assert call("/api/v1/graph/nodes/task%3Aa")[0] == 401
    s, v = call("/api/v1/graph/nodes/task%3Aa/neighborhood?depth=2", vw)
    assert s == 200 and {x["id"] for x in v["nodes"]} == {"task:a"} and v["edges"] == []
    assert call("/api/v1/graph/nodes/task%3Ar", vw)[0] == 404
    assert call("/api/v1/graph/nodes/task%3Anope", vw)[0] == 404
    s, v = call("/api/v1/graph/nodes/task%3Aa/neighborhood", op)
    assert {x["id"] for x in v["nodes"]} == {"task:a", "task:r"}
    s, d = call("/api/v1/graph/nodes/task%3Aa", vw)
    assert s == 200 and d["node"]["label"] == "A" and d["edges"] == []
    s, e = call("/api/v1/graph/edge?edge_type=dependency&from_id=task%3Aa&to_id=task%3Ar", op)
    assert s == 200 and e["edges"][0]["provenance"]["source"] == "pi"
    assert call("/api/v1/graph/edge?edge_type=dependency&from_id=task%3Aa&to_id=task%3Ar", vw)[0] == 404
    assert call("/api/v1/graph/nodes/task%3Aa/neighborhood?depth=99", vw)[0] == 400
    assert call("/api/v1/graph/nodes/task%3Aa/neighborhood?depth=x", vw)[0] == 400
    # raw lineage rows are not readable through the generic event endpoint
    assert call("/api/v1/events?entity_type=lineage&entity_id=task%3Aa", op)[0] == 404
    # nor does lineage appear in the feed
    s, feed = call("/api/v1/events", op)
    assert "lineage" not in json.dumps(feed)


def test_rebuild_from_durable_rows_equals_live_state():
    """Restart path: fabric_events rows -> bridge._rebuild_events -> import_state."""
    import asyncio

    from tests.control_plane.test_envelope_v2 import V2Conn

    from herdr_engineering.control_plane import bridge
    from herdr_engineering.control_plane.sink import DurableEventSink

    conn = V2Conn()
    repo = ControlPlaneRepo(sink=DurableEventSink(conn=conn, run=asyncio.run))
    f = Feed(IngestService(repo))
    f.node("agent:a1", label="A", scope="room")
    f.node("task:t1", scope="private")
    f.edge("reviewed", "review:r1", "task:t1", {"commit": "c1"}, scope="org")
    f.edge("reviewed", "review:r1", "task:t1", {"commit": "c2"}, scope="org")
    f.edge("handoff", "agent:a1", "task:t1", scope="org")
    f.send("edge.remove", {"edge_type": "handoff", "from_id": "agent:a1", "to_id": "task:t1", "reason": "r"})
    live = repo.graph.snapshot()
    rows = [{**r, "sequence": i} for i, r in enumerate(conn.rows)]
    streams, order = bridge._rebuild_events(rows)
    fresh = ControlPlaneRepo()
    fresh.import_state({"events": streams, "event_order": order})
    assert fresh.graph.snapshot() == live
    assert fresh.graph.events_rejected == 0
    assert not [t for t in fresh._event_order if t[0] == "lineage"]


# ------------------------------------------------- review round 1 regressions
class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        self.t += 1.0
        return self.t


def make_clocked():
    clock = Clock()
    repo = ControlPlaneRepo(now=clock)
    return repo, IngestService(repo), clock


def rows(repo):
    return [e for (k, _), s in repo._events.items() if k == "lineage" for e in s]


def test_non_owner_cannot_modify_nodes_edges_or_narrow_scope():
    repo, svc, f = make()
    f.node("agent:a1", label="mine")
    f.edge("spawn", "agent:a1", "agent:a2")
    before = repo.graph.snapshot()
    stored = len(rows(repo))
    hostile = Feed(svc, source="evil")
    hostile.n = 2**62
    attempts = [
        lambda: hostile.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "label": "pwn"},
                             principal=OTHER),
        lambda: hostile.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "state": "failed"},
                             scope="private", principal=OTHER),
        lambda: hostile.send("node.state", {"node_id": "agent:a1", "state": "failed"}, principal=OTHER),
        lambda: hostile.send("edge.add", {"edge_type": "spawn", "from_id": "agent:a1", "to_id": "agent:a2",
                                          "attrs": {"x": 1}}, principal=OTHER),
        lambda: hostile.send("edge.remove", {"edge_type": "spawn", "from_id": "agent:a1",
                                             "to_id": "agent:a2", "reason": "x"}, principal=OTHER),
        lambda: hostile.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1",
                                             "attrs": {"co_writers": ["key:other"]}}, principal=OTHER),
    ]
    for go in attempts:
        with pytest.raises(PermissionError):
            go()
    assert repo.graph.snapshot() == before
    assert len(rows(repo)) == stored                       # nothing persisted
    assert repo.ledger.source_state("evil")["last_sequence"] is None   # sequence not consumed
    # the owner still sees its node, and the owner can still update it
    assert repo.graph.get_node(WRITER, "agent:a1")["node"]["label"] == "mine"
    f.node("agent:a1", label="mine2")


def test_operator_overrides_and_co_writers_are_operator_only():
    repo, svc, f = make()
    f.node("agent:a1", label="mine", scope="private")
    ops = Feed(svc, source="ops")
    ops.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "label": "by-op"}, principal=OPERATOR)
    other = Feed(svc, source="other")
    with pytest.raises(PermissionError):
        other.send("node.state", {"node_id": "agent:a1", "state": "running"}, principal=OTHER)
    ops.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1",
                             "attrs": {"co_writers": ["key:other"]}}, principal=OPERATOR)
    other.send("node.state", {"node_id": "agent:a1", "state": "running"}, principal=OTHER)   # now allowed
    assert repo.graph.snapshot()["nodes"]["agent:a1"]["owner"] == "key:writer"
    # a private node stays visible to its writers, not to others
    assert repo.graph.get_node(OTHER, "agent:a1")["node"]["state"] == "running"
    with pytest.raises(KeyError):
        repo.graph.get_node(VIEWER, "agent:a1")


def test_wildcard_remove_needs_ownership_of_every_binding():
    repo, svc, f = make()
    f.edge("reviewed", "review:r", "pr:1", {"commit": "c1"})
    other = Feed(svc, source="other")
    with pytest.raises(PermissionError):
        other.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:r", "to_id": "pr:1",
                                   "reason": "x"}, principal=OTHER)


def test_cross_source_huge_sequence_cannot_win_forever():
    repo, svc, clock = make_clocked()
    a = Feed(svc, source="pi")
    a.node("agent:a1", label="v1")
    ops = Feed(svc, source="ops")
    ops.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "label": "huge"},
             principal=OPERATOR, seq=2**62)
    assert repo.graph.snapshot()["nodes"]["agent:a1"]["label"] == "huge"
    a.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "label": "v2"}, seq=5)
    assert repo.graph.snapshot()["nodes"]["agent:a1"]["label"] == "v2"       # later receipt wins
    # within ONE source the emitter's sequence still decides, even if it arrives late
    a.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1", "label": "old"}, seq=3)
    assert repo.graph.snapshot()["nodes"]["agent:a1"]["label"] == "v2"


def test_stale_flag_does_not_leak_a_hidden_edge():
    repo, svc, f = make()
    f.edge("reviewed", "review:r1", "pr:1", {"commit": "c1"})
    f.edge("reviewed", "review:r1", "pr:1", {"commit": "c2"}, scope="private")
    pub = repo.graph.get_edge(VIEWER, "reviewed", "review:r1", "pr:1")["edges"]
    assert [(e["binding"], e["stale"], e["bindings"]) for e in pub] == [({"commit": "c1"}, False, ["c1"])]
    assert "c2" not in json.dumps(repo.graph.neighborhood(VIEWER, "pr:1"))
    mine = {e["binding"]["commit"]: e for e in repo.graph.get_edge(WRITER, "reviewed", "review:r1", "pr:1")["edges"]}
    assert mine["c1"]["stale"] is True and mine["c2"]["stale"] is False
    assert mine["c1"]["bindings"] == ["c1", "c2"]


def test_wildcard_remove_is_commutative_and_does_not_leak_reason():
    repo, svc, clock = make_clocked()
    f = Feed(svc)
    f.node("review:a")
    f.node("pr:1")
    E = lambda c: {"edge_type": "reviewed", "from_id": "review:a", "to_id": "pr:1", "attrs": {"commit": c}}
    f.send("edge.add", E("c1"), entity_id="A")
    ops = Feed(svc, source="ops")
    ops.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:a", "to_id": "pr:1",
                             "reason": "internal-note"}, scope="private", entity_id="B", principal=OPERATOR)
    f.send("edge.add", E("c2"), entity_id="A")
    live = repo.graph.snapshot()
    fresh = ControlPlaneRepo()
    fresh.import_state(repo.export_state())
    assert fresh.graph.snapshot() == live                    # the reviewer's reproduction
    # old binding was added before the wildcard => removed; the newer one survives
    view = repo.graph.neighborhood(VIEWER, "pr:1", include_removed=True)
    by = {e["binding"]["commit"]: e for e in view["edges"]}
    assert by["c1"]["removed"] is True and by["c2"]["removed"] is False
    assert "removal" not in by["c1"] and "internal-note" not in json.dumps(view)
    assert by["c1"]["visibility_scope"] == by["c2"]["visibility_scope"] == "org"
    # the operator who wrote the private remove can read its details
    opview = repo.graph.get_edge(OPERATOR, "reviewed", "review:a", "pr:1", commit="c1")["edges"][0]
    assert opview["removal"]["reason"] == "internal-note"


def _scenario(f_ops, f_a, f_b):
    f_a.node("agent:a1", label="A", scope="room")
    f_a.node("task:t1", state="running")
    f_b.node("task:t2", state="running")
    f_b.node("review:r1")
    f_a.edge("delegation", "agent:a1", "task:t1")
    f_b.edge("reviewed", "review:r1", "pr:1", {"commit": "c1"})
    f_b.edge("reviewed", "review:r1", "pr:1", {"commit": "c2", "supersedes": ["c1"]})
    f_b.edge("validated", "review:r1", "pr:1", {"plan_digest": "d1"})
    f_ops.send("node.upsert", {"node_type": "task", "node_id": "task:t1", "attrs": {"co_writers": ["key:other"]}},
               principal=OPERATOR)
    f_ops.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:r1", "to_id": "pr:1",
                               "reason": "w"}, scope="private", principal=OPERATOR, entity_id="review:r1")
    f_a.send("edge.remove", {"edge_type": "delegation", "from_id": "agent:a1", "to_id": "task:t1",
                             "reason": "r"})
    f_b.send("node.state", {"node_id": "task:t2", "state": "done"}, principal=OTHER)
    f_b.send("node.state", {"node_id": "task:t1", "state": "blocked"}, principal=OTHER)
    f_a.send("edge.remove", {"edge_type": "validated", "from_id": "review:r1", "to_id": "pr:1",
                             "reason": "gone", "attrs": {"plan_digest": "d1"}}, principal=OTHER)


def test_all_merges_commute_under_shuffle_live_and_after_export_import():
    repo, svc, clock = make_clocked()
    f_a = Feed(svc, source="pi")
    f_b = Feed(svc, source="aux")
    f_ops = Feed(svc, source="ops")
    f_b_principal = OTHER

    f_b = Feed(svc, source="aux")
    f_b.principal = f_b_principal
    _scenario(f_ops, f_a, f_b)
    live = repo.graph.snapshot()
    assert live["edges"] and live["wildcard_removes"]
    events = rows(repo)
    assert len(events) > 10
    for seed in range(8):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)                    # cross-entity interleavings too
        g = GraphProjection()
        for e in shuffled:
            g.apply(from_stored(e))
        assert g.snapshot() == live, f"seed {seed}"
        state = repo.export_state()
        keys = list(state["events"])
        random.Random(seed).shuffle(keys)
        state["events"] = {k: random.Random(seed).sample(state["events"][k], len(state["events"][k]))
                           for k in keys}
        fresh = ControlPlaneRepo()
        fresh.import_state(json.loads(json.dumps(state, default=str)))
        assert fresh.graph.snapshot() == live, f"import seed {seed}"


def test_attrs_caps_are_cumulative_not_per_event():
    repo, svc, f = make()
    f.node("task:t", attrs={f"a{i}": 1 for i in range(60)})
    f.node("task:t", attrs={f"b{i}": 1 for i in range(60)})
    with pytest.raises(ValueError, match="too many attrs"):
        f.node("task:t", attrs={f"c{i}": 1 for i in range(60)})
    g = Feed(svc, source="big")
    with pytest.raises(ValueError, match="total size"):
        for i in range(10):
            g.node("task:big", attrs={"k%d" % i: "x" * 5000})
    with pytest.raises(ValueError, match="too many attrs|total size"):
        f.edge("dependency", "task:x", "task:y", {f"e{i}": "x" * 100 for i in range(64)})
        f.edge("dependency", "task:x", "task:y", {f"f{i}": "x" * 100 for i in range(64)})
        f.edge("dependency", "task:x", "task:y", {f"g{i}": "x" * 100 for i in range(64)})


def test_total_node_edge_and_source_caps(monkeypatch):
    from herdr_engineering.control_plane import graph as graph_mod
    repo, svc, f = make()
    monkeypatch.setattr(graph_mod, "MAX_TOTAL_NODES", 5)       # non-operators may fill 80% (4)
    f.node("task:1")
    f.node("task:2")
    f.node("task:3")
    f.node("task:4")
    with pytest.raises(ValueError, match="capacity"):
        f.node("task:5")
    f.node("task:3", label="existing nodes stay writable")
    f.node("task:5", principal=OPERATOR)                        # the reserve is for operators
    monkeypatch.setattr(graph_mod, "MAX_TOTAL_EDGES", 1)
    f.edge("dependency", "task:1", "task:2")
    with pytest.raises(ValueError, match="capacity"):
        f.edge("dependency", "task:2", "task:3")
    monkeypatch.setattr(graph_mod, "MAX_SOURCES_PER_OBJECT", 2)
    Feed(svc, source="s1").node("task:1", label="1")
    Feed(svc, source="s2").node("task:1", label="2")
    with pytest.raises(ValueError, match="too many distinct writers"):
        Feed(svc, source="s3").node("task:1", label="3")


def test_response_size_is_capped(monkeypatch):
    from herdr_engineering.control_plane import graph as graph_mod
    repo, svc, f = make()
    for i in range(20):
        f.edge("spawn", "agent:root", f"agent:c{i}")
        f.node(f"agent:c{i}", attrs={"blob": "x" * 5000})
    monkeypatch.setattr(graph_mod, "MAX_RESPONSE_BYTES", 30_000)
    view = repo.graph.neighborhood(VIEWER, "agent:root", depth=1)
    assert view["truncated"] and 1 < len(view["nodes"]) < 21
    assert len(json.dumps(view)) < 40_000
    monkeypatch.setattr(graph_mod, "MAX_RESPONSE_BYTES", 2_000)
    assert repo.graph.get_node(VIEWER, "agent:root")["edges_truncated"] is True


def test_stale_is_by_binding_identity_with_explicit_supersession():
    repo, svc, clock = make_clocked()
    f = Feed(svc)
    f.node("review:r")
    f.node("pr:1")
    # same commit with a plan digest added later is the SAME binding, not a different one
    f.edge("reviewed", "review:r", "pr:1", {"commit": "c1"})
    f.edge("reviewed", "review:r", "pr:1", {"commit": "c1", "plan_digest": "d"})
    edges = repo.graph.get_edge(VIEWER, "reviewed", "review:r", "pr:1")["edges"]
    assert len(edges) == 1 and edges[0]["stale"] is False and edges[0]["attrs"]["plan_digest"] == "d"
    # newer commit declares it supersedes c1; an older-commit review that arrives late
    # must not mark the newer one stale
    f.edge("reviewed", "review:r2", "pr:1", {"commit": "c2", "supersedes": ["c1"]})
    f.edge("reviewed", "review:r3", "pr:1", {"commit": "c1"})        # late, older commit
    by = {(e["from_id"], e["binding"]["commit"]): e["stale"]
          for e in repo.graph.neighborhood(VIEWER, "pr:1", depth=1)["edges"]}
    assert by[("review:r2", "c2")] is False
    assert by[("review:r3", "c1")] is True and by[("review:r", "c1")] is True
    # without any hint, arrival order decides and both bindings are exposed for the UI
    f.edge("reviewed", "review:q", "pr:9", {"commit": "x1"})
    f.edge("reviewed", "review:q2", "pr:9", {"commit": "x2"})
    q = repo.graph.get_edge(VIEWER, "reviewed", "review:q", "pr:9")["edges"][0]
    assert q["stale"] is True and q["bindings"] == ["x1", "x2"]


def test_http_unauthorized_overwrite_is_403(http):
    server, call, keys = http
    op, vw, ing = list(keys)
    ing2 = "in2-" + "k"
    server._keys[ing2] = "ingest"
    n = [0]

    def post(key, et, payload, seq=None, source="pi"):
        n[0] += 1
        return call("/api/v1/ingest", key, {
            "schema_version": 2, "event_id": f"w{n[0]}", "source": source,
            "source_sequence": n[0] if seq is None else seq, "entity_type": "lineage",
            "entity_id": payload["node_id"], "event_type": et, "payload": payload,
            "visibility_scope": "org"})

    node = {"node_type": "task", "node_id": "task:a", "label": "A"}
    assert post(ing, "node.upsert", node)[0] == 200
    assert post(ing2, "node.upsert", {**node, "label": "pwn"}, seq=2**62, source="evil")[0] == 403
    assert post(op, "node.upsert", {**node, "label": "op"}, source="ops")[0] == 200
    assert call("/api/v1/graph/nodes/task%3Aa", vw)[1]["node"]["label"] == "op"


# ------------------------------------------------- review round 2 regressions
V = {"kind": "agent", "id": "key:victim", "authenticated_by": "api_key"}
A = {"kind": "agent", "id": "key:attacker", "authenticated_by": "api_key"}
B = {"kind": "agent", "id": "key:b", "authenticated_by": "api_key"}


def rebuilt(repo):
    fresh = ControlPlaneRepo()
    fresh.import_state(json.loads(json.dumps(repo.export_state(), default=str)))
    return fresh


def test_preemptive_wildcard_remove_is_refused_and_cannot_hide_a_later_edge():
    repo, svc, _ = make()
    atk, vic = Feed(svc, "pi"), Feed(svc, "pi")
    rm = {"edge_type": "reviewed", "from_id": "agent:r", "to_id": "commit:c", "reason": "x"}
    stored = len(rows(repo))
    for payload in (rm, {**rm, "attrs": {"commit": "c1"}}):
        with pytest.raises(PermissionError):
            atk.send("edge.remove", payload, principal=A, seq=2**62)
    assert len(rows(repo)) == stored
    vic.send("node.upsert", {"node_type": "agent", "node_id": "agent:r"}, principal=V, seq=1)
    vic.send("node.upsert", {"node_type": "commit", "node_id": "commit:c"}, principal=V, seq=2)
    vic.send("edge.add", {"edge_type": "reviewed", "from_id": "agent:r", "to_id": "commit:c",
                          "attrs": {"commit": "c1"}}, principal=V, seq=3)
    seen = repo.graph.get_node(VIEWER, "commit:c")["edges"]
    assert [e["removed"] for e in seen] == [False]
    # the attacker still cannot remove it once it exists, whatever sequence it claims
    with pytest.raises(PermissionError):
        atk.send("edge.remove", rm, principal=A, seq=2**62 + 1)
    # and the victim's own wildcard remove works
    vic.send("edge.remove", rm, principal=V, seq=4)
    assert repo.graph.neighborhood(VIEWER, "commit:c")["edges"] == []


def test_same_source_huge_sequence_never_beats_another_principal():
    repo, svc, _ = make()
    f = Feed(svc, "pi")
    f.send("node.upsert", {"node_type": "agent", "node_id": "agent:x", "label": "first"}, principal=V, seq=1)
    ops = Feed(svc, "op")
    ops.send("node.upsert", {"node_type": "agent", "node_id": "agent:x",
                             "attrs": {"co_writers": [B["id"]]}}, principal=OPERATOR)
    f.send("node.upsert", {"node_type": "agent", "node_id": "agent:x", "label": "b-huge"}, principal=B, seq=2**62)
    f.send("node.upsert", {"node_type": "agent", "node_id": "agent:x", "label": "victim-later"}, principal=V, seq=2)
    assert repo.graph.snapshot()["nodes"]["agent:x"]["label"] == "victim-later"
    assert rebuilt(repo).graph.snapshot() == repo.graph.snapshot()


def test_supersedes_from_an_unrelated_reviewer_cannot_mark_a_review_stale():
    repo, svc, _ = make()
    fv, fa = Feed(svc, "v"), Feed(svc, "a")
    fv.edge("reviewed", "agent:good", "commit:subj", {"commit": "c1"}, principal=V)
    fa.node("agent:evil", principal=A)
    fa.send("edge.add", {"edge_type": "reviewed", "from_id": "agent:evil", "to_id": "commit:subj",
                         "attrs": {"commit": "c2", "supersedes": ["c1"]}}, principal=A)
    view = {e["from_id"]: e for e in repo.graph.get_node(VIEWER, "commit:subj")["edges"]}
    assert view["agent:good"]["stale"] is False and view["agent:evil"]["stale"] is False
    assert view["agent:good"]["bindings"] == ["c1", "c2"]          # shown side by side, not ranked
    # nor can the attacker author an edge FROM the victim's node to impersonate that reviewer
    with pytest.raises(PermissionError):
        fa.send("edge.add", {"edge_type": "reviewed", "from_id": "agent:good", "to_id": "commit:subj",
                             "attrs": {"commit": "c3", "supersedes": ["c1"]}}, principal=A)
    # the same reviewer superseding its own earlier review still works
    fv.send("edge.add", {"edge_type": "reviewed", "from_id": "agent:good", "to_id": "commit:subj",
                         "attrs": {"commit": "c4", "supersedes": ["c1"]}}, principal=V)
    mine = {e["binding"]["commit"]: e["stale"] for e in repo.graph.get_node(VIEWER, "commit:subj")["edges"]
            if e["from_id"] == "agent:good"}
    assert mine == {"c1": True, "c4": False}


def test_refusals_are_indistinguishable_and_never_create_ownership():
    repo, svc, _ = make()
    fv, fa = Feed(svc, "v"), Feed(svc, "a")
    fv.node("agent:p", scope="private")
    fv.node("agent:q", scope="org")
    fv.node("agent:pub", scope="org")
    msgs = set()
    attempts = [
        ("edge.remove", {"edge_type": "spawn", "from_id": "agent:p", "to_id": "agent:q", "reason": "x"}),  # no such edge
        ("edge.remove", {"edge_type": "spawn", "from_id": "agent:nope", "to_id": "agent:q", "reason": "x"}),
        ("edge.add", {"edge_type": "spawn", "from_id": "agent:p", "to_id": "agent:q"}),      # unseen endpoint
        ("edge.add", {"edge_type": "spawn", "from_id": "agent:nope", "to_id": "agent:q"}),   # unknown endpoint
        ("edge.add", {"edge_type": "spawn", "from_id": "agent:pub", "to_id": "agent:q"}),    # not writer of from
        ("node.upsert", {"node_type": "agent", "node_id": "agent:pub", "label": "x"}),       # not writer of node
    ]
    for et, pl in attempts:
        with pytest.raises(PermissionError) as exc:
            fa.send(et, pl, principal=A)
        msgs.add(str(exc.value))
    assert len(msgs) == 1                                   # one message for every refusal
    # nothing was claimed: the real owner's edge is accepted afterwards
    fv.edge("spawn", "agent:p", "agent:q")
    assert len(repo.graph.get_node(WRITER, "agent:q")["edges"]) == 1


def test_ownership_is_pinned_to_admission_order_not_clock_or_source_name():
    repo = ControlPlaneRepo()
    t = [1_800_000_000.0]
    repo._now = lambda: t[0]
    svc = IngestService(repo)
    fz, fo, fa = Feed(svc, "zeta"), Feed(svc, "op"), Feed(svc, "alpha")
    fz.send("node.upsert", {"node_type": "agent", "node_id": "agent:x"}, principal=V)
    with pytest.raises(PermissionError):                    # same instant, source sorts first
        fa.send("node.upsert", {"node_type": "agent", "node_id": "agent:x", "label": "a"}, principal=A)
    fo.send("node.upsert", {"node_type": "agent", "node_id": "agent:x",
                            "attrs": {"co_writers": [B["id"]]}}, principal=OPERATOR)
    t[0] -= 5                                               # the clock steps back
    fa.send("node.upsert", {"node_type": "agent", "node_id": "agent:x", "label": "b"}, principal=B)
    live = repo.graph.snapshot()["nodes"]["agent:x"]
    assert live["owner"] == V["id"]
    assert rebuilt(repo).graph.snapshot()["nodes"]["agent:x"]["owner"] == V["id"]
    fz.send("node.upsert", {"node_type": "agent", "node_id": "agent:x", "label": "v"}, principal=V)  # still writes


def test_per_principal_quota_and_wildcard_cap(monkeypatch):
    from herdr_engineering.control_plane import graph as graph_mod
    repo, svc, f = make()
    fv, fa, fo = Feed(svc, "v"), Feed(svc, "a"), Feed(svc, "o")
    monkeypatch.setattr(graph_mod, "MAX_OBJECTS_PER_PRINCIPAL", 3)
    for i in range(3):
        fv.send("node.upsert", {"node_type": "task", "node_id": f"task:v{i}"}, principal=V)
    with pytest.raises(PermissionError):                        # at quota: same refusal as any forbidden write
        fv.send("node.upsert", {"node_type": "task", "node_id": "task:v3"}, principal=V)
    fv.send("node.upsert", {"node_type": "task", "node_id": "task:v0", "label": "update ok"}, principal=V)
    fa.send("node.upsert", {"node_type": "task", "node_id": "task:a0"}, principal=A)   # others unaffected
    for i in range(5):                                                                           # operators exempt
        fo.send("node.upsert", {"node_type": "task", "node_id": f"task:o{i}"}, principal=OPERATOR)
    assert rebuilt(repo).graph._owned == repo.graph._owned     # quota survives restart
    monkeypatch.setattr(graph_mod, "MAX_OBJECTS_PER_PRINCIPAL", 10_000)
    monkeypatch.setattr(graph_mod, "MAX_WILDCARD_REMOVES", 1)
    w = Feed(svc, "w")
    for i in range(2):
        w.edge("reviewed", f"review:{i}", f"pr:{i}", {"commit": "c"})
    w.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:0", "to_id": "pr:0", "reason": "r"})
    with pytest.raises(ValueError, match="wildcard"):
        w.send("edge.remove", {"edge_type": "reviewed", "from_id": "review:1", "to_id": "pr:1", "reason": "r"})


# ------------------------------------------------- review round 3 regressions
def pi_edge(feed, etype, a, b, attrs=None, scope="org"):
    """Pi's emission pattern: ensure both endpoints, then the edge (lineage.ts edgeAdd)."""
    for nid in (a, b):
        feed.send("node.upsert", {"node_type": nid.split(":")[0], "node_id": nid}, scope)
    payload = {"edge_type": etype, "from_id": a, "to_id": b}
    if attrs:
        payload["attrs"] = attrs
    return feed.send("edge.add", payload, scope)


def test_realistic_pi_batch_with_two_emitter_keys_is_fully_accepted():
    repo, svc, _ = make()
    fa, fb = Feed(svc, "pia"), Feed(svc, "pib")         # two emitter keys
    fa.principal, fb.principal = A, B
    pr, commit = "pr:github.com/o/r#1", "commit:0123abc"
    issue = "issue:github.com/o/r#9"
    fa.send("node.upsert", {"node_type": "agent", "node_id": "agent:main", "label": "Main", "state": "running"})
    fa.send("node.upsert", {"node_type": "agent", "node_id": "agent:sub", "label": "Sub"})
    fb.send("node.upsert", {"node_type": "agent", "node_id": "agent:rev", "label": "Reviewer"})
    fb.send("node.upsert", {"node_type": "review", "node_id": "review:r1", "label": "Review"})
    for step in (
        lambda: pi_edge(fa, "spawn", "agent:main", "agent:sub"),
        lambda: pi_edge(fa, "handoff", "agent:main", "agent:sub"),
        lambda: pi_edge(fa, "produced", commit, pr),                       # commons -> commons, both emitters say it
        lambda: pi_edge(fb, "produced", commit, pr),
        lambda: pi_edge(fa, "issue_link", pr, issue),
        lambda: pi_edge(fb, "issue_link", pr, issue),
        lambda: pi_edge(fb, "reviewed", "review:r1", pr, {"commit": "0123abc"}),
        lambda: pi_edge(fb, "reviewed", "review:r1", pr, {"commit": "0456def", "supersedes": ["0123abc"]}),
        lambda: pi_edge(fb, "supersession", "agent:rev", "agent:rev2"),
        lambda: pi_edge(fa, "validated", "ci:42", commit, {"commit": "0123abc"}),
    ):
        step()
    # a second emitter ensuring (not changing) the first one's agent is a harmless no-op
    fb.send("node.upsert", {"node_type": "agent", "node_id": "agent:main"})
    snap = repo.graph.snapshot()
    assert snap["nodes"]["agent:main"]["owner"] == A["id"] and snap["nodes"]["agent:main"]["label"] == "Main"
    assert snap["nodes"][pr]["owner"] is None                              # commons: an ensure never claims it
    view = repo.graph.get_node(VIEWER, pr)
    kinds = sorted((e["edge_type"], e["from_id"]) for e in view["edges"])
    assert ("issue_link", pr) in kinds and ("produced", commit) in kinds and ("reviewed", "review:r1") in kinds
    stale = {e["binding"]["commit"]: e["stale"] for e in view["edges"] if e["edge_type"] == "reviewed"}
    assert stale == {"0123abc": True, "0456def": False}
    assert rebuilt(repo).graph.snapshot() == repo.graph.snapshot()


def test_commons_nodes_cannot_be_hidden_or_squatted_but_owned_ones_are_protected():
    repo, svc, _ = make()
    fa, fb = Feed(svc, "x"), Feed(svc, "y")
    fa.node("commit:abc", principal=A)                        # attacker pre-creates the predictable id
    fb.node("commit:abc", principal=B)                        # the real emitter's ensure succeeds
    fb.send("node.upsert", {"node_type": "commit", "node_id": "commit:abc", "label": "real"}, scope="private",
            principal=B)                                      # a foreign field write...
    node = repo.graph.get_node(VIEWER, "commit:abc")["node"]
    assert node["label"] is None and node["visibility_scope"] == "org"    # ...cannot narrow or hide the node
    assert repo.graph.get_node(B, "commit:abc")["node"]["label"] == "real"   # B's private field stays B's
    with pytest.raises(PermissionError):                      # but an OWNED kind refuses a changing write
        fa.node("agent:own", principal=A)
        fb.send("node.upsert", {"node_type": "agent", "node_id": "agent:own", "label": "pwn"}, principal=B)
    fb.send("node.upsert", {"node_type": "agent", "node_id": "agent:own"}, principal=B)   # ensure = ok
    assert repo.graph.snapshot()["nodes"]["agent:own"]["owner"] == A["id"]
    assert rebuilt(repo).graph.snapshot() == repo.graph.snapshot()


def test_foreign_ensure_is_a_noop_and_cannot_resurrect_or_move_anything():
    repo, svc, _ = make()
    fa, fb = Feed(svc, "pia"), Feed(svc, "pib")
    fa.principal, fb.principal = A, B
    pi_edge(fa, "dependency", "task:c", "task:d")
    fa.send("edge.remove", {"edge_type": "dependency", "from_id": "task:c", "to_id": "task:d", "reason": "r"})
    before = repo.graph.snapshot()
    for nid in ("task:c", "task:d"):                          # B ensures A's (owned) nodes and edge
        fb.send("node.upsert", {"node_type": "task", "node_id": nid})
    with pytest.raises(PermissionError):                      # an edge FROM someone else's owned node
        fb.send("edge.add", {"edge_type": "dependency", "from_id": "task:c", "to_id": "task:d"})
    assert repo.graph.neighborhood(VIEWER, "task:c")["edges"] == []        # still removed
    assert repo.graph.snapshot()["edges"] == before["edges"]
    assert rebuilt(repo).graph.snapshot() == repo.graph.snapshot()
    with pytest.raises(PermissionError):                      # changing it is refused
        fb.send("edge.add", {"edge_type": "dependency", "from_id": "task:c", "to_id": "task:d",
                             "attrs": {"x": 1}})
    # shared (commons) endpoints: each writer has its own instance, so neither can squat the other
    pi_edge(fa, "produced", "commit:c", "pr:1")
    pi_edge(fb, "produced", "commit:c", "pr:1")
    inst = {e["writer"]: e["removed"] for e in repo.graph.get_edge(VIEWER, "produced", "commit:c", "pr:1")["edges"]}
    assert inst == {A["id"]: False, B["id"]: False}


def test_node_state_on_a_missing_node_is_refused_and_operator_objects_are_operator_owned():
    repo, svc, f = make()
    with pytest.raises(PermissionError):
        f.send("node.state", {"node_id": "agent:ghost", "state": "running"})
    with pytest.raises(ValueError, match="not found"):
        f.send("node.state", {"node_id": "agent:ghost", "state": "running"}, principal=OPERATOR)
    assert not rows(repo)
    Feed(svc, "op").send("node.upsert", {"node_type": "agent", "node_id": "agent:o", "label": "op"},
                         principal=OPERATOR)
    assert repo.graph.snapshot()["nodes"]["agent:o"]["owner"] == OPERATOR["id"]
    with pytest.raises(PermissionError):
        f.send("node.upsert", {"node_type": "agent", "node_id": "agent:o", "label": "mine"})
    f.send("node.upsert", {"node_type": "agent", "node_id": "agent:o"})        # ensure ok


def test_http_forbidden_is_distinguishable_from_auth_failure_and_uniform(http):
    server, call, keys = http
    op, vw, ing = list(keys)
    ing2 = "in2-" + "k"
    server._keys[ing2] = "ingest"
    n = [0]

    def post(key, et, payload, source="pi"):
        n[0] += 1
        return call("/api/v1/ingest", key, {
            "schema_version": 2, "event_id": f"u{n[0]}", "source": source, "source_sequence": n[0],
            "entity_type": "lineage", "entity_id": payload.get("node_id") or payload["from_id"],
            "event_type": et, "payload": payload, "visibility_scope": "private"})

    assert post(ing, "node.upsert", {"node_type": "agent", "node_id": "agent:secret", "label": "s"})[0] == 200
    existing = post(ing2, "node.upsert", {"node_type": "agent", "node_id": "agent:secret", "label": "x"})
    missing_state = post(ing2, "node.state", {"node_id": "agent:ghost", "state": "running"})
    assert existing[0] == missing_state[0] == 403
    assert existing[1] == missing_state[1] == {
        "error": "not permitted to modify this lineage object", "reason": "lineage_forbidden"}
    status, body = call("/api/v1/ingest", "bad-" + "key", {"schema_version": 2})
    assert status == 401 and "reason" not in body                 # auth failures carry no lineage reason
    # an ensure of the foreign private node is accepted: no existence oracle
    assert post(ing2, "node.upsert", {"node_type": "agent", "node_id": "agent:secret"})[0] == 200
    assert post(ing2, "node.upsert", {"node_type": "agent", "node_id": "agent:fresh"})[0] == 200


def test_at_quota_refusals_are_identical_for_existing_and_missing(monkeypatch):
    from herdr_engineering.control_plane import graph as graph_mod
    monkeypatch.setattr(graph_mod, "MAX_OBJECTS_PER_PRINCIPAL", 2)
    repo, svc, _ = make()
    fv, fa = Feed(svc, "v"), Feed(svc, "a")
    fv.send("node.upsert", {"node_type": "agent", "node_id": "agent:secret"}, scope="private", principal=V)
    for i in range(2):
        fa.send("node.upsert", {"node_type": "agent", "node_id": f"agent:m{i}"}, principal=A)
    outcomes = []
    for et, pl in (("node.upsert", {"node_type": "agent", "node_id": "agent:secret"}),
                   ("node.upsert", {"node_type": "agent", "node_id": "agent:nothere"}),
                   ("node.upsert", {"node_type": "agent", "node_id": "agent:secret", "label": "x"}),
                   ("edge.add", {"edge_type": "spawn", "from_id": "agent:m0", "to_id": "agent:secret"}),
                   ("edge.add", {"edge_type": "spawn", "from_id": "agent:m0", "to_id": "agent:nothere"})):
        with pytest.raises(PermissionError) as exc:
            fa.send(et, pl, principal=A)
        outcomes.append(str(exc.value))
    assert len(set(outcomes)) == 1


def test_quota_and_rebuild_follow_the_pinned_owner_not_apply_order():
    repo, svc, _ = make()
    fb, fa, fo = Feed(svc, "sb"), Feed(svc, "sa"), Feed(svc, "so")
    fb.send("node.upsert", {"node_type": "agent", "node_id": "agent:other"}, principal=B, entity_id="s1")
    fa.send("node.upsert", {"node_type": "agent", "node_id": "agent:n", "label": "a"}, principal=A, entity_id="s2")
    fo.send("node.upsert", {"node_type": "agent", "node_id": "agent:n", "attrs": {"co_writers": [B["id"]]}},
            principal=OPERATOR, entity_id="s3")
    fb.send("node.upsert", {"node_type": "agent", "node_id": "agent:n", "label": "b"}, principal=B, entity_id="s1")
    live = repo.graph.snapshot()
    assert live["owned"] == {A["id"]: 1, B["id"]: 1}
    assert rebuilt(repo).graph.snapshot() == live
    events = rows(repo)
    for seed in range(10):
        shuffled = random.Random(seed).sample(events, len(events))
        g = GraphProjection()
        for e in shuffled:
            g.apply(from_stored(e))
        assert g.snapshot() == live and g._owned == repo.graph._owned


def _bulk(g, actor, n, start=0):
    """admit + apply n edges straight onto the projection (no HTTP)."""
    for i in range(start, start + n):
        pl = {"edge_type": "reviewed", "from_id": "agent:a", "to_id": "commit:h", "attrs": {"commit": f"c{i}"}}
        op = g.admit(actor, "edge.add", pl, "s")
        g.apply({"event_id": f"b{i}", "source": "s", "source_sequence": i, "event_type": "edge.add",
                 "payload": pl, "visibility_scope": "org", "actor": actor,
                 "lineage_seq": g.next_seq(), "lineage_mode": op["mode"],
                 "ingest_timestamp": "2026-10-22T10:00:00+00:00"})


def _hub():
    import time
    g = GraphProjection()
    seq = [0]

    def put(actor, et, pl):
        seq[0] += 1
        op = g.admit(actor, et, pl, "s0")
        g.apply({"event_id": f"p{seq[0]}", "source": "s0", "source_sequence": seq[0], "event_type": et,
                 "payload": pl, "visibility_scope": "org", "actor": actor, "lineage_seq": g.next_seq(),
                 "lineage_mode": op["mode"], "ingest_timestamp": "2026-10-22T10:00:00+00:00"})

    put(V, "node.upsert", {"node_type": "commit", "node_id": "commit:h"})
    put(V, "node.upsert", {"node_type": "agent", "node_id": "agent:v"})
    put(V, "edge.add", {"edge_type": "reviewed", "from_id": "agent:v", "to_id": "commit:h",
                        "attrs": {"commit": "vv"}})
    put(A, "node.upsert", {"node_type": "agent", "node_id": "agent:a"})
    return g, time


def test_review_fan_in_hub_reads_stay_fast_and_ingest_stays_linear():
    g, time = _hub()
    t = time.perf_counter()
    _bulk(g, A, 2500)
    first = time.perf_counter() - t
    t = time.perf_counter()
    _bulk(g, A, 2500, start=2500)
    second = time.perf_counter() - t
    assert second < max(first * 3.0, 0.5)                    # no quadratic growth in ingest
    for read in (lambda: g.get_node(V, "commit:h"),
                 lambda: g.neighborhood(V, "commit:h", depth=2, max_edges=2000),
                 lambda: g.get_edge(V, "reviewed", "agent:v", "commit:h")):
        t = time.perf_counter()
        out = read()
        assert time.perf_counter() - t < 0.5        # ~0.05-0.1 s idle; loose for a busy CI box
    node = g.get_node(V, "commit:h")
    assert node["edges_truncated"] is True
    mine = [e for e in node["edges"] if e["from_id"] == "agent:v"][0]
    assert mine["stale"] is False                             # unrelated reviewer cannot mark it stale
    assert len(mine["bindings"]) <= 21 and mine["bindings_truncated"] is True
    assert any(e["from_id"] == "agent:v" for e in g.neighborhood(V, "commit:h")["edges"])


def test_flood_cannot_hide_a_victims_edge_and_pages_are_visible_only():
    repo, svc, _ = make()
    fv, fa = Feed(svc, "v"), Feed(svc, "a")
    fv.node("commit:v", principal=V)
    fv.node("task:real", principal=V)
    fv.send("edge.add", {"edge_type": "produced", "from_id": "task:real", "to_id": "commit:v"}, principal=V)
    n_org = 0
    for i in range(400):
        fa.send("node.upsert", {"node_type": "task", "node_id": f"task:a{i:04d}"}, principal=A)
        private = i % 3 == 0
        n_org += not private
        fa.send("edge.add", {"edge_type": "produced", "from_id": f"task:a{i:04d}", "to_id": "commit:v"},
                scope="private" if private else "org", principal=A)
    g = repo.graph
    page = g.get_node(V, "commit:v")
    assert any(e["from_id"] == "task:real" for e in page["edges"])           # own edges are exempt
    assert page["edges_truncated"] is True
    returned_others = len(page["edges"]) - 1
    assert page["edges_omitted"] == n_org - returned_others                  # visible items only
    assert all(e["visibility_scope"] == "org" for e in page["edges"])        # hidden ones never show or count
    # newest admission first, and a cursor reaches the rest without repeats
    first_ids = [e["from_id"] for e in page["edges"] if e["from_id"] != "task:real"]
    assert first_ids == sorted(first_ids, reverse=True)
    p2 = g.get_node(V, "commit:v", offset=page["next_offset"])
    assert not {e["from_id"] for e in page["edges"]} & {e["from_id"] for e in p2["edges"]}
    only = g.get_node(V, "commit:v", from_id="task:real")
    assert [e["from_id"] for e in only["edges"]] == ["task:real"] and only["edges_omitted"] == 0
    assert g.get_node(V, "commit:v", edge_type="spawn")["edges"] == []
    nb = g.neighborhood(V, "commit:v", depth=1, max_nodes=10)
    assert "task:real" in {n["id"] for n in nb["nodes"]} and nb["truncated"] is True


def test_http_node_edge_filters_and_paging(http):
    server, call, keys = http
    op, vw, ing = list(keys)
    n = [0]

    def post(et, payload):
        n[0] += 1
        return call("/api/v1/ingest", ing, {
            "schema_version": 2, "event_id": f"f{n[0]}", "source": "pi", "source_sequence": n[0],
            "entity_type": "lineage", "entity_id": payload.get("node_id") or payload["from_id"],
            "event_type": et, "payload": payload, "visibility_scope": "org"})

    post("node.upsert", {"node_type": "commit", "node_id": "commit:h"})
    for i in range(5):
        post("node.upsert", {"node_type": "task", "node_id": f"task:{i}"})
        post("edge.add", {"edge_type": "produced", "from_id": f"task:{i}", "to_id": "commit:h"})
    s, body = call("/api/v1/graph/nodes/commit%3Ah?limit=2", vw)
    assert s == 200 and len(body["edges"]) == 2 and body["next_offset"] == 2 and body["edges_omitted"] >= 3
    s, body = call("/api/v1/graph/nodes/commit%3Ah?limit=2&offset=4", vw)
    assert s == 200 and len(body["edges"]) == 1 and body["next_offset"] is None
    s, body = call("/api/v1/graph/nodes/commit%3Ah?from_id=task%3A3", vw)
    assert [e["from_id"] for e in body["edges"]] == ["task:3"]
    assert call("/api/v1/graph/nodes/commit%3Ah?limit=0", vw)[0] == 400
    assert call("/api/v1/graph/nodes/commit%3Ah?edge_type=nope", vw)[0] == 400


# ------------------------------------------------------------------ round 4
def P(name):
    return {"kind": "agent", "id": f"key:{name}", "authenticated_by": "api_key"}


def feed_for(svc, source, principal):
    f = Feed(svc, source)
    f.principal = principal
    return f


def raw_lineage(svc, principal, event_id, source, seq, label, node="pr:o/r#z"):
    return svc.ingest({
        "schema_version": 2, "event_id": event_id, "source": source, "source_sequence": seq,
        "entity_type": "lineage", "entity_id": node, "event_type": "node.upsert",
        "payload": {"node_type": node.split(":")[0], "node_id": node, "label": label},
        "visibility_scope": "org"}, principal)


def test_client_cannot_forge_admission_mode_or_sequence():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    for key in ("_envelope", "_lineage_seq"):
        with pytest.raises(ValueError):
            fa.send("node.upsert", {"node_type": "pr", "node_id": "pr:o/r#8", "label": "mine",
                                    key: {"lineage_mode": "foreign", "lineage_seq": 0}})
    # a forged envelope hidden inside attrs is ordinary data and cannot change the mode
    fa.node("pr:o/r#7", label="mine", attrs={"_envelope": {"lineage_mode": "noop"}})
    assert repo.graph.snapshot()["nodes"]["pr:o/r#7"]["label"] == "mine"
    fa.node("pr:o/r#9", label="mine", attrs={"envelope": {"lineage_mode": "noop"}})
    fv.node("pr:o/r#9", label="victim")
    live = repo.graph.snapshot()
    assert live == rebuilt(repo).graph.snapshot()
    # an event whose payload carries a forged envelope is ignored by apply: only the
    # server-set top-level keys count
    g = GraphProjection()
    g.apply({"event_id": "x", "source": "s", "source_sequence": 0, "event_type": "node.upsert",
             "payload": {"node_type": "task", "node_id": "task:x", "label": "real",
                         "_envelope": {"lineage_mode": "noop", "lineage_seq": 10**9}},
             "visibility_scope": "org", "actor": A, "lineage_seq": 1})
    assert g.snapshot()["nodes"]["task:x"]["label"] == "real"


def test_commons_overwrite_keeps_primary_value_and_attributes_the_contribution():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    fv.node("pr:o/r#5", label="Fix bug", state="open", attrs={"url": "https://good.example"})
    fa.node("pr:o/r#5", label="FREE STUFF", state="merged", attrs={"url": "https://evil.example"})
    for g in (repo.graph, rebuilt(repo).graph):
        n = g.get_node(V, "pr:o/r#5")["node"]
        assert (n["label"], n["state"], n["attrs"]["url"]) == ("Fix bug", "open", "https://good.example")
        assert n["fields"]["label"]["writer"] == V["id"] and n["source"] == "gh"
        other = [c for c in n["contributions"]["label"] if c["writer"] == A["id"]]
        assert other and other[0]["value"] == "FREE STUFF" and other[0]["source"] == "evil"
        assert n["contributions"]["state"][0]["value"] == "merged"
    # an operator's value outranks the owner's
    fo = feed_for(svc, "ops", OPERATOR)
    fo.node("pr:o/r#5", state="closed")
    assert repo.graph.get_node(V, "pr:o/r#5")["node"]["state"] == "closed"


def test_precreating_a_commons_node_privately_cannot_hide_it():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    fa.node("pr:o/r#6", scope="private", label="x")
    fv.node("pr:o/r#6", scope="org", label="real", state="open")
    for g in (repo.graph, rebuilt(repo).graph):
        assert g.get_node(OTHER, "pr:o/r#6") is not None, "widest scope wins on commons"
        assert g.get_node(OTHER, "pr:o/r#6")["node"]["state"] == "open"
    # an owned kind keeps the narrowest scope (a co-writer cannot widen it)
    fa.node("agent:s", scope="private", label="s")
    with pytest.raises(KeyError):
        repo.graph.get_node(V, "agent:s")


def test_commons_from_edges_are_per_writer_and_removable_by_endpoint_owner():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    fv.node("commit:abc", label="c")
    fv.node("pr:p1", label="p")
    fa.edge("produced", "commit:abc", "pr:p1", {"note": "fake"}, nodes=False)
    fv.edge("produced", "commit:abc", "pr:p1", {"ci": "green"}, nodes=False)
    for g in (repo.graph, rebuilt(repo).graph):
        edges = g.get_node(V, "pr:p1")["edges"]
        mine = [e for e in edges if e["writer"] == V["id"]]
        theirs = [e for e in edges if e["writer"] == A["id"]]
        assert len(mine) == 1 and mine[0]["attrs"] == {"ci": "green"}
        assert len(theirs) == 1 and theirs[0]["attrs"] == {"note": "fake"}
    # the victim's own remove only drops the victim's instance
    fv.send("edge.remove", {"edge_type": "produced", "from_id": "commit:abc", "to_id": "pr:p1", "reason": "mine"})
    left = repo.graph.get_node(V, "pr:p1")["edges"]
    assert [e["writer"] for e in left] == [A["id"]]
    fv.edge("produced", "commit:abc", "pr:p1", {"ci": "green"}, nodes=False)
    # a COMMONS endpoint confers no tombstone authority: the victim cannot remove the squatter's instance
    with pytest.raises(PermissionError):
        fv.send("edge.remove", {"edge_type": "produced", "from_id": "commit:abc", "to_id": "pr:p1",
                                "attrs": {"writer": A["id"]}, "reason": "squat"})
    stranger = feed_for(svc, "x", P("stranger"))
    with pytest.raises(PermissionError):
        stranger.send("edge.remove", {"edge_type": "produced", "from_id": "commit:abc",
                                      "to_id": "pr:p1", "attrs": {"writer": V["id"]}, "reason": "x"})
    # an operator moderates
    feed_for(svc, "ops", OPERATOR).send("edge.remove", {
        "edge_type": "produced", "from_id": "commit:abc", "to_id": "pr:p1",
        "attrs": {"writer": A["id"]}, "reason": "squat"})
    assert [e["writer"] for e in repo.graph.get_node(V, "pr:p1")["edges"]] == [V["id"]]
    assert repo.graph.snapshot() == rebuilt(repo).graph.snapshot()


def test_owner_of_an_owned_endpoint_may_tombstone_a_squatters_instance():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    fv.node("task:mine", label="t")
    fv.node("commit:abc", label="c")
    fa.edge("produced", "commit:abc", "task:mine", nodes=False)
    fv.send("edge.remove", {"edge_type": "produced", "from_id": "commit:abc", "to_id": "task:mine",
                            "attrs": {"writer": A["id"]}, "reason": "squat"})
    assert repo.graph.get_node(V, "task:mine")["edges"] == []
    assert repo.graph.snapshot() == rebuilt(repo).graph.snapshot()


def test_edge_instances_per_triple_are_bounded():
    from herdr_engineering.control_plane.graph import MAX_INSTANCES_PER_TRIPLE
    repo, svc, _ = make()
    fv = feed_for(svc, "gh", V)
    fv.node("commit:abc")
    fv.node("pr:p1")
    done = 0
    with pytest.raises((PermissionError, ValueError)):
        for i in range(MAX_INSTANCES_PER_TRIPLE + 3):
            f = feed_for(svc, "s", P(f"w{i}"))
            f.edge("produced", "commit:abc", "pr:p1", nodes=False)
            done += 1
    assert done == MAX_INSTANCES_PER_TRIPLE


def test_node_state_does_not_reveal_whether_a_hidden_object_exists():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    fa.node("pr:o/r#6", scope="private", label="x")
    # hidden commons and missing commons behave identically: both accepted
    assert fv.send("node.state", {"node_id": "pr:o/r#6", "state": "open"})["event_type"] == "node.state"
    assert fv.send("node.state", {"node_id": "pr:o/r#none", "state": "open"})["event_type"] == "node.state"
    # hidden and missing owned nodes are refused the same way
    fa.node("agent:secret", scope="private", label="s")
    errs = []
    for nid in ("agent:secret", "agent:absent"):
        with pytest.raises(PermissionError) as ei:
            fv.send("node.state", {"node_id": nid, "state": "open"})
        errs.append(str(ei.value))
    assert errs[0] == errs[1]


def test_attr_cap_exhaustion_on_commons_node_cannot_lock_out_the_victim():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "gh", V), feed_for(svc, "evil", A)
    fv.node("pr:p2", label="p", attrs={"a": 1})
    refused = 0
    for i in range(3000):           # fill key by key until the attacker is refused
        try:
            fa.node("pr:p2", attrs={f"junk{i}": "x" * 100})
        except (PermissionError, ValueError):
            refused += 1
            break
    assert refused == 1, "the attacker is capped per writer / per object"
    assert fv.node("pr:p2", attrs={"ci": "green"})["event_type"] == "node.upsert"
    assert repo.graph.get_node(V, "pr:p2")["node"]["attrs"]["ci"] == "green"
    assert repo.graph.snapshot() == rebuilt(repo).graph.snapshot()


def test_event_id_is_unique_per_principal_not_global():
    repo, svc, _ = make()
    raw_lineage(svc, A, "gh-delivery-123", "evil", 0, "squat")
    # the victim's identical event_id (different or same source/seq) is a distinct event
    ack = raw_lineage(svc, V, "gh-delivery-123", "gh", 0, "real")
    assert ack["envelope"]["duplicate"] is False
    ack = raw_lineage(svc, P("v2"), "gh-delivery-123", "evil", 0, "real2")
    assert ack["envelope"]["duplicate"] is False       # sequence domain is per principal too
    # the same principal replaying its own id is still a duplicate
    assert raw_lineage(svc, A, "gh-delivery-123", "evil", 0, "squat")["envelope"]["duplicate"] is True
    # ... and the same id with different content is still a conflict for that principal
    from herdr_engineering.control_plane.envelope import EnvelopeConflict
    with pytest.raises(EnvelopeConflict):
        raw_lineage(svc, A, "gh-delivery-123", "evil", 0, "changed")


def test_event_id_isolation_survives_ledger_eviction():
    from herdr_engineering.control_plane.envelope import EnvelopeConflict
    repo, svc, _ = make()
    raw_lineage(svc, A, "gh-delivery-123", "evil", 0, "squat")
    repo.ledger._events.clear()                         # as after eviction / restart
    repo.ledger._sequences.clear()
    ack = raw_lineage(svc, V, "gh-delivery-123", "gh", 0, "real")
    assert ack["envelope"]["duplicate"] is False
    # the attacker's own replay after eviction is still recognised as its duplicate or rejected
    # without ever touching the victim's event
    try:
        raw_lineage(svc, A, "gh-delivery-123", "evil", 0, "squat")
    except EnvelopeConflict:
        pass
    node = repo.graph.get_node(V, "pr:o/r#z")["node"]
    assert {"squat", "real"} >= {c["value"] for c in node["contributions"]["label"]} | {node["label"]}
    assert node["label"] == "squat" and node["contributions"]["label"][0]["value"] == "real"


def test_shuffled_replay_with_commons_contributions_is_deterministic():
    repo, svc, _ = make()
    fv, fa, fb = feed_for(svc, "gh", V), feed_for(svc, "evil", A), feed_for(svc, "b", P("c"))
    for i in range(5):
        fv.node(f"pr:p{i}", label=f"v{i}", state="open", attrs={"k": i})
        fa.node(f"pr:p{i}", label=f"a{i}", attrs={"k": 100 + i})
        fa.send("node.upsert", {"node_type": "pr", "node_id": f"pr:p{i}"})
        fb.send("node.upsert", {"node_type": "commit", "node_id": f"commit:c{i}", "label": "c"})
        fv.send("node.upsert", {"node_type": "commit", "node_id": f"commit:c{i}"})
        fa.send("node.state", {"node_id": f"pr:p{i}", "state": "closed"})
        fb.edge("produced", f"commit:c{i}", f"pr:p{i}", nodes=False)
        fv.edge("produced", f"commit:c{i}", f"pr:p{i}", nodes=False)
    ref = repo.graph.snapshot()
    evs = [from_stored(e) for e in rows(repo)]
    assert evs
    for seed in range(15):
        random.Random(seed).shuffle(evs)
        g = GraphProjection()
        for e in evs:
            g.apply(e)
        assert g.snapshot() == ref
    n = repo.graph.get_node(OPERATOR, "pr:p0")["node"]
    assert n["label"] == "v0" and n["contributions"]["label"]


# ------------------------------------------------------------------ round 5
def _view_json(g, principal, nid):
    return json.dumps(g.get_node(principal, nid), sort_keys=True, default=str)


@pytest.mark.parametrize("attacker_write", ["attrs", "state"])
def test_widening_a_private_commons_node_does_not_expose_its_private_fields(attacker_write):
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "v", V), feed_for(svc, "a", A)
    secret = "SECRET-" + "x" * 5
    fv.node("pr:secret-1", scope="private", label=secret, attrs={"note": secret}, state="open")
    with pytest.raises(KeyError):
        repo.graph.get_node(A, "pr:secret-1")
    if attacker_write == "attrs":
        fa.node("pr:secret-1", scope="org", attrs={"x": 1})
    else:
        fa.send("node.state", {"node_id": "pr:secret-1", "state": "x"}, scope="org")
    for g in (repo.graph, rebuilt(repo).graph):
        for viewer in (A, OTHER, VIEWER):
            text = _view_json(g, viewer, "pr:secret-1")
            assert secret not in text and "v-0" not in text and "key:victim" not in text
            n = g.get_node(viewer, "pr:secret-1")["node"]
            assert n["label"] is None and "note" not in n["attrs"] and n["visibility_scope"] == "org"
            nb = json.dumps(g.neighborhood(viewer, "pr:secret-1"), default=str)
            assert secret not in nb and "key:victim" not in nb
        mine = g.get_node(V, "pr:secret-1")["node"]
        assert mine["label"] == secret and mine["attrs"]["note"] == secret   # the owner still sees all
    # the attacker's own field is what the others see
    seen = repo.graph.get_node(OTHER, "pr:secret-1")["node"]
    assert (seen["attrs"].get("x") == 1) if attacker_write == "attrs" else (seen["state"] == "x")


def test_commons_field_visibility_is_per_writer_in_both_directions():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "v", V), feed_for(svc, "a", A)
    fv.node("pr:p", scope="org", label="public-label", attrs={"k": "pub"})
    fa.node("pr:p", scope="private", label="hidden-attacker", attrs={"z": "secret-a"})
    for g in (repo.graph, rebuilt(repo).graph):
        n = g.get_node(OTHER, "pr:p")["node"]
        assert n["label"] == "public-label" and n["attrs"] == {"k": "pub"}
        assert "hidden-attacker" not in _view_json(g, OTHER, "pr:p")
        assert "secret-a" not in _view_json(g, V, "pr:p")          # nor does the victim see the attacker's
        assert g.get_node(A, "pr:p")["node"]["attrs"]["z"] == "secret-a"
    # an operator-only scope is gated by the same rule
    fv.node("pr:q", scope="org", label="q")
    fa.node("pr:q", scope="mission", label="ops-only")
    assert "ops-only" not in _view_json(repo.graph, OTHER, "pr:q")
    assert "ops-only" in _view_json(repo.graph, OPERATOR, "pr:q")


def test_hidden_registers_do_not_leak_through_bindings_ids_or_counts():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "v", V), feed_for(svc, "a", A)
    fv.node("pr:p", scope="private", label="L")
    fv.node("task:t", scope="private")
    fv.edge("produced", "pr:p", "task:t", scope="private", nodes=False)
    fa.node("pr:p", scope="org", attrs={"x": 1})
    n = repo.graph.get_node(OTHER, "pr:p")
    assert n["edges"] == [] and n["edges_omitted"] == 0 and n["edges_truncated"] is False
    node = n["node"]
    assert node["first_event_id"] == node["last_event_id"] == "a-0" or node["first_event_id"] == "a-1"
    assert node["source"] == "a"
    assert "task:t" not in json.dumps(repo.graph.neighborhood(OTHER, "pr:p"), default=str)


def test_contributions_keep_owner_and_operator_entries_against_spam():
    repo, svc, _ = make()
    fv = feed_for(svc, "v", V)
    fo = feed_for(svc, "ops", OPERATOR)
    fv.node("pr:p", label="owner-label")
    fo.node("pr:p", label="op-label")                 # operator is primary; owner's becomes a contribution
    for i in range(12):
        feed_for(svc, f"s{i}", P(f"spam{i}")).node("pr:p", label=f"spam{i}")
    for g in (repo.graph, rebuilt(repo).graph):
        n = g.get_node(OTHER, "pr:p")["node"]
        assert n["label"] == "op-label"
        writers = [c["writer"] for c in n["contributions"]["label"]]
        assert writers[0] == V["id"] and len(writers) <= 5
    mine = feed_for(svc, "late", P("late"))
    mine.node("pr:p", label="late-label")
    seen = [c["writer"] for c in repo.graph.get_node(P("late"), "pr:p")["node"]["contributions"]["label"]]
    assert V["id"] in seen                              # owner kept even when spammed out otherwise


def test_an_empty_ensure_does_not_make_the_ensurer_the_commons_owner():
    repo, svc, _ = make()
    fv, fa = feed_for(svc, "v", V), feed_for(svc, "a", A)
    fa.send("node.upsert", {"node_type": "issue", "node_id": "issue:7"})           # attacker ensures first
    fv.node("pr:9", label="pr")
    fv.send("node.upsert", {"node_type": "issue", "node_id": "issue:7", "label": "the issue",
                            "attrs": {"url": "https://good.example"}})
    fa.node("issue:7", attrs={"url": "https://evil.example"})
    for g in (repo.graph, rebuilt(repo).graph):
        snap = g.snapshot()["nodes"]["issue:7"]
        assert snap["owner"] == V["id"]
        n = g.get_node(OTHER, "issue:7")["node"]
        assert n["attrs"]["url"] == "https://good.example" and n["label"] == "the issue"
    fv.edge("issue_link", "pr:9", "issue:7", {"k": 1}, nodes=False)
    for who, key in ((A, "key:victim"),):
        with pytest.raises(PermissionError):
            fa.send("edge.remove", {"edge_type": "issue_link", "from_id": "pr:9", "to_id": "issue:7",
                                    "reason": "x", "attrs": {"writer": key}})
    assert [e["writer"] for e in repo.graph.get_node(V, "pr:9")["edges"]] == [V["id"]]


def test_tombstoned_instances_do_not_count_toward_the_instance_cap():
    from herdr_engineering.control_plane.graph import MAX_INSTANCES_PER_TRIPLE as CAP
    repo, svc, _ = make()
    fv = feed_for(svc, "v", V)
    fv.node("commit:c")
    fv.node("pr:p")
    feeds = []
    for i in range(CAP):
        f = feed_for(svc, f"s{i}", P(f"w{i}"))
        f.edge("produced", "commit:c", "pr:p", nodes=False)
        feeds.append(f)
    extra = feed_for(svc, "extra", P("extra"))
    with pytest.raises(ValueError):
        extra.edge("produced", "commit:c", "pr:p", nodes=False)
    feeds[0].send("edge.remove", {"edge_type": "produced", "from_id": "commit:c", "to_id": "pr:p", "reason": "done"})
    extra.edge("produced", "commit:c", "pr:p", nodes=False)       # a freed slot is usable
    assert repo.graph.snapshot() == rebuilt(repo).graph.snapshot()


# ------------------------------------------------------------------ round 6
def _apply(g, actor, source, seq, node, scope, attrs, et="node.upsert", payload=None, admit=True):
    pl = payload or {"node_type": node.split(":")[0], "node_id": node, "attrs": attrs}
    op = g.admit(actor, et, pl, source) if admit else {"mode": "normal"}
    g.apply({"event_id": f"{source}-{seq}", "source": source, "source_sequence": seq, "event_type": et,
             "payload": pl, "visibility_scope": scope, "actor": actor, "lineage_seq": g.next_seq(),
             "lineage_mode": op["mode"], "ingest_timestamp": "2026-10-22T10:00:00+00:00"})


def test_primary_fallback_is_the_first_writer_and_stable_under_updates():
    repo, svc, _ = make()
    fo, fl, fx = (feed_for(svc, n, P(n)) for n in ("o", "l", "x"))
    fo.node("pr:1", attrs={"title": "t"})          # the owner writes a different key
    fl.node("pr:1", attrs={"status": "L1"})
    fx.node("pr:1", attrs={"status": "EVIL"})
    fl.node("pr:1", attrs={"status": "L2"})        # the legitimate writer updates its own value
    for g in (repo.graph, rebuilt(repo).graph):
        n = g.get_node(OTHER, "pr:1")["node"]
        assert n["attrs"]["status"] == "L2" and n["fields"]["attrs"]["status"]["writer"] == "key:l"
        assert [c["value"] for c in n["contributions"]["attrs"]["status"]] == ["EVIL"]


def test_commons_view_does_not_depend_on_writers_the_reader_cannot_see():
    """Non-interference: removing a private-only writer's events changes nothing a reader sees."""
    def build(with_hidden):
        repo, svc, _ = make()
        f = {n: feed_for(svc, n, P(n)) for n in ("V", "A", "B")}
        if with_hidden:
            f["V"].node("pr:1", scope="private", attrs={"q": 1}, label="hidden")
        f["A"].node("pr:1", attrs={"k": "A1"})
        f["B"].node("pr:1", attrs={"k": "B1"})
        f["B"].node("pr:1", attrs={"j": "B"})
        f["A"].node("pr:1", attrs={"k": "A2"})
        n = repo.graph.get_node(OTHER, "pr:1")["node"]
        n = json.loads(json.dumps(n))
        # event ids are numbered per feed, so compare without them
        for fld in (n["fields"]["attrs"].values()):
            fld.pop("event_id"), fld.pop("admission"), fld.pop("admitted_at")
        for lst in n.get("contributions", {}).get("attrs", {}).values():
            for c in lst:
                c.pop("event_id"), c.pop("admission"), c.pop("admitted_at")
        for k in ("first_event_id", "last_event_id"):
            n.pop(k)
        n["fields"]["label"] = n["fields"]["state"] = None
        return n
    assert build(True) == build(False)


def test_property_hidden_writers_never_change_what_a_reader_sees_of_commons():
    """Random commons traffic: dropping the private-only victim's events leaves every other
    principal's views of the commons nodes (node, neighborhood) identical, apart from event ids."""
    rng = random.Random(7)
    names = ["a", "b", "c", "op"]
    for seed in range(40):
        rng = random.Random(seed)
        script = []
        for step in range(40):
            who = rng.choice(names + ["victim"])
            node = rng.choice(["pr:1", "pr:2", "commit:c1"])
            scope = "private" if who == "victim" else rng.choice(["org", "org", "mission"])
            attrs = {rng.choice("xyz"): rng.choice("pqr")}
            script.append((who, node, scope, attrs))

        def view(include_victim):
            g = GraphProjection()
            counter = {}
            for who, node, scope, attrs in script:
                if who == "victim" and not include_victim:
                    continue
                actor = OPERATOR if who == "op" else P(who)
                counter[who] = counter.get(who, 0) + 1
                _apply(g, actor, who, counter[who], node, scope, attrs)
            out = {}
            for viewer in (OTHER, P("a"), OPERATOR):
                for node in ("pr:1", "pr:2", "commit:c1"):
                    try:
                        n = g.get_node(viewer, node)["node"]
                    except KeyError:
                        out[(viewer["id"], node)] = None
                        continue
                    out[(viewer["id"], node)] = (n["label"], n["state"], n["attrs"], n["visibility_scope"],
                                                 {k: (v or {}).get("writer") for k, v in n["fields"]["attrs"].items()},
                                                 {k: [c["writer"] for c in v]
                                                  for k, v in n.get("contributions", {}).get("attrs", {}).items()})
            return out
        assert view(True) == view(False), f"seed {seed}"


def test_neighborhood_stops_rendering_neighbours_once_the_budget_is_spent():
    import time
    g = GraphProjection()
    hub = P("hub")
    _apply(g, hub, "sh", 0, "task:hub", "org", {}, payload={"node_type": "task", "node_id": "task:hub", "state": "running"})
    seq = 0
    for ni in range(120):
        nid = f"pr:{ni}"
        for w in range(16):
            for s in range(4):
                for half in range(2):
                    seq += 1
                    _apply(g, P(f"w{w}"), f"s{w}{s}", seq, nid, "org" if (w + s) % 3 else "private",
                           {f"k{half}_{j}": f"v{w}{s}{j}" for j in range(64)}, admit=False)
        seq += 1
        _apply(g, hub, "sh", seq, nid, "org", {}, et="edge.add",
               payload={"edge_type": "spawn", "from_id": "task:hub", "to_id": nid})
    best = 9.0
    for _ in range(3):
        t = time.perf_counter()
        r = g.neighborhood(OTHER, "task:hub", depth=1)
        best = min(best, time.perf_counter() - t)
    assert r["truncated"] is True
    assert best < 0.3, f"neighborhood took {best:.3f}s"


def test_admission_limit_errors_do_not_depend_on_hidden_writers():
    from herdr_engineering.control_plane.graph import MAX_INSTANCES_PER_TRIPLE as CAP
    repo, svc, _ = make()
    fv = feed_for(svc, "v", V)
    fv.node("commit:c")
    fv.node("pr:p")
    for i in range(CAP):
        feed_for(svc, f"s{i}", P(f"w{i}")).edge("produced", "commit:c", "pr:p", scope="private", nodes=False)
    with pytest.raises(ValueError) as ei:
        feed_for(svc, "x", P("x")).edge("produced", "commit:c", "pr:p", nodes=False)
    assert str(ei.value) == "lineage object limit reached"
    # a commons attr overflow caused by other (possibly hidden) writers says the same
    fo = feed_for(svc, "o", P("o"))
    with pytest.raises(ValueError) as ei2:
        for i in range(40):
            feed_for(svc, f"big{i}", P(f"big{i}")).node("pr:q", scope="private",
                                                         attrs={f"k{i}_{j}": "x" * 400 for j in range(30)})
    assert str(ei2.value) == "lineage object limit reached"
