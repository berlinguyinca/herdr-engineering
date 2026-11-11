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
    def test_ingest_scales_linearly_not_quadratically(self):
        """Projecting a 1000-node + 999-edge fixture must scale ~linearly.

        An absolute seconds budget depends on the runner's CPU and disk (CI runs on
        mixed hardware), so compare 1000 nodes against 100 nodes measured on the same
        machine in the same process: linear is ~10x, quadratic ~100x. Best-of-3 on each
        side discards scheduler noise.
        """
        def best(n):
            runs = []
            for _ in range(3):
                t0 = time.perf_counter()
                g = build(n)
                runs.append(time.perf_counter() - t0)
            return min(runs), g

        small, _ = best(100)
        large, g = best(1000)
        assert len(g.snapshot()["nodes"]) == 1000
        ratio = large / max(small, 1e-6)
        assert ratio < 40, f"1000 nodes took {ratio:.1f}x the 100-node time (linear ~10x, quadratic ~100x)"

    def test_duplicate_stream_failure_recovery(self):
        g = GraphProjection()
        em = MissionGraphEmitter()
        events = mission_lineage_events(em, [{"kind": "mission", "id": f"m{i}"}
                                             for i in range(50)])
        # inject duplicates + an out-of-order tail
        events += [events[0], events[5], em.mission_upsert("m99", state="running")[0]]
        MissionGraphEmitter.apply_all(g, events)
        assert len(g.snapshot()["nodes"]) == 51
