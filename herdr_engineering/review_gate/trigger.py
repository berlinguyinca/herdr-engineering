"""CI-green trigger rule: a pure verdict over Woodpecker pipeline rows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .config import RepoEnrollment

_RED = frozenset({"failure", "error", "killed", "failed"})


@dataclass(frozen=True)
class CiVerdict:
    result: Literal["green", "red", "pending"]
    failures: tuple[str, ...] = ()


def _num(row: dict) -> int:
    try:
        return int(row.get("run_number"))
    except (TypeError, ValueError):
        return -1


def ci_verdict_for(
    sha: str, rows: Sequence[dict], enrollment: RepoEnrollment, *, snapshot_stale: bool
) -> CiVerdict:
    """Decide whether CI for ``sha`` is green, red or still pending.

    Precedence (first match wins):
      1. ``snapshot_stale`` -> pending (never trust a stale snapshot).
      2. Only rows with ``revision == sha`` and ``event`` in
         ``enrollment.required_events`` count; none -> pending.
      3. Per event only the highest ``run_number`` counts, so a later retry
         supersedes an earlier run of that event (success fixes a failure; a
         later failure after success is red).
      4. Any counted run failure/error/killed -> red, even if others are still
         running (no review should be spent). Failures are ``"run <n> <state>"``
         sorted by run number.
      5. Any other non-success (running/pending/blocked, declined/skipped/
         canceled, unknown state) -> pending; these need a wait or a human retry.
      6. Otherwise (every counted run succeeded) -> green.
    """
    if snapshot_stale:
        return CiVerdict("pending", ())
    latest: dict[str, dict] = {}
    for r in rows:
        if r.get("revision") != sha or r.get("event") not in enrollment.required_events:
            continue
        ev = r["event"]
        if ev not in latest or _num(r) > _num(latest[ev]):
            latest[ev] = r
    if not latest:
        return CiVerdict("pending", ())
    counted = sorted(latest.values(), key=_num)
    failures = tuple(f"run {_num(r)} {r.get('state')}" for r in counted if r.get("state") in _RED)
    if failures:
        return CiVerdict("red", failures)
    if all(r.get("state") == "success" for r in counted):
        return CiVerdict("green", ())
    return CiVerdict("pending", ())
