"""T15: evidence-based progress, derived attention items and optimization analytics (HCN workbench).

Everything here is a PURE function over one ``snapshot`` of what a single reader may see in ONE room. The snapshot is
taken only through the reads T09 already built (`GraphProjection.room_graph`, `room_timeline`, `list_nodes`), which
filter every node, edge and event by the reader's current membership, are paged and budgeted, and take the projection
lock for one small chunk at a time. Nothing in this module touches the projection's internals, the HCN service lock,
or a hidden object, and nothing stores anything: progress, items and analytics are derived at read time.

TRUST, in one sentence: **a number may only move because of evidence written by a principal the OPERATOR listed**
(``control_plane.collab.insights.evidence_writers``, YAML only), or by this room's own HCN service principal
(``collab:room:<id>``) for accepted proposals; what an agent says about itself (state, labels, diff stats, cost) is
shown, labelled ``reported``, and never counted. The review verdict comes from the T06 quorum, which has its own
configured reviewers. Spec of record: docs/contracts/lineage-events.md ("Attention inbox, progress and analytics (T15)").
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any

from .graph import MAX_NODES

# ----------------------------------------------------------------------------------------------- vocabulary
SUCCESS_STATES = frozenset({"done", "completed", "success", "passed", "merged"})
MERGED_STATES = frozenset({"merged"})
EXCLUDED_STATES = frozenset({"cancelled", "superseded"})          # leave the denominator ONLY when a trusted writer says so
ACCEPTED_STATES = frozenset({"accepted"})
ACTIVE_STATES = frozenset({"running", "working", "active", "in_progress", "started"})
BLOCKED_STATES = frozenset({"blocked", "waiting", "waiting_on"})
IDLE_STATES = frozenset({"idle", "paused"})
STUCK_STATES = frozenset({"stuck", "stalled", "timeout", "timed_out", "expired", "lease_expired"})
CI_FAILED = frozenset({"failed", "failure", "error"})
SELECTED_STATES = frozenset({"selected", "promoted"})
LOST_STATES = frozenset({"superseded", "rejected"})
TASK_LIKE = ("task", "attempt")
SEVERITY_RANK = {"info": 1, "notice": 2, "warning": 3, "error": 4}      # the vocabulary of attention.AttentionCenter
LINK_EDGES = frozenset({"issue_link", "produced"})
SERVICE_PROPOSAL_PREFIX = "artifact:hcn-proposal."
SERVICE_DELEGATION_PREFIX = "task:hcn-delegation."
PR_RE = re.compile(r"pr:github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})#([0-9]{1,9})\Z")
SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_EVIDENCE_PER_TASK = 8
MAX_LINKED_PRS = 10
MAX_TASKS_SHOWN = 300
MAX_MISSION_GROUPS = 40
MAX_LABEL = 160
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]")
METHOD = "completion_interval_quantiles_v1"


def clip(value: Any, limit: int = MAX_LABEL) -> str | None:
    """Untrusted text from the graph, reduced to something safe to put in a response: control, bidi and zero-width
    characters removed, length bounded. (The client still escapes it.)"""
    return _CTRL.sub("", value)[:limit] if isinstance(value, str) and value else None


def epoch(value: Any) -> float | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    except (TypeError, ValueError):
        return None


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts is not None and math.isfinite(ts) else None


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def quantile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank quantile: deterministic, no interpolation, no randomness (an estimate must be reproducible)."""
    n = len(sorted_values)
    return sorted_values[min(n - 1, max(0, math.ceil(p * n) - 1))]


def suppressed(k: int) -> dict:
    return {"suppressed": True, "reason": f"fewer than {k} items contribute; nothing is shown so a single item cannot be singled out"}


# ----------------------------------------------------------------------------------------------- the snapshot
def take_snapshot(graph, reader: dict, contexts: frozenset, since: float | None, *, max_pages: int,
                  mission_ctxs: list[tuple]) -> dict:
    """What ``reader`` may see of one room: nodes, live edges, per-node timeline lanes and edge markers, and which
    visible tasks belong to which linked mission. Every call below is the reader-filtered read T09 built; a page
    boundary hands the projection lock back, and ``reason`` names the budget that cut the read short (it counts only
    the reader's own visible items, never hidden ones)."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    reason, offset = None, 0
    for _ in range(max_pages):
        out = graph.room_graph(reader, contexts, limit=MAX_NODES, offset=offset)
        for n in out["nodes"]:
            nodes.setdefault(n["id"], n)
        for e in out["edges"]:
            if not e["removed"]:
                edges.setdefault((e["edge_type"], e["from_id"], e["to_id"], repr(e.get("binding"))), e)
        why = out["truncated_reason"]
        if out["next_offset"] is None:
            if why:
                reason = "edge_limit" if why == "limit" else why
            break
        if why not in (None, "limit"):
            reason = why
        offset = out["next_offset"]
        time.sleep(0.0002)                      # really hand the projection to a waiting ingest between pages
    else:
        reason = reason or "page_limit"
    tl = graph.room_timeline(reader, contexts, since=since)
    missions: dict[str, set] = {}
    if len(mission_ctxs) > MAX_MISSION_GROUPS:
        reason = reason or "mission_limit"
    for ctx in mission_ctxs[:MAX_MISSION_GROUPS]:
        got = graph.list_nodes(reader, "task", contexts=frozenset({ctx}), limit=MAX_NODES)
        missions[ctx[1]] = {n["id"] for n in got["nodes"]}
        if got["truncated"]:
            reason = reason or "mission_listing"
    return {"nodes": nodes, "edges": list(edges.values()), "reason": reason,
            "lanes": {lane["node_id"]: lane for lane in tl["lanes"]}, "markers": tl["markers"],
            "timeline_reason": tl["truncated_reason"], "missions": missions,
            "as_of": tl["as_of"], "range": tl["range"]}


def _index(snap: dict) -> dict:
    """Edge indexes (built once per snapshot)."""
    if "idx" in snap:
        return snap["idx"]
    out_, in_ = {}, {}
    for e in snap["edges"]:
        out_.setdefault((e["edge_type"], e["from_id"]), []).append(e)
        in_.setdefault((e["edge_type"], e["to_id"]), []).append(e)
    snap["idx"] = {"out": out_, "in": in_}
    return snap["idx"]


# ----------------------------------------------------------------------------------------------- trust helpers
def _entries(node: dict, kind: str, name: str | None = None) -> list[dict]:
    fields = node.get("fields") or {}
    contrib = node.get("contributions") or {}
    if kind == "state":
        own = [fields.get("state")]
        more = contrib.get("state") or []
    else:
        own = [(fields.get("attrs") or {}).get(name)]
        more = (contrib.get("attrs") or {}).get(name) or []
    return [e for e in [*own, *more] if isinstance(e, dict)]


def trusted_value(node: dict | None, kind: str, writers: frozenset, name: str | None = None) -> Any:
    """The newest value of a node's state (or attribute) written by one of ``writers``; None when none did.
    A commons node (pr, issue, ci, artifact) carries every writer's value; only the listed writers' values are read."""
    if not node or not writers:
        return None
    best = [e for e in _entries(node, kind, name) if e.get("writer") in writers]
    if not best:
        return None
    return max(best, key=lambda e: e.get("admitted_at") or "").get("value")


def _edge_trusted(e: dict, writers: frozenset) -> bool:
    return (e.get("provenance") or {}).get("writer") in writers


def _pr_url(node_id: str) -> str | None:
    m = PR_RE.fullmatch(node_id or "")
    return f"https://github.com/{m.group(1)}/{m.group(2)}/pull/{m.group(3)}" if m else None


def _ref(node_id: str, nodes: dict) -> dict:
    n = nodes.get(node_id) or {}
    out = {"node_id": node_id, "node_type": n.get("node_type") or node_id.split(":")[0], "label": clip(n.get("label"))}
    url = _pr_url(node_id)
    if url:
        out["url"] = url
    return out


# ----------------------------------------------------------------------------------------------- task evidence
def linked_prs(task_id: str, snap: dict) -> list[tuple[str, dict, str]]:
    """PR nodes this task is linked to, with the link edge: directly (``issue_link`` / ``produced``, either
    direction) or through one shared issue. ``(pr_id, link_edge, route)``; bounded."""
    idx, nodes = _index(snap), snap["nodes"]
    found: list[tuple[str, dict, str]] = []
    seen = set()

    def add(pr, edge, route):
        if pr in seen or pr not in nodes or nodes[pr].get("node_type") != "pr" or len(found) >= MAX_LINKED_PRS:
            return
        seen.add(pr)
        found.append((pr, edge, route))
    for et in LINK_EDGES:
        for e in idx["out"].get((et, task_id), ()):
            add(e["to_id"], e, "direct")
        for e in idx["in"].get((et, task_id), ()):
            add(e["from_id"], e, "direct")
    for e in idx["out"].get(("issue_link", task_id), ()):
        issue = e["to_id"]
        if nodes.get(issue, {}).get("node_type") != "issue":
            continue
        for f in [*idx["in"].get(("issue_link", issue), ()), *idx["out"].get(("issue_link", issue), ())]:
            other = f["from_id"] if f["to_id"] == issue else f["to_id"]
            if other != task_id:
                # a two-hop link is only as good as its weaker edge: attested only if BOTH are
                add(other, {**f, "_second": e}, "via_issue")
    return found


def pr_facts(pr_id: str, snap: dict, writers: frozenset, quorum) -> dict:
    """Everything the evidence rules need about one PR, each item marked with why it does or does not count."""
    nodes = snap["nodes"]
    pr = nodes[pr_id]
    idx = _index(snap)
    state = trusted_value(pr, "state", writers)
    head = trusted_value(pr, "attr", writers, "head_sha")
    head = head.lower() if isinstance(head, str) and SHA_RE.fullmatch(head.lower()) else None
    validations = []
    for e in idx["in"].get(("validated", pr_id), ()):
        commit = ((e.get("binding") or {}).get("commit") or "")
        commit = commit.lower() if isinstance(commit, str) else ""
        why = []
        if not _edge_trusted(e, writers):
            why.append("writer_not_trusted")
        if head is None:
            why.append("head_unknown")
        elif commit != head:
            why.append("bound_to_another_head")
        if e.get("stale"):
            why.append("stale")
        validations.append({"from_id": e["from_id"], "commit": commit or None, "counted": not why, "why_not": why})
    out = {"pr": pr_id, "merged": state in MERGED_STATES, "state_reported": clip(pr.get("state"), 32),
           "head": head, "validations": validations[:MAX_EVIDENCE_PER_TASK],
           "validated": any(v["counted"] for v in validations), "quorum": None}
    if quorum is not None:
        out["quorum"] = quorum(pr_id)
    return out


def task_evidence(task_id: str, snap: dict, writers: frozenset, room_id: str, quorum) -> dict:
    """The completion evidence of ONE task. Returns the stage (``complete`` | ``verified`` | ``none``), the full list
    of evidence with ``counted`` and ``why_not`` (so a person sees what was ignored and why), and what the task
    reports about itself (``reported_state``), which never counts."""
    nodes, idx = snap["nodes"], _index(snap)
    node = nodes[task_id]
    service = f"collab:room:{room_id}"
    items: list[dict] = []
    trusted_pr = False
    complete = verified = False
    for pr_id, link, route in linked_prs(task_id, snap):
        ok_link = _edge_trusted(link, writers) and (route == "direct" or _edge_trusted(link["_second"], writers))
        facts = pr_facts(pr_id, snap, writers, quorum)
        if not ok_link:
            items.append({"kind": "claimed_link", **_ref(pr_id, nodes), "counted": False,
                          "why_not": ["link_not_attested_by_a_trusted_writer"]})
            continue
        trusted_pr = True
        items.append({"kind": "merged_pr", **_ref(pr_id, nodes), "counted": facts["merged"],
                      "why_not": [] if facts["merged"] else ["not_merged_according_to_a_trusted_writer"],
                      "reported_state": facts["state_reported"]})
        complete = complete or facts["merged"]
        for v in facts["validations"]:
            items.append({"kind": "validated_at_head", **_ref(v["from_id"], nodes), "subject": pr_id,
                          "commit": v["commit"], "counted": v["counted"], "why_not": v["why_not"]})
        verified = verified or facts["validated"]
        q = facts["quorum"]
        if q is not None:
            # satisfied for THIS reader only if the approvals the reader can see are enough: a hidden approval never
            # moves a number (T06 discloses that hidden reviews exist, not how many)
            sat = q.get("status") == "satisfied" and len(q.get("counted") or ()) >= (q.get("required") or 1)
            items.append({"kind": "reviewed_at_head", **_ref(pr_id, nodes), "counted": sat, "quorum_status": q.get("status"),
                          "head": q.get("head"), "why_not": [] if sat else [f"quorum_{q.get('status')}"]})
            verified = verified or sat
    # an accepted proposal: the room's own service wrote both the edge and the accepted state
    for e in idx["out"].get(("produced", task_id), ()):
        art = nodes.get(e["to_id"])
        if art is None or not e["to_id"].startswith(SERVICE_PROPOSAL_PREFIX):
            continue
        by_service = (e.get("provenance") or {}).get("writer") == service
        accepted = trusted_value(art, "state", frozenset({service})) in ACCEPTED_STATES
        ok = by_service and accepted
        items.append({"kind": "accepted_proposal", **_ref(e["to_id"], nodes), "counted": ok,
                      "why_not": [] if ok else ["not_written_by_this_rooms_service"]})
        complete = complete or ok
    # a validation bound straight to the task completes it only when no attested PR exists (then the PR decides)
    for e in idx["in"].get(("validated", task_id), ()):
        why = []
        if not _edge_trusted(e, writers):
            why.append("writer_not_trusted")
        if (e.get("binding") or {}).get("commit") is None:
            why.append("no_commit_binding")
        if e.get("stale"):
            why.append("stale")
        if trusted_pr:
            why.append("an_attested_pull_request_decides")
        ok = not why
        items.append({"kind": "validated_task", **_ref(e["from_id"], nodes), "commit": (e.get("binding") or {}).get("commit"),
                      "counted": ok, "why_not": why})
        complete = complete or ok
    stage = "complete" if complete else ("verified" if verified else "none")
    items.sort(key=lambda i: (not i["counted"], i["kind"], i["node_id"]))
    return {"task_id": task_id, "label": clip(node.get("label")), "reported_state": clip(node.get("state"), 32),
            "reported_done": node.get("state") in SUCCESS_STATES, "stage": stage,
            "excluded": trusted_value(node, "state", writers) in EXCLUDED_STATES,
            "evidence": items[:MAX_EVIDENCE_PER_TASK], "evidence_truncated": len(items) > MAX_EVIDENCE_PER_TASK}


# ----------------------------------------------------------------------------------------------- times (server receipt time)
def _span_start(lane: dict | None, states: frozenset) -> float | None:
    for s in (lane or {}).get("spans", ()):
        if s["state"] in states:
            return epoch(s["start"])
    return None


def _trusted_state_time(node: dict | None, writers: frozenset, states: frozenset) -> float | None:
    """When a TRUSTED writer's own state entry first said one of ``states`` (server admission time). Entries of other
    writers on the same commons node never move it, so an agent's early self-reported "merged" cannot shift a time."""
    times = [epoch(e["admitted_at"]) for e in _entries(node or {}, "state")
             if e.get("writer") in writers and e.get("value") in states and e.get("admitted_at")]
    times = [t for t in times if t is not None]
    return min(times) if times else None


def evidence_time(task: dict, snap: dict, writers: frozenset, room_id: str) -> float | None:
    """When the room's ledger first showed the evidence that completed the task (server receipt time, not the
    emitter's claim). None when it cannot be told; such a task is left out of any time statistic, never guessed."""
    times = []
    for ev in task["evidence"]:
        if not ev["counted"]:
            continue
        if ev["kind"] == "merged_pr":
            t = _trusted_state_time(snap["nodes"].get(ev["node_id"]), writers, MERGED_STATES)
        elif ev["kind"] == "accepted_proposal":
            t = _trusted_state_time(snap["nodes"].get(ev["node_id"]), frozenset({f"collab:room:{room_id}"}), ACCEPTED_STATES)
        elif ev["kind"] in ("validated_task", "validated_at_head"):
            subject = ev.get("subject") or task["task_id"]
            t = next((epoch(m["at"]) for m in snap["markers"]
                      if m["edge_type"] == "validated" and m["from_id"] == ev["node_id"] and m["to_id"] == subject
                      and m["kind"] in ("edge", "head_change")), None)
        else:
            continue
        if t is not None:
            times.append(t)
    return min(times) if times else None


# ----------------------------------------------------------------------------------------------- progress
def _group_label(mid: str, snap: dict) -> str | None:
    for cand in (f"mission:{mid}", mid):
        n = snap["nodes"].get(cand)
        if n and n.get("label"):
            return clip(n["label"])
    return None


def progress(snap: dict, cfg: dict, room_id: str, now: float, quorum=None, *, keep_rows: bool = False) -> dict:
    """Room and per-milestone (linked mission) progress from evidence only, with an explicit, labelled estimate."""
    writers = frozenset(cfg["evidence_writers"])
    k = cfg["eta_min_samples"]
    # service-generated delegation tasks are bookkeeping of the room's own service, not work items: they are reported
    # separately ("delegations") unless a trusted writer has marked one, which makes it an ordinary task
    tasks_ids, deleg = [], {"total": 0, "accepted": 0}
    for n in sorted(snap["nodes"].values(), key=lambda x: x["id"]):
        if n["node_type"] != "task":
            continue
        if n["id"].startswith(SERVICE_DELEGATION_PREFIX) and trusted_value(n, "state", writers) is None:
            deleg["total"] += 1
            deleg["accepted"] += any(
                e["to_id"].startswith(SERVICE_PROPOSAL_PREFIX) and trusted_value(
                    snap["nodes"].get(e["to_id"]), "state", frozenset({f"collab:room:{room_id}"})) in ACCEPTED_STATES
                for e in _index(snap)["out"].get(("produced", n["id"]), ()))
            continue
        tasks_ids.append(n["id"])
    # EVERY visible task in the snapshot is evaluated (counts only); only the DISPLAY list is capped below
    tasks = [task_evidence(t, snap, writers, room_id, quorum) for t in tasks_ids]
    partial_why = snap["reason"]                 # the graph read was cut: counts may be low
    timing_why = snap["timeline_reason"]         # the timeline was cut: counts are right, times are not
    for t in tasks:
        t["evidence_at"] = evidence_time(t, snap, writers, room_id) if t["stage"] == "complete" else None
        lane = snap["lanes"].get(t["task_id"])
        t["started_at"] = epoch(lane["first_at"]) if lane else None
    where = {}
    for mid, ids in snap["missions"].items():
        for i in ids:
            where.setdefault(i, mid)
    groups: dict[str | None, list[dict]] = {}
    for t in tasks:
        groups.setdefault(where.get(t["task_id"]), []).append(t)

    def summarize(rows: list[dict]) -> dict:
        counted = [t for t in rows if not t["excluded"]]
        done = sum(1 for t in counted if t["stage"] == "complete")
        ver = sum(1 for t in counted if t["stage"] == "verified")
        reported = sum(1 for t in counted if t["stage"] == "none" and t["reported_done"])
        total = len(counted)
        out = {"partial": bool(partial_why), "partial_reason": partial_why,
               "timing_partial": bool(timing_why), "timing_partial_reason": timing_why,
               "tasks": total, "excluded_by_trusted_writer": len(rows) - total, "complete": done,
               "verified_not_merged": ver, "reported_done_without_evidence": reported,
               "in_flight_or_not_started": total - done - ver - reported}
        if not writers:
            out.update(status="no_trusted_evidence_configured", completion=None, progress=None,
                       note="the operator has listed no evidence writers, so nothing can count as evidence yet")
        elif total == 0:
            out.update(status="insufficient_evidence", completion=None, progress=None, note="no tasks are visible to you here")
        else:
            out.update(status="ok", completion=round(done / total, 4),
                       progress=round((done + cfg["verified_credit"] * ver) / total, 4))
        return out
    all_summary = summarize(tasks)
    def _times(rows):
        return sorted(t["evidence_at"] for t in rows if t["evidence_at"] is not None and not t["excluded"])
    pool = _times(tasks)
    out_groups = []
    for mid in sorted(g for g in groups if g is not None):
        rows = groups[mid]
        g = {"milestone": f"mission:{mid}", "label": _group_label(mid, snap), **summarize(rows)}
        g["estimate"] = estimate(_times(rows), g["tasks"] - g["complete"], k, now, cfg, snap, summary=g)
        out_groups.append(g)
    room_rows = groups.get(None, [])
    if room_rows and out_groups:
        out_groups.append({"milestone": "room", "label": "Not tied to a mission", **summarize(room_rows)})
        out_groups[-1]["estimate"] = estimate(_times(room_rows), out_groups[-1]["tasks"] - out_groups[-1]["complete"], k, now,
                                              cfg, snap, summary=out_groups[-1])
    all_summary["estimate"] = estimate(pool, all_summary["tasks"] - all_summary["complete"], k, now, cfg, snap, summary=all_summary)
    by_stage = {"complete": 0, "verified": 1, "none": 2}
    shown = sorted(tasks, key=lambda t: (by_stage[t["stage"]], t["task_id"]))[:MAX_TASKS_SHOWN]
    omitted = max(0, len(tasks) - MAX_TASKS_SHOWN)
    return {**({"_rows": tasks} if keep_rows else {}), "room": all_summary, "milestones": out_groups, "delegations": deleg,
            "tasks": [{kk: v for kk, v in t.items() if kk not in ("evidence_at", "started_at")}
                      | {"evidence_at": iso(t["evidence_at"])} for t in shown],
            "tasks_omitted": omitted, "truncated": snap["reason"], "timeline_truncated": snap["timeline_reason"],
            "as_of": snap["as_of"], "method": {
                "completion": "a task is complete only with: a PR it is attested to be linked to that a trusted writer marks merged; "
                              "or an accepted proposal written by this room's service; or, when no attested PR exists, a validated edge "
                              "from a trusted writer. Reviewed/validated-at-head without a merge counts as verified, not complete.",
                "progress": f"(complete + {cfg['verified_credit']} x verified_not_merged) / tasks that a trusted writer has not excluded",
                "self_reports": "state, labels, diff statistics and cost written by an agent about itself are shown as reported and never counted"}}


def estimate(pool: list[float], remaining: int, min_samples: int, now: float, cfg: dict, snap: dict, *,
             summary: dict) -> dict:
    """A labelled ESTIMATE of when the remaining tasks will have evidence, from the intervals between the
    evidence-completed tasks of the same group (the room, or one milestone). Deterministic (nearest-rank quantiles, no sampling). Says "insufficient evidence"
    rather than inventing a date."""
    base = {"label": "estimate", "method": METHOD}
    if summary.get("status") != "ok":
        return {**base, "status": "insufficient_evidence", "reason": summary.get("note") or "no progress can be computed yet"}
    if summary.get("partial") or summary.get("timing_partial"):
        why = summary.get("partial_reason") or summary.get("timing_partial_reason")
        return {**base, "status": "insufficient_evidence",
                "reason": f"the room view is partial ({why}), so no pace is estimated"}
    if remaining <= 0:
        return {**base, "status": "not_needed", "reason": "every counted task has completion evidence"}
    n = len(pool)
    if n < min_samples:
        return {**base, "status": "insufficient_evidence", "needed": min_samples, "have": n,
                "reason": f"{n} task(s) with timed completion evidence; at least {min_samples} are needed to estimate a pace"}
    gaps = sorted(b - a for a, b in zip(pool, pool[1:], strict=False))
    if not gaps or gaps[-1] <= 0:
        return {**base, "status": "insufficient_evidence", "needed": min_samples, "have": n,
                "reason": "the completions all happened at the same instant, so no pace can be measured"}
    p25, p50, p75 = (quantile(gaps, p) for p in (0.25, 0.5, 0.75))
    anchor = pool[-1]
    lo, mid, hi = (max(now, anchor + remaining * q) for q in (p25, p50, p75))
    mean = sum(gaps) / len(gaps)
    cv = math.sqrt(sum((g - mean) ** 2 for g in gaps) / len(gaps)) / mean if mean > 0 else 0.0
    reasons = []
    level = "medium"
    if n < 8:
        level = "low"
        reasons.append(f"only {n} completions")
    if cv > 1.5:
        level = "low"
        reasons.append("completion intervals vary widely")
    if snap["reason"] or snap["timeline_reason"]:
        level = "low"
        reasons.append("the room view was cut by a read budget")
    if now - anchor > p75 * 2:
        level = "low"
        reasons.append("no completion for a long time, so the pace may no longer apply")
    if level == "medium" and n >= 30 and cv < 0.6:
        level = "high"
    return {**base, "status": "estimate", "n_completed": n, "n_remaining": remaining,
            "interval_seconds": {"p25": round(p25), "p50": round(p50), "p75": round(p75)},
            "finish": {"earliest": iso(lo), "likely": iso(mid), "latest": iso(hi)},
            "confidence": level, "confidence_reasons": reasons,
            "assumptions": ["remaining tasks need as long as finished ones", "no tasks are added or dropped",
                            "completions continue at the observed pace (parallelism unchanged)"],
            "window": {"from": iso(pool[0]), "to": iso(pool[-1])}}


# ----------------------------------------------------------------------------------------------- analytics
def _merge_small(groups: dict[str, int], k: int) -> dict:
    """A distribution with no cell smaller than ``k``: small cells are merged into 'other'; if 'other' is still
    smaller than ``k`` the smallest published cell joins it, so a hidden single item cannot be recovered as a
    difference between the total and the listed cells."""
    kept = dict(sorted(groups.items(), key=lambda kv: (-kv[1], kv[0])))
    other = 0
    for name in [n for n, c in kept.items() if c < k]:
        other += kept.pop(name)
    while 0 < other < k and kept:
        name = min(kept, key=lambda n: (kept[n], n))
        other += kept.pop(name)
    if 0 < other < k:
        return {"suppressed": True}
    if other:
        kept["other"] = other
    return kept


def _durations(rows: list[float], k: int) -> dict:
    if len(rows) < k:
        return suppressed(k)
    s = sorted(rows)
    return {"n": len(s), "median_seconds": round(quantile(s, 0.5)), "p75_seconds": round(quantile(s, 0.75)),
            "p90_seconds": round(quantile(s, 0.9))}


def _rate(num: int, den: int, k: int) -> dict:
    if den < k:
        return suppressed(k)
    return {"rate": round(num / den, 4), "of": den}


def _span_seconds(span: dict, now: float) -> float:
    a = epoch(span["start"])
    b = epoch(span["end"]) if span["end"] else now
    return max(0.0, (b if b is not None else now) - a) if a is not None else 0.0


def analytics_of(snap: dict, cfg: dict, room_id: str, now: float, quorum=None) -> dict:
    """Simple, explainable aggregates over what this reader may see. Never per person or per agent; every figure
    that rests on fewer than ``min_group_size`` items is replaced by ``suppressed``; cost is self-reported."""
    k = cfg["min_group_size"]
    prog = progress(snap, cfg, room_id, now, quorum, keep_rows=True)
    rows = prog.pop("_rows")
    nodes, lanes = snap["nodes"], snap["lanes"]
    full = {t["task_id"]: t for t in rows}
    # cycle time: first sight of the task -> first counted completion evidence (server receipt times)
    cycle = []
    for n in nodes.values():
        t = full.get(n["id"])
        if n["node_type"] != "task" or not t or t["stage"] != "complete":
            continue
        a = epoch((lanes.get(n["id"]) or {}).get("first_at"))
        b = t["evidence_at"]
        if a is not None and b is not None and b >= a:
            cycle.append(b - a)
    # time blocked / idle: share of the active time of task and attempt lanes
    blocked = idle = total = 0.0
    contributing = 0
    rework_num = rework_den = 0
    for nid, lane in lanes.items():
        if nodes.get(nid, {}).get("node_type") not in TASK_LIKE:
            continue
        spans = lane["spans"]
        active = [s for s in spans if not s["terminal"]]
        if active:
            contributing += 1
            for s in active:
                d = _span_seconds(s, now)
                total += d
                if s["state"] in BLOCKED_STATES:
                    blocked += d
                elif s["state"] in IDLE_STATES:
                    idle += d
        seen_terminal = False
        reopened = False
        for s in spans:
            if s["terminal"]:
                seen_terminal = True
            elif seen_terminal:
                reopened = True
        if seen_terminal:
            rework_den += 1
            rework_num += 1 if reopened else 0
    # re-review: pull requests whose reviews were bound to a newer head after an earlier review
    reviewed, rereviewed = set(), set()
    for m in snap["markers"]:
        if m["edge_type"] == "reviewed" and nodes.get(m["to_id"], {}).get("node_type") == "pr" and m["kind"] != "edge_removed":
            reviewed.add(m["to_id"])
            if m["kind"] == "head_change":
                rereviewed.add(m["to_id"])
    # forks: of the decided approaches, how many were the one selected
    won = lost = 0
    for n in nodes.values():
        if n["node_type"] == "approach":
            won += 1 if n.get("state") in SELECTED_STATES else 0
            lost += 1 if n.get("state") in LOST_STATES else 0
    # cost: self-reported by the emitter that owns the node, so only totals, never a per-agent figure
    spend, tokens, reporters, eligible = 0.0, 0, 0, 0
    for n in nodes.values():
        if n["node_type"] in TASK_LIKE:
            eligible += 1
            a = n.get("attrs") or {}
            c, t = a.get("cost_usd"), a.get("tokens")
            if isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 1e9:
                spend += c
                reporters += 1
            if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= t <= 1e15:
                tokens += int(t)
    mix: dict[str, int] = {}
    for n in nodes.values():
        if n["node_type"] == "task":
            s = clip(n.get("state"), 32) or "unknown"
            mix[s] = mix.get(s, 0) + 1
    by_mission = []
    small = []
    for g in prog["milestones"]:
        (by_mission if g["tasks"] >= k else small).append(g)
    return {
        "scope": {"room_id": room_id, "reader_view_only": True, "per_person_metrics": False,
                  "min_group_size": k, "note": "Computed only from items you can see now. Nothing is broken down by person or agent."},
        "as_of": snap["as_of"], "window": snap["range"], "truncated": snap["reason"], "timeline_truncated": snap["timeline_reason"],
        "cycle_time": {"basis": "first sighting of a task to its first completion evidence, server receipt times",
                       **_durations(cycle, k)},
        "rework": {"basis": "tasks that returned to an active state after reaching a terminal state", **_rate(rework_num, rework_den, k)},
        "rereview": {"basis": "pull requests whose review was bound to a newer head after an earlier review",
                     **_rate(len(rereviewed), len(reviewed), k)},
        "fork_win_rate": {"basis": "selected approaches among approaches that were selected or lost", **_rate(won, won + lost, k)},
        "blocked_time": {"basis": "share of the active time of task and attempt lanes spent in a blocked/waiting state",
                         **(_rate_float(blocked, total, contributing, k))},
        "idle_time": {"basis": "share of the active time spent in an idle/paused state", **(_rate_float(idle, total, contributing, k))},
        "completion": {"complete": prog["room"]["complete"], "of": prog["room"]["tasks"], "status": prog["room"]["status"]},
        "state_mix": _merge_small(mix, k),
        "milestones": [{"milestone": g["milestone"], "label": g["label"], "tasks": g["tasks"], "complete": g["complete"],
                        "completion": g["completion"]} for g in by_mission],
        "milestones_small_groups_hidden": bool(small),
        "cost": ({"self_reported": True, "currency": "USD", "reporting_nodes": reporters, "of_nodes": eligible,
                  "usd": round(spend, 4), "tokens": tokens} if reporters >= k else
                 ({**suppressed(k), "self_reported": True} if reporters else {"self_reported": True, "reported": False})),
    }


def _rate_float(num: float, den: float, contributing: int, k: int) -> dict:
    if contributing < k or den <= 0:
        return suppressed(k)
    return {"share": round(num / den, 4), "lanes": contributing}


# ----------------------------------------------------------------------------------------------- graph-derived attention items
def _state_began(lane: dict | None, states: frozenset) -> float | None:
    """When the node's CURRENT run in one of ``states`` began (server receipt time), or None when unknown."""
    spans = (lane or {}).get("spans") or ()
    return epoch(spans[-1]["start"]) if spans and spans[-1]["state"] in states else None


def graph_items(snap: dict, cfg: dict, now: float, quorum=None) -> tuple[list[dict], dict]:
    """Attention items the room graph implies for this reader: blocked work, failed CI, review requests and stale
    heads (T06), and agents or tasks that are stuck or expired. Each has a ``key`` (what it is about) and an ``fp``
    (the state of the condition), so an acknowledgement of the old condition does not hide a changed one."""
    nodes, lanes = snap["nodes"], snap["lanes"]
    items: list[dict] = []
    budget = {"prs_evaluated": 0, "prs_skipped": 0}

    def add(category, severity, nid, n, *, since=None, fp_extra=(), detail=None, expires=None):
        items.append({"key": f"{category}|{nid}", "category": category, "severity": severity,
                      "subject": {"node_id": nid, "node_type": n.get("node_type"), "label": clip(n.get("label"))},
                      "since": iso(since) if isinstance(since, float) else since, "for_you": False,
                      "fp": fingerprint(category, nid, n.get("state"), *fp_extra),
                      "detail": detail, "expires_at": expires})
    for nid in sorted(nodes):
        n = nodes[nid]
        t, st = n["node_type"], n.get("state")
        lane = lanes.get(nid)
        last = epoch(lane["last_at"]) if lane else None
        if t in ("task", "attempt", "agent", "issue") and st in BLOCKED_STATES:
            began = _state_began(lane, BLOCKED_STATES)
            sev = "warning" if began is not None and now - began >= cfg["blocked_escalate_seconds"] else "notice"
            add("blocked", sev, nid, n, since=began, fp_extra=(began,))
        elif t == "ci" and st in CI_FAILED and (last is None or now - last <= cfg["ci_failed_window_seconds"]):
            began = _state_began(lane, CI_FAILED)           # a node that failed, was fixed and failed AGAIN is a new condition
            add("ci_failed", "error", nid, n, since=began if began is not None else last, fp_extra=(began,))
        elif st in STUCK_STATES and t in ("task", "attempt", "agent", "session", "gate"):
            add("expired_or_stuck", "warning", nid, n, since=last, detail={"reported_state": clip(st, 32)})
        elif t in ("attempt", "agent", "session") and st in ACTIVE_STATES and last is not None \
                and now - last >= cfg["stuck_after_seconds"]:
            add("expired_or_stuck", "warning", nid, n, since=last, fp_extra=(last,),
                detail={"silent_for_seconds": int(now - last), "basis": "no event for this node in the room ledger"})
        elif t == "pr" and st not in ("merged", "closed"):
            if quorum is None:
                continue
            if budget["prs_evaluated"] >= cfg["max_prs"]:
                budget["prs_skipped"] += 1
                continue
            budget["prs_evaluated"] += 1
            q = quorum(nid)
            if q is None:
                continue
            status = q.get("status")
            if status == "satisfied" and len(q.get("counted") or ()) < (q.get("required") or 1):
                status = "insufficient"        # T06 decides over ALL reviews; this reader is told only what the visible ones add up to
            extra = (q.get("head"), status, tuple(q.get("counted") or ()), tuple(q.get("stale_reviewers") or ()))
            detail = {"quorum_status": status, "head": q.get("head"), "required": q.get("required"),
                      "approvals": len(q.get("counted") or ())}
            if status == "blocked":
                if q.get("blocked_by_hidden"):
                    detail["blocked_by_hidden"] = True       # T06 discloses that a hidden block exists, never whose
                add("review_blocked", "error", nid, n, fp_extra=extra, detail=detail)
            elif status == "head_unverified":
                add("stale_head", "warning", nid, n, fp_extra=extra, detail={**detail, "alert": clip(q.get("alert"), 300)})
            elif status == "insufficient":
                if q.get("stale_reviewers"):
                    add("rereview_required", "warning", nid, n, fp_extra=extra,
                        detail={**detail, "stale_reviewers": len(q["stale_reviewers"])})
                else:
                    add("review_requested", "notice", nid, n, fp_extra=extra, detail=detail)
            elif status in ("unknown_head", "unknown_author", "invalid_target"):
                add("review_unverifiable", "info", nid, n, fp_extra=extra, detail=detail)
    return items, budget


def order_items(items: list[dict]) -> list[dict]:
    """The documented priority rule: severity, then items addressed to this reader, then the oldest first."""
    return sorted(items, key=lambda i: (-SEVERITY_RANK[i["severity"]], not i["for_you"], i["since"] or "~", i["key"]))
