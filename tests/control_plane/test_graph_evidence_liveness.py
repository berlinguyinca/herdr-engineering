"""Liveness of the evidence writer (review of PR #409). Everything here failed closed before (the gate waited, never
passed) but could wedge a PR: an unrelated key flooding the PR with edges, a pre-created run node, a lost edge."""
import logging

import pytest

from herdr_engineering.control_plane.graph_evidence_writer import GraphEvidenceWriter
from herdr_engineering.control_plane.graph_merge_gate import READER, decide_from_graph, validate_section
from herdr_engineering.review_gate.config import RepoEnrollment
from tests.control_plane.test_graph_evidence_writer import GATE, PR_NODE, REPO, WRITER, A, B, Rig, ci_row, pr

STRANGER = {"kind": "agent", "id": "key:deadbeef0000", "authenticated_by": "api_key"}
OPERATOR = {"kind": "operator", "id": "key:op0000000000", "authenticated_by": "api_key"}
WRITER_PRINCIPAL = {"kind": "agent", "id": WRITER, "authenticated_by": "evidence_writer"}


def ev(src, seq, et, eid, payload):
    return {"schema_version": 2, "event_id": f"{src}-{seq}", "source": src, "source_sequence": seq,
            "entity_type": "lineage", "entity_id": eid, "event_type": et, "payload": payload, "visibility_scope": "org"}


def forge(r, nid, actor, src="forge", commit=A, check="ci", conclusion="success"):
    r.ingest.ingest(ev(src, 1, "node.upsert", nid, {"node_type": "ci", "node_id": nid}), actor)
    r.ingest.ingest(ev(src, 2, "edge.add", nid, {"edge_type": "validated", "from_id": nid, "to_id": PR_NODE,
                                                  "attrs": {"commit": commit, "check": check,
                                                            "conclusion": conclusion}}), actor)


def run_edges(r, include_removed=False):
    return r.cp.graph.edges_into_by_writers(READER, PR_NODE, "validated", {WRITER}, include_removed=include_removed)


def test_a_flood_of_validated_edges_from_other_keys_cannot_hide_the_writers_pass():
    r = Rig(GATE)
    r.push(A)
    for i in range(300):
        forge(r, f"ci:flood{i}", dict(STRANGER, id="key:%012x" % i), src=f"fl{i % 3}x{i}")
    checks, notes = r.checks(A)
    assert [(c.name, c.status, c.sha) for c in checks] == [("ci", "pass", A)]
    assert notes["untrusted"] > 0                              # the junk is still counted, and still ignored
    approve_and_merge(r)


def approve_and_merge(r):
    from tests.review_gate.test_service import KEY, approve
    approve(r.store, KEY, A)
    r.svc.tick()
    assert len(r.merged) == 1 and r.merged[0][3] == A


def test_the_writers_fail_is_also_seen_under_a_flood():
    r = Rig(GATE)
    r.push(A, state="failure")
    for i in range(300):
        forge(r, f"ci:flood{i}", dict(STRANGER, id="key:%012x" % i), src=f"fl{i}")
    decision, _ = decide_from_graph(r.cp.graph, PR_NODE, A, validate_section(GATE))
    assert decision.decision == "reject"


def test_forged_validated_edges_from_non_writers_are_still_ignored():
    r = Rig(GATE)
    r.push(B)                                                  # the PR node exists; the writer vouched for B only
    forge(r, "ci:forged", STRANGER)                            # a stranger vouches for A
    forge(r, "ci:forgedop", OPERATOR, src="forgeop")           # even an operator key is not a listed writer
    checks, notes = r.checks(A)
    assert checks == [] and notes["untrusted"] == 2
    decision, _ = decide_from_graph(r.cp.graph, PR_NODE, A, validate_section(GATE))
    assert decision.decision == "wait"


def test_a_stranger_cannot_forge_into_the_writers_namespace_or_pose_as_it():
    r = Rig(GATE)
    r.push(A)
    run_id = run_edges(r)[0]["from_id"]
    assert run_id.startswith(f"ci:{WRITER}/")
    for actor in (STRANGER, dict(STRANGER, id="key:0123456789ab")):
        with pytest.raises(PermissionError):
            r.ingest.ingest(ev("atk", 1, "node.upsert", run_id, {"node_type": "ci", "node_id": run_id}), actor)
        with pytest.raises(PermissionError):
            r.ingest.ingest(ev("atk2", 1, "edge.add", "ci:other", {"edge_type": "validated", "from_id": "ci:other",
                                                                    "to_id": run_id}), actor)


def test_a_pre_created_run_node_does_not_block_evidence():
    twin = Rig(GATE)
    twin.push(A)
    run_id = run_edges(twin)[0]["from_id"]                     # the (predictable) id the writer will use
    r = Rig(GATE)
    with pytest.raises(PermissionError):                       # the squat is refused ...
        r.ingest.ingest(ev("atk", 1, "node.upsert", run_id, {"node_type": "ci", "node_id": run_id, "label": "x",
                                                              "state": "success", "attrs": {"commit": A}}), STRANGER)
    r.push(A)                                                  # ... and the writer is not affected
    assert r.writer.last_error is None
    assert [(c.status, c.sha) for c in r.checks(A)[0]] == [("pass", A)]


def test_a_pre_created_pr_node_does_not_block_evidence():
    r = Rig(GATE)
    r.ingest.ingest(ev("atk", 1, "node.upsert", PR_NODE, {"node_type": "pr", "node_id": PR_NODE, "label": "x"}), STRANGER)
    r.push(A)
    assert r.writer.last_error is None and [c.status for c in r.checks(A)[0]] == ["pass"]


def remove_edge(r, edge, actor=OPERATOR, src="rm"):
    r.ingest.ingest(ev(src, 1, "edge.remove", edge["from_id"], {
        "edge_type": "validated", "from_id": edge["from_id"], "to_id": PR_NODE, "reason": "operator",
        "attrs": {"commit": A, "writer": WRITER}}), actor)


def test_an_edge_removed_by_an_operator_is_rewritten_on_the_next_tick():
    r = Rig(GATE)
    r.push(A)
    remove_edge(r, run_edges(r)[0])
    assert r.checks(A)[0] == [] and run_edges(r) == []         # the gate is back to waiting
    r.svc.tick()                                               # same CI state: before the fix the memo said "known"
    assert [(c.status, c.sha) for c in r.checks(A)[0]] == [("pass", A)]
    r.svc.tick()
    assert len(run_edges(r)) == 1


def test_repeated_operator_removals_keep_recovering():
    r = Rig(GATE)
    r.push(A)
    for i in range(3):
        remove_edge(r, run_edges(r)[0], src=f"rm{i}")
        r.svc.tick()
        assert [c.status for c in r.checks(A)[0]] == ["pass"], i


def test_a_restarted_writer_also_recovers_a_removed_edge():
    r = Rig(GATE)
    r.push(A)
    remove_edge(r, run_edges(r)[0])
    again = GraphEvidenceWriter(ingest=r.ingest, section_fn=lambda: r.section, graph_fn=lambda: r.cp.graph)
    assert again.observe(RepoEnrollment(repo=REPO), pr(sha=A), [ci_row(A, "success")], False) == "written"
    assert [c.status for c in r.checks(A)[0]] == ["pass"]


def test_an_edge_lost_to_a_graph_rebuild_is_rewritten():
    r = Rig(GATE)
    r.push(A)
    assert len(run_edges(r)) == 1
    # retention / a restore from an older copy: graph AND ledger have forgotten the earlier events
    from herdr_engineering.control_plane.graph import GraphProjection
    fresh = GraphProjection()
    fresh._write_gates, fresh._node_gates = dict(r.cp.graph._write_gates), dict(r.cp.graph._node_gates)
    r.cp.graph = fresh
    r.cp.ledger = type(r.cp.ledger)(now=r.cp._now, persist=lambda *a, **k: None)
    with pytest.raises(KeyError):
        run_edges(r)
    r.svc.tick()
    assert [c.status for c in r.checks(A)[0]] == ["pass"]


def test_the_memo_still_avoids_rewriting_while_the_edge_is_present():
    r = Rig(GATE)
    r.push(A)
    calls = []
    real = r.ingest.ingest
    r.ingest.ingest = lambda *a, **k: calls.append(1) or real(*a, **k)
    assert r.writer.observe(RepoEnrollment(repo=REPO), pr(sha=A), [ci_row(A, "success")], False) == "known"
    assert calls == []


def test_a_persistent_refusal_is_logged_once_not_every_tick(caplog):
    r = Rig(GATE)
    now = [0.0]
    r.writer._clock = lambda: now[0]
    r.ingest.ingest = lambda *a, **k: (_ for _ in ()).throw(PermissionError("refused"))
    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            r.push(A)
            now[0] += 5
    assert len([x for x in caplog.records if "not written" in x.getMessage()]) == 1
    now[0] += 1000                                             # still refused much later: one reminder
    with caplog.at_level(logging.WARNING):
        r.push(A)
    assert len([x for x in caplog.records if "not written" in x.getMessage()]) == 2
    assert r.writer.last_error.startswith("PermissionError")


def test_a_different_error_is_logged_again(caplog):
    r = Rig(GATE)
    errors = iter([PermissionError("one"), ValueError("two")])
    r.ingest.ingest = lambda *a, **k: (_ for _ in ()).throw(next(errors))
    with caplog.at_level(logging.WARNING):
        r.push(A)
        r.push(B)
    assert len([x for x in caplog.records if "not written" in x.getMessage()]) == 2


def test_a_real_rebuild_from_the_ledger_keeps_the_edge_and_the_memo_valid():
    r = Rig(GATE)
    r.push(A)
    r.cp._rebuild_graph()
    assert len(run_edges(r)) == 1
    assert r.writer.observe(RepoEnrollment(repo=REPO), pr(sha=A), [ci_row(A, "success")], False) == "known"
