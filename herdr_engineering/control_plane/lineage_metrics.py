"""Read-only delivery metrics over a reader's lineage-graph snapshot (spec 5, 10.9).

Computed from the SAME reader-filtered snapshot the room analytics use (``collab_insights.take_snapshot``), so it inherits
their rules and adds no store, scheduler or parallel graph walk:
* only what the reader can see now is counted; nothing is broken down by person or agent;
* a figure that rests on fewer than ``min_group_size`` items (a rate, a duration, a cost) is ``suppressed``;
* a state that an emitter wrote about its own work is labelled ``self_reported``; "accepted change" and "gate failure"
  count only states written by the configured ``evidence_writers`` (nothing is claimed when none is configured);
* tokens and cost are summed ONLY from nodes whose emitter reported them, otherwise the answer is ``unknown``.
  Nothing is estimated, extrapolated or defaulted.
Pure: the function reads the snapshot and writes nothing.
"""
from __future__ import annotations

from . import collab_insights as ins

HANDOFF_OK = frozenset({"running", "working", "active", "in_progress", "started", "accepted", "ready", "resumed",
                        "done", "completed", "success", "passed"})
HANDOFF_FAILED = frozenset({"failed", "error", "cancelled", "abandoned", "rejected", "timeout", "timed_out", "expired",
                            "lost", "lease_expired"})
GATE_FAILED = frozenset({"failed", "blocked", "rejected", "error"})
FINDING_CONFIRMED = frozenset({"confirmed", "accepted", "fixed", "resolved"})
FINDING_DISMISSED = frozenset({"dismissed", "rejected", "false_positive", "wontfix"})
FINDING_CLOSED = FINDING_CONFIRMED | FINDING_DISMISSED | frozenset({"closed", "done"})
ISSUE_CLOSED = frozenset({"closed", "done", "resolved", "completed", "merged"})
USAGE_NODES = ("task", "attempt", "session")


def _number(value, hi: float):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= hi else None


def _usage(snap: dict, k: int) -> dict:
    """Tokens and cost, only where the emitter reported them."""
    tokens = cost = 0.0
    token_nodes = cost_nodes = eligible = 0
    for n in snap["nodes"].values():
        if n["node_type"] not in USAGE_NODES:
            continue
        eligible += 1
        attrs = n.get("attrs") or {}
        parts = [_number(attrs.get(name), 1e15) for name in ("tokens", "tokens_in", "tokens_out")]
        if any(p is not None for p in parts):
            tokens += sum(p for p in parts if p is not None)
            token_nodes += 1
        spent = _number(attrs.get("cost_usd"), 1e9)
        if spent is not None:
            cost += spent
            cost_nodes += 1

    def shown(total, reporters, unit):
        if not reporters:
            return {"status": "unknown", "reason": f"no event reported {unit}; nothing is estimated"}
        if reporters < k:
            return {**ins.suppressed(k), "self_reported": True}
        return {"status": "reported", "self_reported": True, "reporting_nodes": reporters, "of_nodes": eligible,
                "value": round(total, 4) if unit == "cost" else int(total)}
    return {"tokens": shown(tokens, token_nodes, "tokens"), "cost_usd": shown(cost, cost_nodes, "cost")}


def lineage_metrics(snap: dict, cfg: dict, now: float | None = None) -> dict:
    k = cfg["min_group_size"]
    writers = frozenset(cfg.get("evidence_writers") or ())
    nodes, lanes, markers = snap["nodes"], snap["lanes"], snap["markers"]
    by_type: dict[str, list[dict]] = {}
    for n in nodes.values():
        by_type.setdefault(n["node_type"], []).append(n)
    edges = [e for e in snap["edges"] if not e.get("removed")]

    # accepted changes and gate failures: trusted writers only
    if writers:
        merged = sum(1 for n in by_type.get("pr", []) if ins.trusted_value(n, "state", writers) in ins.MERGED_STATES)
        ci_failed = sum(1 for n in by_type.get("ci", []) if ins.trusted_value(n, "state", writers) in ins.CI_FAILED)
        gate_blocked = sum(1 for n in by_type.get("gate", []) if ins.trusted_value(n, "state", writers) in GATE_FAILED)
        red_edges = sum(1 for e in edges if e["edge_type"] == "validated" and ins._edge_trusted(e, writers)
                        and str((e.get("attrs") or {}).get("conclusion", "")).lower() in ("fail", "failed", "failure"))
        accepted = {"count": merged, "basis": "pull requests whose merged state came from a trusted evidence source"}
        gates = {"count": ci_failed + gate_blocked, "ci_failed": ci_failed, "gate_blocked": gate_blocked,
                 "validated_edges_failed": red_edges,
                 "basis": "ci nodes and gate nodes in a failed/blocked state reported by a trusted evidence source; the failed "
                          "validated edges are a cross-check and are not added"}
    else:
        unknown = {"count": "unknown", "reason": "no trusted evidence source is configured: self-reported state is never counted"}
        accepted, gates = dict(unknown), dict(unknown)

    # handoffs: success by the successor's reported state; latency from the edge to the successor's first running span
    outcomes = {"success": 0, "failed": 0, "pending": 0, "unknown_target": 0}
    latencies: list[float] = []
    handoffs = [e for e in edges if e["edge_type"] == "handoff"]
    at_of = {(m["from_id"], m["to_id"]): ins.epoch(m["at"]) for m in markers
             if m["edge_type"] == "handoff" and m["kind"] == "edge"}
    for e in handoffs:
        target = nodes.get(e["to_id"])
        state = (target or {}).get("state")
        if target is None:
            outcomes["unknown_target"] += 1
        elif state in HANDOFF_FAILED:
            outcomes["failed"] += 1
        elif state in HANDOFF_OK:
            outcomes["success"] += 1
            started, begun = at_of.get((e["from_id"], e["to_id"])), None
            for span in (lanes.get(e["to_id"]) or {}).get("spans", ()):
                s = ins.epoch(span["start"])
                if span["state"] in HANDOFF_OK and s is not None and started is not None and s >= started:
                    begun = s
                    break
            if begun is not None:
                latencies.append(begun - started)
        else:
            outcomes["pending"] += 1
    decided = outcomes["success"] + outcomes["failed"]
    handoff = {"total": len(handoffs), "outcomes": outcomes, "self_reported": True,
               "success_rate": {"basis": "successor reported a running/done state vs a failed/abandoned state",
                                **ins._rate(outcomes["success"], decided, k)},
               "recovery_latency": {"basis": "server receipt time of the handoff edge to the successor's first running "
                                            "span; successful handoffs with both times only",
                                    **ins._durations(latencies, k)}}

    # rework: how often evidence had to be redone
    stale_reviews = sum(1 for e in edges if e["edge_type"] == "reviewed" and e.get("stale"))
    superseded = sum(1 for e in edges if e["edge_type"] == "supersession")
    repair_pushes = sum(1 for m in markers if m["kind"] == "head_change"
                        and nodes.get(m["to_id"], {}).get("node_type") == "pr")
    rework = {"stale_reviews": stale_reviews, "superseded": superseded, "head_changes_after_review": repair_pushes,
              "basis": "reviews bound to a head that was later replaced; supersession edges; reviewed/validated "
                       "evidence re-bound to a new PR head"}

    # review findings
    findings = by_type.get("finding", [])
    severity: dict[str, int] = {}
    confirmed = dismissed = still_open = 0
    for n in findings:
        sev = ins.clip((n.get("attrs") or {}).get("severity"), 32) or "unspecified"
        severity[sev] = severity.get(sev, 0) + 1
        state = n.get("state")
        confirmed += state in FINDING_CONFIRMED
        dismissed += state in FINDING_DISMISSED
        still_open += state not in FINDING_CLOSED
    review_findings = {"total": len(findings), "open": still_open, "self_reported": True,
                       "by_severity": ins._merge_small(severity, k) if severity else {},
                       "precision": {"basis": "confirmed/fixed vs dismissed/false-positive findings",
                                     **ins._rate(confirmed, confirmed + dismissed, k)}}

    # issue lead time: first sighting of the issue to the start of its closed state (server receipt times)
    lead: list[float] = []
    for n in by_type.get("issue", []):
        lane = lanes.get(n["id"]) or {}
        first = ins.epoch(lane.get("first_at"))
        done = next((ins.epoch(s["start"]) for s in lane.get("spans", ()) if s["state"] in ISSUE_CLOSED), None)
        if first is not None and done is not None and done >= first:
            lead.append(done - first)

    return {
        "scope": {"reader_view_only": True, "per_person_metrics": False, "min_group_size": k,
                  "note": "Computed only from items you can see now; unreported tokens and cost stay unknown."},
        "as_of": snap["as_of"], "window": snap["range"], "truncated": snap["reason"],
        "timeline_truncated": snap["timeline_reason"],
        "accepted_changes": accepted, "gate_failures": gates, "handoffs": handoff, "rework": rework,
        "review_findings": review_findings,
        "issue_lead_time": {"basis": "first sighting of an issue to its first closed state, server receipt times",
                            **ins._durations(lead, k)},
        "sessions": {"total": len(by_type.get("session", []))},
        "usage": _usage(snap, k),
    }
