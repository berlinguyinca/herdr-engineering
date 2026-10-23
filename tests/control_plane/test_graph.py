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
ANON = {"kind": "unknown", "id": None, "authenticated_by": "none"}


class Feed:
    """Emits lineage envelopes with a monotonically increasing source_sequence."""

    def __init__(self, svc, source="pi"):
        self.svc, self.source, self.n = svc, source, 0
        self.known = set()

    def send(self, event_type, payload, scope="org", entity_id=None, seq=None, principal=WRITER, **extra):
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
        self.known.add(node_id)
        return self.send("node.upsert", {"node_type": node_id.split(":")[0], "node_id": node_id, **kw}, scope)

    def edge(self, etype, a, b, attrs=None, scope="org", nodes=True, **kw):
        for nid in (a, b):          # nodes the edge needs must be defined to be visible
            if nodes and nid not in self.known and "seq" not in kw:
                self.node(nid, scope=scope)
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
            g.apply({**e, "payload": {k: v for k, v in e["payload"].items() if k != "_envelope"}})
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


def test_remove_before_add_out_of_order_stays_removed():
    repo, svc, f = make()
    f.send("edge.remove", {"edge_type": "dependency", "from_id": "task:a", "to_id": "task:b", "reason": "r"}, seq=5)
    f.node("task:a")
    f.node("task:b")
    f.edge("dependency", "task:a", "task:b", seq=2)
    assert repo.graph.neighborhood(OPERATOR, "task:a")["edges"] == []


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
    assert repo.graph.snapshot() == {"nodes": {}, "edges": {}}
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
    f.edge("dependency", "task:hub", "task:far2", scope="org", nodes=False)  # endpoint never upserted: stays invisible


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
    assert not can_see(VIEWER, "private", frozenset({"key:view", "key:other"}))
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
