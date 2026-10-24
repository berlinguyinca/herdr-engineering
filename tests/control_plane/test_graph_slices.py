"""P2-P4 slice tests: mission/repo/trace emitters over the existing GraphProjection.

Verifies the emitters produce lineage events that the existing lineage graph
applies idempotently, and that the spec's mapping (mission/session/handoff,
repository/dependency, request/span/calls/observed_in) lands in the projection.
"""
import json
import subprocess
import sys

from herdr_engineering.control_plane.graph import GraphProjection
from herdr_engineering.control_plane.graph_mission import MissionGraphEmitter, mission_lineage_events
from herdr_engineering.control_plane.graph_repo import (
    RepositoryGraphEmitter, parse_dependencies, repo_lineage_events)
from herdr_engineering.control_plane.graph_trace import (
    TraceGraphEmitter, from_inferweave, trace_lineage_events)


def build_mission_graph():
    g = GraphProjection()
    em = MissionGraphEmitter()
    events = mission_lineage_events(em, [
        {"kind": "mission", "id": "m1", "state": "running", "stage": "plan", "progress": 0.4},
        {"kind": "mission", "id": "m2", "state": "queued"},
        {"kind": "session", "id": "s1", "mission_id": "m1", "state": "active"},
    ])
    events.append(em.handoff("m1", "m2"))
    MissionGraphEmitter.apply_all(g, events)
    return g


class TestMissionSlice:
    def test_mission_projection(self):
        g = build_mission_graph()
        snap = g.snapshot()
        assert "mission:m1" in snap["nodes"]
        assert snap["nodes"]["mission:m1"]["state"] == "running"
        assert snap["nodes"]["mission:m1"]["attrs"]["stage"] == "plan"
        assert "mission:m2" in snap["nodes"]
        assert "session:s1" in snap["nodes"]

    def test_handoff_and_spawn_edges(self):
        g = build_mission_graph()
        snap = g.snapshot()
        triples = {(e.get("edge_type"), e.get("from_id"), e.get("to_id"))
                   for e in snap["edges"].values()}
        assert ("handoff", "mission:m1", "mission:m2") in triples
        assert ("spawn", "mission:m1", "session:s1") in triples

    def test_offline_reconnect_replay(self):
        """Replaying the same events onto a fresh projection converges."""
        g1 = build_mission_graph()
        events = mission_lineage_events(MissionGraphEmitter(), [
            {"kind": "mission", "id": "m1", "state": "running"},
            {"kind": "mission", "id": "m2"},
            {"kind": "session", "id": "s1", "mission_id": "m1"},
        ])
        events.append(MissionGraphEmitter().handoff("m1", "m2"))
        g2 = GraphProjection()
        MissionGraphEmitter.apply_all(g2, events)
        assert set(g1.snapshot()["nodes"]) == set(g2.snapshot()["nodes"])
        assert len(g1.snapshot()["edges"]) == len(g2.snapshot()["edges"])

    def test_blocked_by_edge(self):
        g = GraphProjection()
        em = MissionGraphEmitter()
        MissionGraphEmitter.apply_all(g, em.mission_upsert("m1", state="blocked", blocked_by="m2"))
        MissionGraphEmitter.apply_all(g, em.mission_upsert("m2", state="running"))
        triples = {(e.get("edge_type"), e.get("from_id"), e.get("to_id"))
                   for e in g.snapshot()["edges"].values()}
        assert ("blocked_by", "mission:m1", "mission:m2") in triples


class TestRepoSlice:
    def test_dep_parsing(self):
        assert parse_dependencies({"project": {"dependencies": ["click>=8.0", "flask==2.3"]}},
                                  "pyproject") == ["click", "flask"]
        assert parse_dependencies({"dependencies": {"lodash": "^4.17", "react": "18"}},
                                  "package.json") == ["lodash", "react"]
        assert parse_dependencies({"dependencies": {"serde": "1", "tokio": "1"}},
                                  "Cargo.toml") == ["serde", "tokio"]

    def test_pyproject_optional_dependencies_mapping(self):
        """optional-dependencies is a MAPPING of extra -> dep list (PEP 621)."""
        manifest = {"project": {
            "dependencies": ["requests[socks]>=2.0", "foo; python_version >= '3.10'"],
            "optional-dependencies": {
                "dev": ["pytest>=8.0", "black"],
                "test": ["coverage"],
            },
        }}
        deps = parse_dependencies(manifest, "pyproject")
        assert "requests" in deps and "foo" in deps
        assert "pytest" in deps and "black" in deps and "coverage" in deps
        # the extra NAME "dev"/"test" must never be reported as a dependency
        assert "dev" not in deps and "test" not in deps

    def test_repo_graph(self):
        g = GraphProjection()
        em = RepositoryGraphEmitter()
        RepositoryGraphEmitter.apply_all(g, repo_lineage_events(em, [
            {"name": "acme/api", "depends_on": ["acme/lib"]},
            {"name": "acme/lib", "depends_on": ["acme/core"]},
            {"name": "acme/core"},
        ]))
        snap = g.snapshot()
        assert "repository:acme/api" in snap["nodes"]
        assert snap["nodes"]["repository:acme/api"]["label"] == "acme/api"
        triples = {(e.get("edge_type"), e.get("from_id"), e.get("to_id"))
                   for e in snap["edges"].values()}
        assert ("dependency", "repository:acme/api", "repository:acme/lib") in triples
        assert ("dependency", "repository:acme/lib", "repository:acme/core") in triples

    def test_supersession_rename(self):
        g = GraphProjection()
        em = RepositoryGraphEmitter()
        RepositoryGraphEmitter.apply_all(g, em.repo("acme/api-v2", supersedes="acme/api"))
        RepositoryGraphEmitter.apply_all(g, em.repo("acme/api"))
        triples = {(e.get("edge_type"), e.get("from_id"), e.get("to_id"))
                   for e in g.snapshot()["edges"].values()}
        assert ("supersession", "repository:acme/api-v2", "repository:acme/api") in triples


class TestTraceSlice:
    def test_request_and_span_mapping(self):
        g = GraphProjection()
        em = TraceGraphEmitter()
        rows = [
            {"request_id": "r1", "model": "flint-40b", "status": "ok", "duration_ms": 120},
            {"request_id": "r1", "span_id": "s1", "op": "tool_calls",
             "duration_ms": 80, "parent_span_id": None, "sampled": True},
            {"request_id": "r1", "span_id": "s2", "op": "content",
             "duration_ms": 40, "parent_span_id": "s1", "sampled": True},
        ]
        events = trace_lineage_events(em, [from_inferweave(r) for r in rows])
        TraceGraphEmitter.apply_all(g, events)
        snap = g.snapshot()
        assert "request:r1" in snap["nodes"]
        assert "span:s1" in snap["nodes"]
        assert "span:s2" in snap["nodes"]
        triples = {(e.get("edge_type"), e.get("from_id"), e.get("to_id"))
                   for e in snap["edges"].values()}
        assert ("observed_in", "span:s1", "request:r1") in triples
        # parent span -> calls edge; orphan span gets none (honesty)
        assert ("calls", "span:s1", "span:s2") in triples
        assert not any(t[0] == "calls" and t[2] == "span:s1" for t in triples)

    def test_sampled_flag_surfaced_not_fabricated(self):
        g = GraphProjection()
        em = TraceGraphEmitter()
        TraceGraphEmitter.apply_all(g, em.request("r9", status="sampled_out", sampled=False))
        assert g.snapshot()["nodes"]["request:r9"]["attrs"]["sampled"] is False

    def test_orphan_span_never_fabricates_request_none(self):
        """A span with no request_id must not create a request:None node."""
        g = GraphProjection()
        em = TraceGraphEmitter()
        TraceGraphEmitter.apply_all(g, em.span("s0", request_id=None, op="tool_calls"))
        snap = g.snapshot()
        assert "request:None" not in snap["nodes"]
        assert snap["nodes"]["span:s0"]["attrs"]["orphan"] is True
        # no observed_in edge to a fabricated request
        assert not any(
            e.get("edge_type") == "observed_in" and e.get("to_id") == "request:None"
            for e in snap["edges"].values())


class TestArtifactCli:
    def test_cli_render_command(self):
        """graph-artifact render produces a deterministic file from a snapshot."""
        snapshot = {"nodes": {"mission:m1": {"node_type": "mission", "state": "running",
                                             "owner": "k", "attrs": {"stage": "plan"}}},
                    "edges": {}}
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"title": "Test", **snapshot}, f)
            path = f.name
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "herdr_engineering.cli",
                 "graph-artifact", "render", path, "--out", path + ".html"],
                capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
            out = json.loads(proc.stdout)
            assert out["ok"] is True and out["nodes"] == 1
            assert os.path.exists(path + ".html")
        finally:
            for p in (path, path + ".html"):
                try:
                    os.unlink(p)
                except OSError:
                    pass
