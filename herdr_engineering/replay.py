"""Sanitized historical placement replay and advisory resource estimates."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import quantiles


@dataclass(frozen=True)
class ReplayResult:
    decisions: int
    queued: int
    overload_seconds: float
    admission_p50_seconds: float | None
    admission_p95_seconds: float | None
    confidence: str
    limitations: tuple[str, ...]


def replay(history: list[dict], *, candidate_weights=None) -> ReplayResult:
    rows = [row for row in history if isinstance(row, dict)]
    waits = sorted(float(row["wait_seconds"]) for row in rows if isinstance(row.get("wait_seconds"), (int, float)))
    queued = sum(row.get("state") == "queued" for row in rows)
    q = quantiles(waits, n=20, method="inclusive") if len(waits) >= 2 else []
    return ReplayResult(
        decisions=len(rows), queued=queued,
        overload_seconds=sum(float(row.get("overload_seconds", 0) or 0) for row in rows),
        admission_p50_seconds=q[9] if q else (waits[0] if waits else None),
        admission_p95_seconds=q[18] if len(q) >= 19 else (waits[-1] if waits else None),
        confidence="low" if len(rows) < 20 else "observational",
        limitations=("counterfactual replay is not a performance claim",
                     "missing telemetry and external inference scheduling are not inferred"),
    )


def advisory_estimate(samples: list[float], *, cold_start: float) -> dict:
    values = [float(v) for v in samples if float(v) >= 0]
    if not values:
        return {"p50": cold_start, "p90": cold_start, "proposed": False, "reason": "cold_start"}
    values.sort()
    return {"p50": values[len(values) // 2], "p90": values[min(len(values) - 1, int(len(values) * .9))],
            "proposed": len(values) >= 5, "reason": "observed_history"}

