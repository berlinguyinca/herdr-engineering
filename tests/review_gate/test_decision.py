from herdr_engineering.review_gate.decision import decide_merge
from herdr_engineering.review_gate.models import CheckRun, Verdict

HEAD = "abc123"
REQ = ["ci"]


def _v(verdict="PASS", sha=HEAD):
    return Verdict(verdict=verdict, sha=sha, findings=(), reviewer_model="m")


def _c(status="pass", sha=HEAD, name="ci"):
    return CheckRun(name=name, status=status, sha=sha)


def _decide(checks, verdict):
    return decide_merge(head_sha=HEAD, checks=checks, required_checks=REQ, verdict=verdict)


def test_merge_when_all_required_pass_and_review_passes():
    d = _decide([_c()], _v())
    assert d.decision == "merge"
    assert d.reasons == ()


def test_reject_on_review_fail():
    d = _decide([_c()], _v("FAIL"))
    assert d.decision == "reject"
    assert d.reasons == ("review FAIL",)


def test_reject_when_check_sha_differs_from_head():
    d = _decide([_c(sha="old")], _v())
    assert d.decision == "reject"
    assert d.reasons == (f"check ci sha old != head {HEAD}",)


def test_reject_when_review_sha_differs_from_head():
    d = _decide([_c()], _v(sha="old"))
    assert d.decision == "reject"
    assert d.reasons == (f"review sha old != head {HEAD}",)


def test_wait_when_required_check_missing():
    d = _decide([], _v())
    assert d.decision == "wait"
    assert d.reasons == ("required check ci missing",)


def test_wait_when_required_check_pending():
    d = _decide([_c("pending")], _v())
    assert d.decision == "wait"
    assert d.reasons == ("required check ci pending",)


def test_wait_when_no_verdict():
    d = _decide([_c()], None)
    assert d.decision == "wait"
    assert d.reasons == ("no review",)


def test_skipped_required_check_is_not_a_pass():
    d = _decide([_c("skipped")], _v())
    assert d.decision == "wait"
    assert d.reasons == ("required check ci skipped",)


def test_rejects_collected_in_fixed_order_and_before_waits():
    d = _decide([_c("fail", sha="old")], _v("FAIL", sha="old2"))
    assert d.decision == "reject"
    assert d.reasons == (
        f"review sha old2 != head {HEAD}",
        f"check ci sha old != head {HEAD}",
        "review FAIL",
        "check ci failed",
    )
