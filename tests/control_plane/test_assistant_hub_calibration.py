"""Calibration math, report and gate. Pure functions over real records; nothing touches user config dirs."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.calibration import (
    build_report,
    clopper_pearson_upper,
    gate_satisfied,
)
from herdr_engineering.control_plane.assistant_hub.shadow import JudgeRecord

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
    return JudgeRecord(record_id=f"r{n}", worker_id="w", session_id=session or f"s{n}", task_id="t", key_id=f"k{n}",
                       risk_class=risk, judge_version=version if version is not None else _version(),
                       status=status, escalate=escalate, owner_decision=decision, regret=regret,  # type: ignore[arg-type]
                       destructive=destructive, created_at=NOW - timedelta(minutes=n))


def cls(records: list[JudgeRecord], risk: str = "local_reversible"):
    return build_report(records, CFG, NOW).classes[risk]


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
    assert build_report(records, CFG, NOW).classes == {}


def test_duplicates_counted_once() -> None:
    a, b = rec(1, decision="denied"), rec(1, decision="denied")
    b.record_id = "other"
    c = cls([a, b, rec(2)])
    assert (c.labeled, c.false_negatives) == (2, 1)


def test_risk_classes_separate() -> None:
    rpt = build_report([rec(1, risk="outward", decision="denied"), rec(2)], CFG, NOW)
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
    note = build_report([], CFG, NOW).labels_note.lower()
    assert "owner" in note and "noisy" in note


def _report(n: int, fn: int = 0):
    return build_report([rec(i, decision="denied" if i < fn else "approved") for i in range(n)], CFG, NOW)


def test_gate_table() -> None:
    ok = _report(40)
    assert gate_satisfied(ok, CFG, "local_reversible") == (True, "")
    assert gate_satisfied(ok, {**CFG, "enabled": False}, "local_reversible") == (False, "judge_disabled")
    assert gate_satisfied(ok, {**CFG, "backend_confirmed": False}, "local_reversible") == (False, "backend_unconfirmed")
    assert gate_satisfied(_report(19), CFG, "local_reversible") == (False, "insufficient_labels")
    assert gate_satisfied(ok, CFG, "outward") == (False, "insufficient_labels")
    assert gate_satisfied(_report(40, fn=3), CFG, "local_reversible") == (False, "fn_bound_exceeded")


def test_gate_requires_literal_true() -> None:
    assert gate_satisfied(_report(40), {**CFG, "enabled": "yes"}, "local_reversible") == (False, "judge_disabled")


def test_effective_state_resets_on_judge_version_change() -> None:
    g = ActionGrant(state=GrantState.PROPOSED, judge_version="a", policy_version=1)
    assert g.effective_state(NOW, 1) is GrantState.PROPOSED
    assert g.effective_state(NOW, 1, "a") is GrantState.PROPOSED
    assert g.effective_state(NOW, 1, "b") is GrantState.SHADOW
    assert ActionGrant(state=GrantState.PROPOSED, judge_version="").effective_state(NOW, 1, "b") is GrantState.PROPOSED
