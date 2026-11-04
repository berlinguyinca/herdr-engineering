"""Spec 10.4 over the EXISTING pieces: CI failure -> repair commit -> fresh independent re-review -> merge gate.

No new gate is introduced. The gate under test is what already decides merges: the head-bound review quorum
(``review_quorum.pr_review_quorum``, shared vectors with AutoSpec) for the review half and
``review_gate.decision.decide_merge`` for the check half, fed from graph ``validated`` edges. The test proves, in order:
a review bound to head A is stale after head B; a CI failure on B blocks; the repair commit C needs a fresh independent
review (a reviewer who is the PR author or its producer never counts); only then does the gate allow.
"""
import hashlib
import json
from pathlib import Path

from herdr_engineering.review_gate.decision import decide_merge
from herdr_engineering.review_gate.models import CheckRun, Verdict
from tests.control_plane.test_graph import OPERATOR
from tests.control_plane.test_review_quorum import HUMAN, PI, PR, R1, R2, R3, World, sha

FIXTURES = Path(__file__).parent / "fixtures"
# sha256 of autospec crates/autospec-core/tests/fixtures/review-quorum-vectors.json at origin/main when this copy was made
SHARED_VECTORS_SHA256 = "18d82766de6a382e6c83ef4cd4e1e42bfba5d2f4fb30e9865f988e6f335c6f39"


def ci_result(w, run, commit, conclusion):
    """A CI run is a `ci:` node plus a `validated` edge to the PR bound to the exact commit it ran on."""
    w.feed.node(run, principal=PI, attrs={"commit": commit}, state="passed" if conclusion == "pass" else "failed")
    w.feed.edge("validated", run, PR, {"commit": commit, "conclusion": conclusion}, principal=PI, scope="org")


def checks_at(w, head):
    """CheckRuns for ``head`` from the graph's non-stale `validated` edges (what a gate adapter would read)."""
    edges = w.repo.graph.neighborhood(OPERATOR, PR, depth=1)["edges"]
    return [CheckRun(name="ci", status=e["attrs"]["conclusion"], sha=e["attrs"]["commit"])
            for e in edges if e["edge_type"] == "validated" and not e["stale"] and e["attrs"]["commit"] == head]


def gate(w, head, config=None):
    quorum = w.q(config)
    verdict = Verdict("PASS", head, (), "review-quorum") if quorum["status"] == "satisfied" and not quorum["blocking"] \
        else None
    return decide_merge(head_sha=head, checks=checks_at(w, head), required_checks=["ci"], verdict=verdict), quorum


def reviewed_edges(w):
    return {(e["attrs"]["commit"], e["from_id"]): e["stale"]
            for e in w.repo.graph.neighborhood(OPERATOR, PR, depth=1)["edges"] if e["edge_type"] == "reviewed"}


def test_ci_failure_repair_rereview_then_merge():
    w = World()
    w.feed.node(PR, label="PR 7", principal=PI, attrs={"author": "Author-Login"})
    A, B, C = sha(1), sha(2), sha(3)
    w.feed.edge("produced", "attempt:impl-1", PR, {"commit": A}, principal=PI)        # the implementer is key:pi

    # head A: two independent reviewers and green CI -> the gate allows
    w.head(A)
    w.review(R1, "review:a1", A)
    w.review(R2, "review:a2", A)
    ci_result(w, "ci:run-1", A, "pass")
    decision, quorum = gate(w, A)
    assert quorum["status"] == "satisfied" and decision.decision == "merge", decision

    # head B (a new push): reviews bound to A are stale; the quorum no longer holds; the old green CI is stale
    w.head(B)
    quorum = w.q()
    assert quorum["status"] == "insufficient" and quorum["counted"] == []
    assert quorum["stale_reviewers"] == ["key:r1", "key:r2"] and quorum["rereview_required"]
    ci_result(w, "ci:run-2", B, "fail")                              # CI fails on B
    w.review(R1, "review:b1", B)
    w.review(R2, "review:b2", B)                                     # even a fresh quorum on B...
    decision, quorum = gate(w, B)
    assert quorum["status"] == "satisfied"
    assert decision.decision == "reject" and "check ci failed" in decision.reasons    # ...cannot override a red CI
    assert checks_at(w, A) == []                                     # run-1's pass is stale for A once B exists

    # repair commit C: everything bound to B is stale again; the repair author cannot approve its own repair
    w.head(C)
    w.feed.edge("produced", "attempt:repair-1", PR, {"commit": C}, principal=PI)      # the repair agent produced C
    decision, quorum = gate(w, C)
    assert quorum["status"] == "insufficient" and quorum["counted"] == [] and decision.decision == "wait"
    assert "no review" in decision.reasons
    w.review(PI, "review:self", C)                                   # the producer / repair agent reviewing itself
    w.review(HUMAN, "review:author", C)                              # the PR author's login reviewing it
    decision, quorum = gate(w, C)
    assert quorum["counted"] == [] and decision.decision == "wait", quorum
    rejected = {(r["reviewer"], r["reason"]) for r in quorum["rejected"]}
    assert {("key:pi", "author"), ("key:human", "author")} <= rejected
    assert {("key:r1", "stale_head"), ("key:r2", "stale_head")} <= rejected       # B-bound reviews are not reused

    # CI green on C but still no independent review: wait, never merge
    ci_result(w, "ci:run-3", C, "pass")
    decision, _ = gate(w, C)
    assert decision.decision == "wait"

    # fresh independent reviews at C (not the earlier reviewers' B-bound ones): now, and only now, the gate allows
    w.review(R1, "review:c1", C)
    decision, quorum = gate(w, C)
    assert quorum["status"] == "insufficient" and decision.decision == "wait"
    w.review(R3, "review:c3", C)
    decision, quorum = gate(w, C)
    assert quorum["counted"] == ["key:r1", "key:r3"] and decision.decision == "merge", (decision, quorum)

    # the graph keeps the whole story, flagged: reviews of A and B are kept but stale, only C's are current
    flags = reviewed_edges(w)
    assert flags[(A, "review:a1")] and flags[(B, "review:b1")]
    assert not flags[(C, "review:c1")] and not flags[(C, "review:c3")]


def test_rebuild_from_stored_events_gives_the_same_gate_answer():
    w = World()
    w.feed.node(PR, label="PR 7", principal=PI, attrs={"author": "Author-Login"})
    A, B = sha(1), sha(2)
    w.head(A)
    w.review(R1, "review:a1", A)
    w.review(R2, "review:a2", A)
    ci_result(w, "ci:run-1", A, "pass")
    w.head(B)
    ci_result(w, "ci:run-2", B, "fail")
    before = gate(w, B)
    w.repo._rebuild_graph()
    after = gate(w, B)
    assert (before[0], before[1]["status"]) == (after[0], after[1]["status"]) and after[0].decision == "reject"


def test_a_late_review_for_an_old_head_cannot_unblock_a_new_one():
    w = World()
    w.feed.node(PR, label="PR 7", principal=PI, attrs={"author": "Author-Login"})
    A, B = sha(1), sha(2)
    w.head(A)
    w.head(B)
    ci_result(w, "ci:run-2", B, "pass")
    w.review(R1, "review:late1", A)                                  # issued for A, lands after B
    w.review(R2, "review:late2", A)
    decision, quorum = gate(w, B)
    assert quorum["counted"] == [] and decision.decision == "wait"


def test_shared_vectors_copy_is_byte_identical_to_autospec_and_pins_the_rules_this_scenario_uses():
    raw = (FIXTURES / "review-quorum-vectors.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHARED_VECTORS_SHA256
    cases = {c["name"]: c for c in json.loads(raw)["cases"]}
    for name in ("older_head_never_counts", "author_login_excluded", "block_at_head_blocks"):
        assert name in cases and "herdr" in cases[name]["applies"] and "rust" in cases[name]["applies"]
    assert cases["older_head_never_counts"]["expect"]["status"] != "satisfied"
    assert cases["author_login_excluded"]["expect"]["status"] != "satisfied"

