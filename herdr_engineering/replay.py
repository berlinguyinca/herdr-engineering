"""Sanitized historical placement replay and advisory resource estimates."""
from __future__ import annotations

import math
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


def placement_preview(reservations, *, cpu_weight=.6, memory_weight=.4, excluded_hosts=()):
    """Rescore recorded candidate snapshots without relaxing hard exclusions."""
    weights = (cpu_weight, memory_weight)
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in weights) or sum(weights) == 0:
        raise ValueError("simulator weights must be finite values between zero and one with positive sum")
    excluded = set(excluded_hosts)
    results, incomplete = [], 0
    for row in list(reservations)[-200:]:
        decision = row.get("decision") or {}
        candidates = []
        missing = False
        for uid, explanation in (decision.get("explanations") or {}).items():
            if uid in excluded or explanation.get("reason_codes") or explanation.get("eligible") is not True:
                continue
            measured = explanation.get("measured") or {}
            keys = ("total_cpu", "available_cpu", "total_memory_gib", "available_memory_gib")
            if any(type(measured.get(key)) not in (int, float) or not math.isfinite(measured[key]) for key in keys):
                missing = True
                continue
            if measured["total_cpu"] <= 0 or measured["total_memory_gib"] <= 0:
                missing = True
                continue
            cpu = max(0, measured["available_cpu"] - row.get("requested_cpu", 0)) / measured["total_cpu"]
            memory = max(0, measured["available_memory_gib"] - row.get("requested_memory", 0)) / measured["total_memory_gib"]
            candidates.append((cpu_weight * cpu + memory_weight * memory, uid))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected = candidates[0][1] if candidates else None
        incomplete += bool(missing)
        results.append({"request_id": row.get("request_id"), "observed_host_uid": decision.get("selected_host_uid"),
                        "simulated_host_uid": selected, "snapshot_incomplete": missing,
                        "state": "planned" if selected else "unknown" if missing else "queued"})
    return {"observed_decisions": len(results), "decisions": results,
            "changed_placements": sum(row["observed_host_uid"] != row["simulated_host_uid"] for row in results if not row["snapshot_incomplete"]),
            "queued": sum(row["state"] == "queued" for row in results), "incomplete_snapshots": incomplete,
            "confidence": "counterfactual", "mutates_policy": False,
            "limitations": ["Independent recorded snapshots; later load is not predicted.",
                            "Hard exclusions are retained. Missing metrics are not fabricated.",
                            "No measured performance improvement or completion forecast is claimed."]}

