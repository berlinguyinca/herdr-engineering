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
    assert s.on_push(KEY, "B")
    assert s.on_ci_green(KEY, "B")
    assert s.on_review_started(KEY, "B", "m")
    assert s.snapshot(KEY).state == State.REVIEWING
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
    secret = "sk-" + "a" * 30
    assert s.on_verdict(KEY, fail("A", Finding("a.py", "sec", "leak " + secret, "high", "rotate")))
    s.on_push(KEY, "B")
    assert s.on_ci_red(KEY, "B", ["boom " + secret])
    assert secret not in str(repo.stream("pr_gate", str(KEY)))


def test_snapshot_rebuilds_from_events_after_restart():
    repo, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A"))
    s.on_push(KEY, "B")
    assert PipelineStore(repo).snapshot(KEY) == s.snapshot(KEY)


def test_repeat_detection_works_with_redacted_text():
    _, s = make()
    f = Finding("a.py", "sec", "leak sk-" + "b" * 30, "high", "rotate", 3)
    to_reviewing(s, "A")
    assert s.on_verdict(KEY, fail("A", f))
    to_reviewing(s, "B")
    assert s.on_verdict(KEY, fail("B", f))
    assert s.snapshot(KEY).state == State.ESCALATED


def _escalate_ci(s):
    for sha in ("A", "B", "C"):
        s.on_push(KEY, sha)
        s.on_ci_red(KEY, sha, ["x"])
    assert s.snapshot(KEY).state == State.ESCALATED


def test_push_while_escalated_stays_escalated():
    _, s = make()
    _escalate_ci(s)
    reason = s.snapshot(KEY).reason
    assert s.on_push(KEY, "D")
    snap = s.snapshot(KEY)
    assert snap.state == State.ESCALATED and snap.head_sha == "D" and snap.reason == reason


def test_ci_green_ignored_after_push_while_escalated():
    _, s = make()
    _escalate_ci(s)
    s.on_push(KEY, "D")
    assert s.on_ci_green(KEY, "D") is False
    assert s.snapshot(KEY).state == State.ESCALATED


def test_resume_after_push_while_escalated_returns_to_ci_pending_on_new_head():
    _, s = make()
    _escalate_ci(s)
    s.on_push(KEY, "D")
    assert s.resume(KEY)
    snap = s.snapshot(KEY)
    assert snap.state == State.CI_PENDING and snap.head_sha == "D"


def test_illegal_state_rejections():
    _, s = make()
    s.on_push(KEY, "A")
    s.on_ci_red(KEY, "A", ["x"])
    assert s.on_ci_green(KEY, "A") is False  # same-SHA re-run ignored
    assert s.on_verdict(KEY, fail("A")) is False
    assert s.on_merged(KEY, "A") is False
    assert s.snapshot(KEY).state == State.CI_RED


def test_custom_caps_honoured():
    _, s = make(review_cap=1, ci_fix_cap=1)
    s.on_push(KEY, "A")
    s.on_ci_red(KEY, "A", ["x"])
    assert s.snapshot(KEY).state == State.ESCALATED
    s.resume(KEY)
    to_reviewing(s, "B")
    s.on_verdict(KEY, fail("B"))
    assert s.snapshot(KEY).state == State.ESCALATED


def test_ci_fix_rounds_persist_across_pushes():
    _, s = make()
    for sha in ("A", "B"):
        s.on_push(KEY, sha)
        s.on_ci_red(KEY, sha, ["x"])
    snap = s.snapshot(KEY)
    assert snap.ci_fix_rounds == 2 and snap.state == State.CI_RED


def test_resume_after_review_cap_escalation():
    _, s = make(review_cap=1)
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A"))
    assert s.snapshot(KEY).state == State.ESCALATED
    assert s.resume(KEY)
    snap = s.snapshot(KEY)
    assert snap.state == State.CI_PENDING and snap.review_rounds == 0


def test_carry_over_wrong_sha_and_null_patch_id():
    _, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, Verdict("PASS", "A", (), "m"), patch_id=None)
    s.on_push(KEY, "B")
    assert s.carry_over_approval(KEY, "B", "p1") is False
    s2_repo, s2 = make()
    to_reviewing(s2, "A")
    s2.on_verdict(KEY, Verdict("PASS", "A", (), "m"), patch_id="p1")
    s2.on_push(KEY, "B")
    assert s2.carry_over_approval(KEY, "Z", "p1") is False


def test_last_findings_retained_across_push():
    _, s = make()
    to_reviewing(s, "A")
    s.on_verdict(KEY, fail("A"))
    s.on_push(KEY, "B")
    assert len(s.snapshot(KEY).last_findings) == 1


def test_review_started_is_idempotent_from_reviewing_for_current_head():
    repo, s = make()
    to_reviewing(s, "A")
    before = s.snapshot(KEY)
    # A worker died; its lease expired and a new worker claimed the same job.
    assert s.on_review_started(KEY, "A", "m2")
    after = s.snapshot(KEY)
    assert after.state == State.REVIEWING
    assert (after.review_rounds, after.ci_fix_rounds) == (
        before.review_rounds,
        before.ci_fix_rounds,
    )
    assert repo.stream("pr_gate", str(KEY))[-1]["event_type"] == "review_started"
    assert s.on_verdict(KEY, Verdict("PASS", "A", (), "m2"))
    s.on_push(KEY, "B")
    assert s.on_review_started(KEY, "A", "m3") is False  # old head stays refused


def test_known_keys_and_snapshots_enumerate_seen_prs():
    _, store = make()
    other = PrKey.parse("github.com/acme/widgets#3")
    store.on_push(KEY, "a" * 40)
    store.on_push(other, "b" * 40)
    assert store.known_keys() == [other, KEY]
    assert [s.key for s in store.snapshots()] == [other, KEY]


def test_per_repo_cap_overrides_default():
    _, store = make(ci_fix_cap=3, ci_fix_caps={KEY.repo: 1})
    store.on_push(KEY, "a" * 40)
    store.on_ci_red(KEY, "a" * 40, ["lint"])
    assert store.snapshot(KEY).state == State.ESCALATED
    elsewhere = PrKey.parse("github.com/acme/other#1")
    store.on_push(elsewhere, "c" * 40)
    store.on_ci_red(elsewhere, "c" * 40, ["lint"])
    assert store.snapshot(elsewhere).state != State.ESCALATED


def _forge(repo, event_type, payload):
    repo.record_event("pr_gate", str(KEY), event_type, payload)


def test_forged_verdict_outside_reviewing_is_ignored_by_fold():
    repo, s = make()
    s.on_push(KEY, "A")
    s.on_ci_green(KEY, "A")  # CI_GREEN, not REVIEWING
    _forge(repo, "verdict", {"verdict": "PASS", "sha": "A"})
    snap = s.snapshot(KEY)
    assert snap.state == State.CI_GREEN and snap.approved_sha is None


def test_forged_verdict_for_wrong_sha_is_ignored_by_fold():
    repo, s = make()
    to_reviewing(s, "A")
    _forge(repo, "verdict", {"verdict": "PASS", "sha": "B"})
    _forge(repo, "verdict", {"verdict": "PASS"})  # no sha at all: must not raise
    snap = s.snapshot(KEY)
    assert snap.state == State.REVIEWING and snap.approved_sha is None


def test_forged_review_started_from_ci_pending_is_ignored_by_fold():
    repo, s = make()
    s.on_push(KEY, "A")
    _forge(repo, "review_started", {"sha": "A"})
    _forge(repo, "verdict", {"verdict": "PASS", "sha": "A"})
    assert s.snapshot(KEY).state == State.CI_PENDING


def test_forged_resume_outside_escalated_is_ignored_by_fold():
    repo, s = make()
    to_reviewing(s, "A")
    assert s.on_verdict(KEY, fail("A"))
    _forge(repo, "resume", {})
    snap = s.snapshot(KEY)
    assert snap.state == State.CHANGES_REQUESTED and snap.review_rounds == 1


def test_forged_merged_and_ci_green_out_of_order_are_ignored_by_fold():
    repo, s = make()
    s.on_push(KEY, "A")
    _forge(repo, "merged", {"sha": "A"})
    _forge(repo, "ci_green", {"sha": "B"})
    assert s.snapshot(KEY).state == State.CI_PENDING


def test_malformed_forged_events_never_break_the_fold():
    repo, s = make()
    to_reviewing(s, "A")
    _forge(repo, "verdict", {"verdict": "FAIL", "sha": "A", "findings": [{"bogus": 1}]})
    _forge(repo, "push", {})
    assert s.snapshot(KEY).head_sha == "A"
    assert s.snapshots()  # GET /prs must keep working
