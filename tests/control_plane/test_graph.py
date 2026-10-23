"""Lineage graph projection (docs/contracts/lineage-events.md, T02)."""
import json
import random
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.graph import MAX_DEPTH, GraphProjection, can_see
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
            g.apply(e)
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
    assert repo.graph.snapshot() == {"nodes": {}, "edges": {}, "wildcard_removes": {}}
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
    rows = [{**r, "ingest_timestamp": f"2026-10-22T10:00:{i:02d}+00:00",
             "source_timestamp": f"2026-10-22T10:00:{i:02d}+00:00", "sequence": i}
            for i, r in enumerate(conn.rows)]
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
        lambda: hostile.send("node.upsert", {"node_type": "agent", "node_id": "agent:a1"}, scope="private",
                             principal=OTHER),
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
            g.apply(e)
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
    monkeypatch.setattr(graph_mod, "MAX_TOTAL_NODES", 3)
    f.node("task:1")
    f.node("task:2")
    f.node("task:3")
    with pytest.raises(ValueError, match="capacity"):
        f.node("task:4")
    f.node("task:3", label="existing nodes stay writable")
    monkeypatch.setattr(graph_mod, "MAX_TOTAL_EDGES", 1)
    f.edge("dependency", "task:1", "task:2")
    with pytest.raises(ValueError, match="capacity"):
        f.edge("dependency", "task:2", "task:3")
    monkeypatch.setattr(graph_mod, "MAX_SOURCES_PER_OBJECT", 2)
    Feed(svc, source="s1").node("task:1")
    with pytest.raises(ValueError, match="too many distinct writers"):
        Feed(svc, source="s2").node("task:1")


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
        ("node.upsert", {"node_type": "agent", "node_id": "agent:pub"}),                     # not writer of node
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
        fa.send("node.upsert", {"node_type": "agent", "node_id": "agent:x"}, principal=A)
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
    with pytest.raises(ValueError, match="quota"):
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
