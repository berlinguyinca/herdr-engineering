from herdr_engineering.control_plane import repair_policy as rp
from herdr_engineering.control_plane.intel_incidents import IncidentBoard, classify_failure


def test_flaky_when_same_sha_passes_on_rerun():
    c = classify_failure({"log_excerpt": "AssertionError", "attempts": [False, True]})
    assert (c.kind, c.action) == ("flaky", "rerun_then_human")


def test_infra_without_assertion_is_rerun_not_repair():
    c = classify_failure({"log_excerpt": "dial tcp: connection reset by peer", "attempts": [False, False]})
    assert (c.kind, c.action) == ("infra", "rerun")


def test_mixed_signatures_go_to_a_human():
    c = classify_failure({"log_excerpt": "connection reset ... AssertionError: x != y", "changed_paths_overlap": True})
    assert (c.kind, c.action) == ("unknown", "human")


def test_regression_requires_deterministic_failure_in_touched_code():
    base = {"log_excerpt": "FAILED tests/test_a.py::test_x - AssertionError", "attempts": [False, False]}
    assert classify_failure({**base, "changed_paths_overlap": True}).action == "repair"
    assert classify_failure({**base, "changed_paths_overlap": False}).action == "human"
    assert classify_failure({}).kind == "unknown"


def test_unrelated_repeated_failures_are_medium_flaky():
    c = classify_failure({"log_excerpt": "AssertionError", "distinct_commits_failed": 4, "changed_paths_overlap": False})
    assert (c.kind, c.confidence) == ("flaky", "medium")


def _apply(b, eid, kind, state):
    return b.apply(project="p", source_incident="o/r#7", event_id=eid, at="2026-10-28T00:00:00Z", kind=kind, state=state)


def test_ci_fix_events_fold_forward_idempotently_and_never_backwards():
    b = IncidentBoard("v1")
    assert _apply(b, "e1", "ci_fix", "queued").state == "AUTHORIZED"
    assert _apply(b, "e2", "ci_fix", "running").state == "EXECUTING"
    assert _apply(b, "e2", "ci_fix", "running").state == "EXECUTING"          # duplicate attaches
    assert _apply(b, "e0", "ci_fix", "queued").state == "EXECUTING"           # late/stale: no regression
    assert _apply(b, "e3", "ci_fix", "succeeded").state == "REVIEWING"
    assert _apply(b, "e4", "review_gate", "merged").state == "VERIFYING"
    assert _apply(b, "e5", "review_gate", "post_merge_verified").state == "COMPLETED"
    assert _apply(b, "e6", "ci_fix", "failed").state == "COMPLETED"           # completed only allows rollback
    assert len(b.incidents) == 1
    assert [t.to_state for t in next(iter(b.incidents.values())).history][:3] == ["TRIAGED", "INVESTIGATING", "PROPOSED"]


def test_failure_is_terminal_and_unknown_states_ignored():
    b = IncidentBoard("v1")
    _apply(b, "e1", "ci_fix", "claimed")
    assert _apply(b, "e2", "ci_fix", "bogus").state == "EXECUTING"
    assert _apply(b, "e3", "ci_fix", "failed").state == "FAILED"
    assert _apply(b, "e4", "ci_fix", "running").state == "FAILED"
    assert b.summary()[0]["state"] == "FAILED"
    assert rp.Incident  # the board produces policy Incidents


def test_gate_events_move_only_incidents_bound_by_a_recorded_pr_url():
    from herdr_engineering.control_plane.intel_incidents import pr_key_from_url
    assert pr_key_from_url("https://github.com/o/r/pull/12") == "o/r#12"
    for bad in ("https://evil.example/o/r/pull/12", "https://github.com/o/r/pull/0", "https://github.com/o/r/issues/3", None, 5):
        assert pr_key_from_url(bad) is None
    b = IncidentBoard("v1")
    _apply(b, "e1", "ci_fix", "succeeded")
    assert b.apply_gate("o/r#12", event_id="g1", at="t", state="merged") is None      # unbound PR: ignored
    b.bind_pr("o/r#12", "p", "o/r#7")
    assert b.apply_gate("o/r#12", event_id="g1", at="t", state="merged").state == "VERIFYING"
    assert b.apply_gate("o/r#12", event_id="g2", at="t", state="push").state == "VERIFYING"   # other gate events ignored
