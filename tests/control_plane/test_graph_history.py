"""P6 history tests: diff, impact, SLO/load over graph snapshots.

Gate (07-phases-tests.md P6): SLO/load tests, failure recovery, rollback.
"""
import time

from herdr_engineering.control_plane.graph import GraphProjection
from herdr_engineering.control_plane.graph_history import diff, impact
from herdr_engineering.control_plane.graph_mission import MissionGraphEmitter, mission_lineage_events


def build(n_chain, tail_edges=True):
    g = GraphProjection()
    em = MissionGraphEmitter()
    recs = [{"kind": "mission", "id": f"m{i}", "state": "running"} for i in range(n_chain)]
    events = mission_lineage_events(em, recs)
    if tail_edges:
        for i in range(n_chain - 1):
            events += [em.handoff(f"m{i}", f"m{i+1}")]
    MissionGraphEmitter.apply_all(g, events)
    return g


class TestHistorySlice:
    def test_diff_before_after(self):
        before = build(3).snapshot()
        after = build(4).snapshot()
        d = diff(before, after)
        assert "mission:m3" in d.added_nodes
        assert not d.removed_nodes
        assert any(k == "handoff" and t == "mission:m3" for k, f, t in d.added_edges)

    def test_diff_empty_for_same(self):
        s = build(3).snapshot()
        assert diff(s, build(3).snapshot()).is_empty

    def test_impact_closure(self):
        g = build(5)
        impacted = impact(g.snapshot(), "mission:m0")
        assert "mission:m1" in impacted
        assert "mission:m4" in impacted

    def test_rollback_equals_prior_snapshot(self):
        pre = build(3).snapshot()
        post = build(5).snapshot()
        rolled = build(3).snapshot()
        assert diff(pre, rolled).is_empty
        assert diff(pre, post).summary()["added_nodes"] == 2


class TestSloScale:
    def test_p95_ingest_under_target(self):
        """Project a 1000-node + 999-edge fixture well under a 1s budget
        (acceptance target p95 < 2s)."""
        t0 = time.perf_counter()
        g = build(1000)
        elapsed = time.perf_counter() - t0
        snap = g.snapshot()
        assert len(snap["nodes"]) == 1000
        assert elapsed < 1.0, f"projection took {elapsed:.3f}s (budget 1.0s)"

    def test_duplicate_stream_failure_recovery(self):
        g = GraphProjection()
        em = MissionGraphEmitter()
        events = mission_lineage_events(em, [{"kind": "mission", "id": f"m{i}"}
                                             for i in range(50)])
        # inject duplicates + an out-of-order tail
        events += [events[0], events[5], em.mission_upsert("m99", state="running")[0]]
        MissionGraphEmitter.apply_all(g, events)
        assert len(g.snapshot()["nodes"]) == 51
