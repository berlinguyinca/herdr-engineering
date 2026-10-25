from herdr_engineering.review_gate.config import RepoEnrollment
from herdr_engineering.review_gate.trigger import CiVerdict, ci_verdict_for

SHA = "a" * 40
ENR = RepoEnrollment(repo="o/r")


def row(n, state, *, sha=SHA, event="pull_request"):
    return {"revision": sha, "state": state, "event": event, "run_number": n, "finished_at": None}


def verdict(rows, enr=ENR, stale=False):
    return ci_verdict_for(SHA, rows, enr, snapshot_stale=stale)


def test_no_matching_rows_is_pending():
    assert verdict([]) == CiVerdict("pending", ())


def test_running_is_pending():
    assert verdict([row(1, "running")]).result == "pending"
    assert verdict([row(1, "pending")]).result == "pending"


def test_all_success_is_green():
    assert verdict([row(1, "success")]) == CiVerdict("green", ())


def test_one_failure_is_red_with_failures_listed():
    v = verdict(
        [row(7, "failure", event="push"), row(3, "killed"), row(5, "success", event="deployment")],
        RepoEnrollment(repo="o/r", required_events=("pull_request", "push", "deployment")),
    )
    assert v == CiVerdict("red", ("run 3 killed", "run 7 failure"))


def test_failure_beats_running():
    enr = RepoEnrollment(repo="o/r", required_events=("pull_request", "push"))
    v = verdict([row(2, "running", event="push"), row(1, "error")], enr)
    assert v == CiVerdict("red", ("run 1 error",))


def test_other_sha_rows_ignored():
    assert verdict([row(1, "failure", sha="b" * 40), row(2, "success")]).result == "green"


def test_non_required_event_ignored():
    assert verdict([row(1, "failure", event="push"), row(2, "success")]).result == "green"
    assert verdict([row(1, "success", event="push")]).result == "pending"


def test_stale_snapshot_is_never_green():
    assert verdict([row(1, "success")], stale=True) == CiVerdict("pending", ())
    assert verdict([row(1, "failure")], stale=True).result == "pending"


def test_cancelled_is_pending():
    for s in ("canceled", "cancelled", "skipped", "declined"):
        assert verdict([row(1, s)]).result == "pending"
    assert (
        verdict(
            [row(1, "canceled"), row(2, "success", event="push")],
            RepoEnrollment(repo="o/r", required_events=("pull_request", "push")),
        ).result
        == "pending"
    )


def test_retry_success_supersedes_earlier_failure():
    assert verdict([row(1, "failure"), row(2, "success")]) == CiVerdict("green", ())
    assert verdict([row(2, "success"), row(1, "failure")]) == CiVerdict("green", ())


def test_later_failure_after_success_is_red():
    assert verdict([row(1, "success"), row(2, "failure")]) == CiVerdict("red", ("run 2 failure",))


def test_unknown_state_is_pending():
    assert verdict([row(1, "weird")]).result == "pending"


TWO = RepoEnrollment(repo="o/r", required_events=("pull_request", "push"))


def test_missing_required_event_is_pending():
    assert verdict([row(1, "success", event="push")], TWO) == CiVerdict("pending", ())


def test_every_required_event_successful_is_green():
    rows = [row(1, "success", event="push"), row(2, "success")]
    assert verdict(rows, TWO) == CiVerdict("green", ())


def test_red_beats_a_missing_required_event():
    assert verdict([row(4, "failure", event="push")], TWO) == CiVerdict("red", ("run 4 failure",))
