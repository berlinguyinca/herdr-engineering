from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.models import Finding, PrKey, State, Verdict
from herdr_engineering.review_gate.store import PipelineStore

KEY = PrKey.parse("github.com/acme/widgets#7")


def make(**kw):
    repo = ControlPlaneRepo()
    return repo, PipelineStore(repo, **kw)


def finding(msg="missing null check", line=10):
    return Finding(
        path="a.py", category="bug", message=msg, severity="high", required_change="fix", line=line
    )


def fail(sha, *findings):
    return Verdict("FAIL", sha, tuple(findings) or (finding(),), "m")


def to_reviewing(store, sha):
    assert store.on_push(KEY, sha)
    assert store.on_ci_green(KEY, sha)
    assert store.on_review_started(KEY, sha, "m")


def test_push_then_green_then_review_then_pass_reaches_approved():
    _, s = make()
    assert s.snapshot(KEY).state == State.CI_PENDING
    assert s.snapshot(KEY).head_sha is None
    to_reviewing(s, "A")
    assert s.snapshot(KEY).state == State.REVIEWING
    assert s.on_verdict(KEY, Verdict("PASS", "A", (), "m"), patch_id="p1")
    snap = s.snapshot(KEY)
    assert snap.state == State.APPROVED
    assert snap.approved_sha == "A" and snap.approved_patch_id == "p1"
    assert s.on_merged(KEY, "A")
    assert s.snapshot(KEY).state == State.MERGED


def test_verdict_for_old_sha_is_ignored():
    repo, s = make()
    s.on_push(KEY, "A")
    s.on_push(KEY, "B")
    n = len(repo.stream("pr_gate", str(KEY)))
    assert s.on_verdict(KEY, fail("A")) is False
    assert s.snapshot(KEY).state == State.CI_PENDING
    assert len(repo.stream("pr_gate", str(KEY))) == n


def test_push_during_review_supersedes():
    _, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A"))
    to_reviewing_b = s.on_push(KEY, "B")
    assert to_reviewing_b
    s.on_ci_green(KEY, "B")
    s.on_review_started(KEY, "B", "m")
    rounds = s.snapshot(KEY).review_rounds
    assert s.on_push(KEY, "C")
    snap = s.snapshot(KEY)
    assert snap.state == State.CI_PENDING and snap.head_sha == "C"
    assert snap.review_rounds == rounds == 1


def test_ci_red_does_not_consume_review_round():
    _, s = make()
    s.on_push(KEY, "A")
    assert s.on_ci_red(KEY, "A", ["lint"])
    snap = s.snapshot(KEY)
    assert snap.state == State.CI_RED
    assert snap.review_rounds == 0 and snap.ci_fix_rounds == 1


def test_ci_red_three_times_escalates():
    _, s = make()
    for sha in ("A", "B", "C"):
        s.on_push(KEY, sha)
        assert s.on_ci_red(KEY, sha, ["t"])
    snap = s.snapshot(KEY)
    assert snap.state == State.ESCALATED and snap.ci_fix_rounds == 3


def test_fourth_failing_verdict_escalates():
    _, s = make()
    for i, sha in enumerate("ABCD"):
        to_reviewing(s, sha)
        assert s.on_verdict(KEY, fail(sha, finding(f"issue kind{'x' * i}")))
        if i < 3:
            assert s.snapshot(KEY).state == State.CHANGES_REQUESTED
    snap = s.snapshot(KEY)
    assert snap.state == State.ESCALATED and snap.review_rounds == 4


def test_repeated_fingerprint_escalates_with_moved_line():
    _, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A", finding(line=10)))
    to_reviewing(s, "B")
    s.on_verdict(KEY, fail("B", finding(line=42)))
    snap = s.snapshot(KEY)
    assert snap.state == State.ESCALATED and snap.review_rounds == 2


def test_resume_clears_escalation_and_counters():
    _, s = make()
    s.on_push(KEY, "A")
    s.on_ci_red(KEY, "A", ["x"])
    assert s.resume(KEY) is False  # not escalated
    for sha in ("B", "C"):
        s.on_push(KEY, sha)
        s.on_ci_red(KEY, sha, ["x"])
    assert s.snapshot(KEY).state == State.ESCALATED
    assert s.resume(KEY)
    snap = s.snapshot(KEY)
    assert snap.state == State.CI_PENDING
    assert snap.ci_fix_rounds == 0 and snap.review_rounds == 0
    assert snap.last_findings == ()


def _approved(s):
    to_reviewing(s, "A")
    s.on_verdict(KEY, Verdict("PASS", "A", (), "m"), patch_id="p1")


def test_carry_over_same_patch_id_reapproves_new_head():
    _, s = make()
    _approved(s)
    s.on_push(KEY, "B")
    assert s.carry_over_approval(KEY, "B", "p1")
    snap = s.snapshot(KEY)
    assert snap.state == State.APPROVED and snap.approved_sha == "B"


def test_carry_over_different_patch_id_does_not():
    _, s = make()
    _approved(s)
    s.on_push(KEY, "B")
    assert s.carry_over_approval(KEY, "B", "p2") is False
    assert s.snapshot(KEY).state == State.CI_PENDING


def test_finding_text_is_redacted():
    repo, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A", Finding("a.py", "sec", "leak sk-" + "a" * 30, "high", "rotate")))
    assert "sk-" + "a" * 30 not in str(repo.stream("pr_gate", str(KEY)))


def test_snapshot_rebuilds_from_events_after_restart():
    repo, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A"))
    s.on_push(KEY, "B")
    assert PipelineStore(repo).snapshot(KEY) == s.snapshot(KEY)
