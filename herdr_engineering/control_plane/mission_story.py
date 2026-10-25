"""One mission's story for the dashboard page (missions rework design 2026-10-24, step 2).

``build`` answers ``{header, needs, events, agents, outcome, usage, related, partial}`` in one call: the
stored ``MissionOutcome`` when the mission has ended, or the same shape assembled live while it runs, so
the page does not stitch five requests together. Everything is bounded, redacted and made of structured
fields; link targets are ``ref`` objects the page turns into links, never URLs from text.
"""
from __future__ import annotations

import datetime as dt

from .mission_outcome import TERMINAL, _parse, _text, assemble, last_outcome, mission_key
from .relations import related

EXPECTED_SOURCES = ("mission", "sessions", "tests", "reviews", "prs", "ci", "usage")
_RESULT = {"complete": "succeeded", "failed": "failed", "cancelled": "cancelled", "unknown": "unknown"}
_NEEDS_KINDS = ("mission_attention", "mission_failed", "agent_failed", "agent_blocked")


def _iso(epoch):
    return None if epoch is None else dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat()


def _ev(ts, kind, text, ref=None, **extra):
    epoch = _parse(ts)
    if epoch is None:
        return None
    out = {"ts": _iso(epoch), "kind": kind, "text": _text(text, 200) or kind}
    if ref:
        out["ref"] = ref
    out.update(extra)
    return out


def _events(repo, key, mission, sessions, outcome_event, live, wait_span):
    out = []
    for event in repo.stream("mission", key):
        p, et = event.get("payload") or {}, event.get("event_type")
        if et == "MissionStarted":
            out.append(_ev(event["source_timestamp"], "start", f"Mission started: {p.get('title') or mission.get('title') or key}"))
        elif et == "MissionStageChanged":
            out.append(_ev(event["source_timestamp"], "stage", f"Stage {p.get('from') or '?'} → {p.get('to') or '?'}"))
        elif et == "MissionOutcome":
            stage = p.get("final_stage")
            text = (f"Closed with no activity for a long time ({stage})" if p.get("inferred") else f"Ended: {stage}")
            out.append(_ev(event["source_timestamp"], "end", text, final_stage=stage))
    for s in sessions:
        label = _text(s.get("display_name") or s.get("agent_role"), 60) or "Agent"
        detail = ", ".join(x for x in (s.get("agent_role"), s.get("model")) if x)
        out.append(_ev(s.get("created_at"), "agent", f"Started: {label}" + (f" ({_text(detail, 80)})" if detail else ""),
                       {"kind": "session", "id": s["session_id"]}))
        if str(s.get("status")).lower() in ("failed", "finished", "complete", "completed", "stopped", "cancelled", "crashed", "closed"):
            out.append(_ev(s.get("updated_at"), "agent", f"{label} {str(s.get('status')).lower()}", {"kind": "session", "id": s["session_id"]}))
    for r in repo.list_test_runs(mission_id=key):
        counts = f" ({r['failed']} failed)" if r.get("failed") else (f" ({r['passed']} passed)" if r.get("passed") else "")
        out.append(_ev(r.get("started_at") or r.get("created_at"), "tests", f"Tests {_text(str(r.get('status')), 30)}{counts}"))
    for r in repo.list_reviews(mission_id=key):
        out.append(_ev(r.get("started_at") or r.get("created_at"), "review", f"Review by {_text(r.get('reviewer'), 60) or 'reviewer'}: {_text(str(r.get('verdict') or r.get('status') or 'open'), 40)}",
                       {"kind": "review", "id": r["review_id"]}))
    for p in repo.list_pull_requests(mission_id=key):
        ref = {"kind": "pr", "id": p["pr_id"], "repository": _text(p.get("repository"), 120), "number": p.get("number")}
        out.append(_ev(p.get("created_at"), "pr", f"PR #{p.get('number')} opened", ref))
        if p.get("status") == "merged":
            out.append(_ev(p.get("updated_at"), "pr", f"PR #{p.get('number')} merged", ref))
    try:
        from .relations import _repo_match
        for row in repo.ci_reports()["pipelines"]:
            if any(_repo_match(s, row) for s in sessions):
                ts = row.get("finished_at") or row.get("updated_at") or row.get("started_at") or row.get("created_at")
                out.append(_ev(ts, "ci", f"CI run #{row.get('run_number')} {_text(str(row.get('state') or row.get('status') or 'unknown'), 30)} on {_text(row.get('branch'), 80) or 'a branch'}",
                               {"kind": "ci", "id": f"{row.get('provider_repo_id') or row.get('repository')}:{row.get('run_number')}",
                                "repository": _text(row.get("repository"), 120), "run_number": row.get("run_number"),
                                "repo_filter": _text(str(row.get("provider_repo_id") or row.get("repository") or ""), 200)}))
    except Exception:  # noqa: BLE001 - named in partial by the outcome's sources
        pass
    if live:
        activity = live.get("activity") or {}
        since, reason = _parse(activity.get("waitingSince")), _text(activity.get("waitingReason"), 120)
        if reason and since is not None:
            span = max(0, (_parse(live.get("received_at")) or since) - since)
            if span >= wait_span:
                out.append(_ev(since, "wait", f"Waiting: {reason}", span_s=int(span)))
    return [e for e in out if e is not None]


def build(repo, key, *, outcomes, inbox=None, config=None):
    """The story, or ``None`` for a mission the control plane has no record of."""
    mission = repo.get_mission(key)
    if mission is None:
        return None
    key = mission_key(mission)
    story_cfg = (config or {}).get("story") or {"max_events": 200, "wait_span_seconds": 300}
    max_agents = int(outcomes.config["outcomes"]["max_agents"]) if outcomes else 12
    stage = mission.get("stage")
    ended = stage in TERMINAL
    stored = last_outcome(repo, key)
    # An outcome older than the latest stage change belongs to an earlier run of a reopened mission.
    if stored is not None and not ended:
        stored = None
    recorded = stored is not None
    payload = stored["payload"] if recorded else assemble(repo, key, max_agents=max_agents, final_stage=stage)
    partial = [s for s in EXPECTED_SOURCES if s not in (payload.get("sources") or [])]
    sessions = [s for s in repo.list_sessions() if s.get("mission_key") == key]
    live = None
    try:
        live = repo.mission_activity.get(key)
    except Exception:  # noqa: BLE001
        live = None

    final = payload.get("final_stage") if ended or recorded else None
    result = _RESULT.get(final, "unknown") if ended else "running"
    started = next((_parse(e["source_timestamp"]) for e in repo.stream("mission", key) if e.get("event_type") == "MissionStarted"), None)
    started = started if started is not None else _parse(mission.get("created_at"))
    duration = payload.get("duration_s")
    if duration is None and not ended and started is not None:
        duration = max(0, int(repo._now() - started))
    tests, prs, ci = payload.get("tests"), payload.get("prs") or [], payload.get("ci") or []
    usage = payload.get("usage")
    header = {
        "key": key, "title": _text(mission.get("title"), 180), "stage": stage, "result": result, "source": mission.get("source") or "herdr",
        "repository": payload.get("repository") or next((s.get("repository_id") for s in sessions if s.get("repository_id")), None),
        "branch": next((s.get("branch") for s in sessions if s.get("branch")), None),
        "hosts": sorted({s["host_id"] for s in sessions if s.get("host_id")})[:3],
        "started_at": _iso(started), "ended_at": payload.get("ended_at") if ended else None, "duration_s": duration,
        "agent_count": payload.get("agent_count", len(sessions)),
        "tokens_out": usage.get("output_tokens") if usage else None,
        "inferred": bool(payload.get("inferred")) if ended else False,
        "recorded_before_outcomes": ended and not recorded,
        "summary": {"pr": ({"number": prs[-1].get("number"), "state": prs[-1].get("state")} if prs else None),
                    "ci": (ci[0].get("state") if ci else None),
                    "tests": ({"runs": tests["runs"], "passed": tests["passed"], "failed": tests["failed"]} if tests else None)},
    }
    needs, needs_partial = [], False
    if inbox is not None:
        try:
            ids = {s["session_id"] for s in sessions}
            items, _, unavailable = inbox.derive()
            needs_partial = bool(unavailable)
            for item in items:
                if item["state"] != "open" or item["kind"] not in _NEEDS_KINDS:
                    continue
                k = item["key"]
                if k == f"mission:{key}" or k.split(":", 1)[-1] in ids and k.split(":", 1)[0] in ("session", "blocked"):
                    needs.append({"kind": item["kind"], "severity": item["severity"], "title": item["title"], "detail": item["detail"],
                                  "href": item["href"], "key": k, "fingerprint": item["fingerprint"]})
        except Exception:  # noqa: BLE001
            needs_partial = True
    else:
        needs_partial = True
    if needs_partial:
        partial.append("attention")

    events = sorted(_events(repo, key, mission, sessions, stored, live, float(story_cfg["wait_span_seconds"])), key=lambda e: (e["ts"], e["kind"]))
    cap = int(story_cfg["max_events"])
    more = max(0, len(events) - cap)
    events = events[-cap:] if more else events
    try:
        answer = related(repo, "mission", key, now_llm=False) or {}
        relations = answer.get("relations") or {}
        partial += [f"related:{p}" for p in answer.get("partial", [])]
    except Exception:  # noqa: BLE001
        relations = {}
        partial.append("related")
    objective = payload.get("objective")
    return {"header": header, "needs": needs, "events": events, "more": more, "agents": payload.get("agents"),
            "outcome": {"recorded": recorded, "live": not ended, **{k: v for k, v in payload.items() if k not in ("agents", "usage")}},
            "usage": usage, "objective": objective, "related": relations, "partial": sorted(set(partial))}
