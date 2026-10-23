"""Pure merge decision, fenced on the exact PR head SHA.

Ported from pi-engineering/src/merge/mergeGate.ts without the failure-baseline
classification. Reject reasons are collected first, then wait reasons.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .models import CheckRun, Verdict


@dataclass(frozen=True)
class MergeDecision:
    decision: Literal["merge", "wait", "reject"]
    reasons: tuple[str, ...]


def decide_merge(
    *,
    head_sha: str,
    checks: Sequence[CheckRun],
    required_checks: Sequence[str],
    verdict: Verdict | None,
) -> MergeDecision:
    reasons: list[str] = []

    if verdict is not None and verdict.sha != head_sha:
        reasons.append(f"review sha {verdict.sha} != head {head_sha}")
    for check in checks:
        if check.sha != head_sha:
            reasons.append(f"check {check.name} sha {check.sha} != head {head_sha}")
    if verdict is not None and verdict.verdict == "FAIL":
        reasons.append("review FAIL")
    for check in checks:
        if check.status == "fail":
            reasons.append(f"check {check.name} failed")
    if reasons:
        return MergeDecision("reject", tuple(reasons))

    if verdict is None:
        reasons.append("no review")
    required = list(dict.fromkeys(required_checks))
    for name in required:
        if not any(c.name == name for c in checks):
            reasons.append(f"required check {name} missing")
    for check in checks:
        if check.name not in required:
            continue
        if check.status == "pending":
            reasons.append(f"required check {check.name} pending")
        elif check.status == "skipped":
            reasons.append(f"required check {check.name} skipped")
    if reasons:
        return MergeDecision("wait", tuple(reasons))

    return MergeDecision("merge", ())
