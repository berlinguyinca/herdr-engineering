"""Calibration math, report and gate. Pure functions over real records; nothing touches user config dirs."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.calibration import (
    MIN_DISTINCT_KEYS,
    MIN_NEGATIVE_LABELS,
    build_report,
    clopper_pearson_upper,
    gate_satisfied,
)
from herdr_engineering.control_plane.assistant_hub.shadow import JudgeRecord

TU = ("T", "U")
NOW = datetime(2026, 11, 2, tzinfo=UTC)
CFG: dict[str, Any] = {
    "enabled": True, "backend_confirmed": True, "model": "m",
    "thresholds": {"destructive_score": 0.5, "irreversible_score": 0.5, "severity_score": 1.0, "covers_score": 0.5},
    "calibration": {"min_labeled_decisions": 20, "max_false_negative_bound": 0.15},
}


def _version() -> str:
    from herdr_engineering.control_plane.assistant_hub.judge_version import judge_version
    return judge_version(CFG)


def rec(n: int, *, decision: str = "approved", escalate: bool = False, regret: bool = False, status: str = "ok",
        version: str | None = None, risk: str = "local_reversible", session: str | None = None,
        destructive: float = 0.1) -> JudgeRecord:
    return JudgeRecord(record_id=f"r{n}", tenant_id="T", user_id="U", worker_id="w", session_id=session or f"s{n}",
                       task_id="t", key_id=f"k{n}",
                       risk_class=risk, judge_version=version if version is not None else _version(),
                       status=status, escalate=escalate, owner_decision=decision, regret=regret,  # type: ignore[arg-type]
                       destructive=destructive, created_at=NOW - timedelta(minutes=n))


def cls(records: list[JudgeRecord], risk: str = "local_reversible"):
    return build_report(records, CFG, NOW, *TU).classes[risk]


def test_clopper_pearson_known_values() -> None:
    assert clopper_pearson_upper(0, 30) == pytest.approx(0.0950, abs=1e-3)
    assert clopper_pearson_upper(0, 100) == pytest.approx(0.0295, abs=1e-3)
    assert clopper_pearson_upper(1, 10) == pytest.approx(0.3942, abs=1e-3)
    assert clopper_pearson_upper(5, 200) == pytest.approx(0.0527, abs=1e-3)


def test_clopper_pearson_edges_and_monotonic() -> None:
    assert clopper_pearson_upper(0, 0) == 1.0
    assert clopper_pearson_upper(7, 7) == 1.0
    assert [clopper_pearson_upper(0, n) for n in (10, 20, 50, 100)] == sorted(
        [clopper_pearson_upper(0, n) for n in (10, 20, 50, 100)], reverse=True)
    assert clopper_pearson_upper(0, 50) < clopper_pearson_upper(1, 50) < clopper_pearson_upper(2, 50)


def test_approved_and_safe_is_not_a_false_negative() -> None:
    c = cls([rec(1)])
    assert (c.labeled, c.judge_said_safe, c.false_negatives) == (1, 1, 0)


def test_denied_and_safe_is_a_false_negative() -> None:
    assert cls([rec(1, decision="denied")]).false_negatives == 1


def test_regret_and_safe_is_a_false_negative() -> None:
    assert cls([rec(1, regret=True)]).false_negatives == 1


def test_denied_and_escalated_is_not_a_false_negative() -> None:
    c = cls([rec(1, decision="denied", escalate=True)])
    assert (c.labeled, c.judge_said_safe, c.false_negatives) == (1, 0, 0)


def test_unavailable_unlabeled_and_old_version_ignored() -> None:
    records = [rec(1, status="unavailable"), rec(2, decision=""), rec(3, version="old", decision="denied")]
    assert build_report(records, CFG, NOW, *TU).classes == {}


def test_duplicates_counted_once() -> None:
    a, b = rec(1, decision="denied"), rec(1, decision="denied")
    b.record_id = "other"
    c = cls([a, b, rec(2)])
    assert (c.labeled, c.false_negatives) == (2, 1)


def test_risk_classes_separate() -> None:
    rpt = build_report([rec(1, risk="outward", decision="denied"), rec(2)], CFG, NOW, *TU)
    assert rpt.classes["outward"].false_negatives == 1
    assert rpt.classes["local_reversible"].false_negatives == 0
    assert rpt.judge_version == _version()


def test_reliability_bins() -> None:
    c = cls([rec(1, destructive=0.05, decision="denied"), rec(2, destructive=0.05), rec(3, destructive=0.95)])
    assert len(c.reliability) == 10
    assert (c.reliability[0].n, c.reliability[0].rate) == (2, 0.5)
    assert (c.reliability[9].n, c.reliability[9].rate) == (1, 0.0)
    assert c.reliability[5].n == 0


def test_report_note_says_labels_are_noisy() -> None:
    note = build_report([], CFG, NOW, *TU).labels_note.lower()
    assert "owner" in note and "noisy" in note


def _report(safe: int, caught: int = 6, fn: int = 0, cfg: dict[str, Any] | None = None):
    """``safe`` approved-and-safe, ``caught`` denied-and-escalated (judge right), ``fn`` denied-and-safe."""
    recs = [rec(i) for i in range(safe)]
    recs += [rec(1000 + i, decision="denied", escalate=True) for i in range(caught)]
    recs += [rec(2000 + i, decision="denied") for i in range(fn)]
    return build_report(recs, cfg or CFG, NOW, *TU)


def test_gate_table() -> None:
    ok = _report(40)
    assert gate_satisfied(ok, CFG, "local_reversible") == (True, "")
    assert gate_satisfied(ok, {**CFG, "enabled": False}, "local_reversible") == (False, "judge_disabled")
    assert gate_satisfied(ok, {**CFG, "backend_confirmed": False}, "local_reversible") == (False, "backend_unconfirmed")
    assert gate_satisfied(_report(10, caught=6), CFG, "local_reversible") == (False, "insufficient_labels")
    assert gate_satisfied(ok, CFG, "outward") == (False, "insufficient_labels")
    assert gate_satisfied(_report(40, fn=6), CFG, "local_reversible") == (False, "fn_bound_exceeded")


def test_gate_requires_literal_true() -> None:
    assert gate_satisfied(_report(40), {**CFG, "enabled": "yes"}, "local_reversible") == (False, "judge_disabled")


def test_constants() -> None:
    assert (MIN_DISTINCT_KEYS, MIN_NEGATIVE_LABELS) == (5, 5)


def test_approvals_only_cannot_pass() -> None:
    rpt = _report(60, caught=0)
    c = rpt.classes["local_reversible"]
    assert (c.labeled, c.false_negatives) == (60, 0)
    assert gate_satisfied(rpt, CFG, "local_reversible") == (False, "no_discrimination")


def test_too_few_negatives_fails_with_five_passes() -> None:
    assert gate_satisfied(_report(40, caught=4), CFG, "local_reversible") == (False, "no_discrimination")
    assert gate_satisfied(_report(40, caught=5), CFG, "local_reversible") == (True, "")


def test_insufficient_diversity() -> None:
    recs = []
    for n in range(30):   # many workers, only 4 distinct key ids
        r = rec(n)
        r.worker_id, r.key_id = f"w{n}", f"k{n % 4}"
        recs.append(r)
    for n in range(6):
        r = rec(100 + n, decision="denied", escalate=True)
        r.worker_id, r.key_id = f"x{n}", f"k{n % 4}"
        recs.append(r)
    rpt = build_report(recs, CFG, NOW, *TU)
    assert rpt.classes["local_reversible"].labeled >= 20
    assert gate_satisfied(rpt, CFG, "local_reversible") == (False, "insufficient_diversity")


def test_gate_precedence_diversity_before_discrimination() -> None:
    recs = []
    for n in range(30):
        r = rec(n)
        r.worker_id, r.key_id = f"w{n}", f"k{n % 4}"
        recs.append(r)
    assert gate_satisfied(build_report(recs, CFG, NOW, *TU), CFG, "local_reversible") == (False, "insufficient_diversity")


def test_dedupe_by_tenant_user_worker_key_earliest_wins() -> None:
    first, again = rec(5, decision="approved"), rec(1, decision="denied")      # rec(5) is the earlier one
    again.key_id, again.session_id, again.task_id = first.key_id, "other-session", "other-task"
    c = cls([first, again])
    assert (c.labeled, c.false_negatives) == (1, 0)


def test_other_tenant_and_user_ignored() -> None:
    mine, other_t, other_u = rec(1), rec(2), rec(3)
    other_t.tenant_id, other_u.user_id = "X", "Y"
    assert cls([mine, other_t, other_u]).labeled == 1


def test_gate_config_validated_with_safe_defaults() -> None:
    ok = _report(40)
    for bad in (True, 5, 0, "20"):
        cfg = {**CFG, "calibration": {"min_labeled_decisions": bad, "max_false_negative_bound": 0.15}}
        # invalid minimum falls back to 200 labeled, which 46 records do not reach
        assert gate_satisfied(ok, cfg, "local_reversible") == (False, "insufficient_labels")
    for bad in (0, 0.0, 1, 1.5, -1, True, "x"):
        cfg = {**CFG, "calibration": {"min_labeled_decisions": 20, "max_false_negative_bound": bad}}
        # invalid bound falls back to 0.05; 0/40 safe has upper bound ~0.072 so it fails
        assert gate_satisfied(ok, cfg, "local_reversible") == (False, "fn_bound_exceeded")


def test_stale_report() -> None:
    ok = _report(40)
    changed = {**CFG, "model": "other"}
    assert gate_satisfied(ok, changed, "local_reversible") == (False, "stale_report")


def test_effective_state_resets_on_judge_version_change() -> None:
    # only an ACTIVE grant is judged by version; PROPOSED rows are created with an empty version and stay PROPOSED
    p = ActionGrant(state=GrantState.PROPOSED, judge_version="a", policy_version=1)
    assert p.effective_state(NOW, 1, "b") is GrantState.PROPOSED
    assert ActionGrant(state=GrantState.PROPOSED, judge_version="").effective_state(NOW, 1, "b") is GrantState.PROPOSED
    kw = dict(state=GrantState.ACTIVE, policy_version=1, activated_at=NOW, confirmed_by="o", expires_at=NOW + timedelta(days=1))
    a = ActionGrant(judge_version="a", **kw)
    assert a.effective_state(NOW, 1) is GrantState.ACTIVE
    assert a.effective_state(NOW, 1, "a") is GrantState.ACTIVE
    assert a.effective_state(NOW, 1, "b") is GrantState.SHADOW
    assert ActionGrant(judge_version="", **kw).effective_state(NOW, 1, "b") is GrantState.SHADOW
