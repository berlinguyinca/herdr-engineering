"""Durable mission outcome (missions rework design 2026-10-24, step 1).

When a mission ends, one bounded ``MissionOutcome`` event is written on its stream, assembled from
what the control plane already holds, so the story survives the 24 h live window. A field a source
could not supply is ``None`` and ``sources`` lists what was read; counts are never invented. A
mission that never reports an end is closed after ``stale_close_hours`` as ``unknown`` and marked
inferred, never as success.
"""
from __future__ import annotations

import datetime as dt
import json
import threading

from .redact import redact_text
from .relations import _repo_match

EVENT = "MissionOutcome"
SCHEMA = 1
TERMINAL = frozenset(("complete", "failed", "cancelled"))
MAX_BYTES = 16384
_TEXT = 300
_SWEEP_EVERY = 300
_PASSED = frozenset(("passed", "success", "succeeded", "ok"))
_FAILED = frozenset(("failed", "failure", "error", "errored"))
_ENDED = frozenset(("failed", "finished", "complete", "completed", "stopped", "cancelled", "canceled", "crashed", "closed"))
_CHANGES = frozenset(("changes_requested", "request_changes", "rejected", "denied"))


def missions_config(raw):
    """Validate ``control_plane.missions`` (YAML only); raises ValueError naming the key."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("control_plane.missions must be a mapping")
    unknown = sorted(set(raw) - {"outcomes", "story"})
    if unknown:
        raise ValueError(f"control_plane.missions has unknown keys: {', '.join(unknown)}")

    def section(name, defaults):
        value = raw.get(name)
        if value is None:
            return dict(defaults)
        if not isinstance(value, dict):
            raise ValueError(f"control_plane.missions.{name} must be a mapping")
        extra = sorted(set(value) - set(defaults))
        if extra:
            raise ValueError(f"control_plane.missions.{name} has unknown keys: {', '.join(extra)}")
        return {**defaults, **value}

    def number(where, value, low, high):
        if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
            raise ValueError(f"control_plane.missions.{where} must be a number from {low} to {high}")
        return value

    outcomes = section("outcomes", {"enabled": True, "stale_close_hours": 72, "max_agents": 12})
    if not isinstance(outcomes["enabled"], bool):
        raise ValueError("control_plane.missions.outcomes.enabled must be true or false")
    number("outcomes.stale_close_hours", outcomes["stale_close_hours"], 1, 24 * 365)
    number("outcomes.max_agents", outcomes["max_agents"], 1, 50)
    story = section("story", {"max_events": 200, "wait_span_seconds": 300})
    number("story.max_events", story["max_events"], 10, 1000)
    number("story.wait_span_seconds", story["wait_span_seconds"], 30, 86400)
    return {"outcomes": outcomes, "story": story}


def _text(value, limit=_TEXT):
    if not isinstance(value, str):
        return None
    value = redact_text("".join(ch for ch in value if ch.isprintable() or ch == " ")).strip()
    return value[:limit] or None


def _parse(stamp):
    if isinstance(stamp, int | float) and not isinstance(stamp, bool):
        return float(stamp)
    try:
        return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def mission_key(mission):
    return mission.get("mission_key") or mission["mission_id"]


def last_outcome(repo, key):
    for event in reversed(repo.stream("mission", key)):
        if event.get("event_type") == EVENT:
            return event
    return None


def _last_stage_event(repo, key):
    for event in reversed(repo.stream("mission", key)):
        if event.get("event_type") in ("MissionStageChanged", "MissionUpdated", "MissionStarted"):
            return event
    return None


def assemble(repo, key, *, max_agents=12, final_stage=None, ended_at=None, inferred=False):
    """The outcome payload for ``key`` from the data held now. Never raises: a source that fails is left out."""
    sources, mission = [], repo.get_mission(key)
    if mission is None:
        return None
    sources.append("mission")
    started = None
    for event in repo.stream("mission", key):
        if event.get("event_type") == "MissionStarted":
            started = _parse(event.get("source_timestamp"))
            break
    started = started if started is not None else _parse(mission.get("created_at"))
    end = _parse(ended_at) if ended_at else _parse(repo._stamp())
    out = {
        "schema": SCHEMA,
        "ended_at": ended_at or repo._stamp(),
        "duration_s": int(end - started) if started is not None and end is not None and end >= started and not inferred else None,
        "final_stage": final_stage or mission.get("stage"),
        "inferred": bool(inferred),
        "objective": None, "last_activity": None,
        "agents": None, "tests": None, "ci": None, "reviews": None, "prs": None, "usage": None,
        "title": _text(mission.get("title")), "repository": _text(mission.get("repository_id"), 120),
    }
    live = None
    try:
        live = repo.mission_activity.get(key)
    except Exception:  # noqa: BLE001 - one unreadable source must not hide the others
        live = None
    if live:
        sources.append("live")
        out["objective"] = _text((live.get("activity") or {}).get("currentObjective") or (live.get("mission") or {}).get("goal"))
        out["last_activity"] = _text((live.get("activity") or {}).get("currentActivity"))
    out["objective"] = out["objective"] or _text(mission.get("purpose")) or _text(mission.get("title"))

    sessions = []
    try:
        sessions = [s for s in repo.list_sessions() if s.get("mission_key") == key]
        summaries = {e["session_id"]: e["summary"] for e in repo.agent_summary_events()}
        agents = []
        for s in sessions[:max_agents]:
            summary = summaries.get(s["session_id"]) or {}
            status = s.get("status")
            agents.append({
                "session_id": s["session_id"], "label": _text(s.get("display_name") or s.get("agent_role"), 80),
                "role": _text(s.get("agent_role"), 40), "model": _text(s.get("model"), 80),
                "started_at": s.get("created_at"),
                "ended_at": s.get("updated_at") if str(status).lower() in _ENDED else None,
                "outcome": _text(status, 40), "summary": _text(summary.get("overview"), 200)})
        out["agents"] = agents
        out["agent_count"] = len(sessions)
        sources.append("sessions")
    except Exception:  # noqa: BLE001
        pass

    try:
        runs = repo.list_test_runs(mission_id=key)
        out["tests"] = {"runs": len(runs),
                        "passed": sum(1 for r in runs if str(r.get("status")).lower() in _PASSED),
                        "failed": sum(1 for r in runs if str(r.get("status")).lower() in _FAILED)}
        sources.append("tests")
    except Exception:  # noqa: BLE001
        pass
    try:
        reviews = repo.list_reviews(mission_id=key)
        verdict = lambda r: str(r.get("verdict") or "").lower()  # noqa: E731
        out["reviews"] = {"open": sum(1 for r in reviews if not verdict(r)),
                          "approved": sum(1 for r in reviews if verdict(r) == "approved"),
                          "changes_requested": sum(1 for r in reviews if verdict(r) in _CHANGES)}
        sources.append("reviews")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["prs"] = [{"repository": _text(p.get("repository"), 120), "number": p.get("number"), "state": _text(p.get("status"), 40),
                       "url": p.get("url") if isinstance(p.get("url"), str) and p["url"].startswith(("https://", "http://")) else None}
                      for p in repo.list_pull_requests(mission_id=key)[:5]]
        sources.append("prs")
    except Exception:  # noqa: BLE001
        pass
    try:
        latest = {}
        for row in repo.ci_reports()["pipelines"]:
            if any(_repo_match(s, row) for s in sessions):
                k = (row.get("repository"), row.get("branch"))
                if k not in latest or (row.get("run_number") or 0) > (latest[k].get("run_number") or 0):
                    latest[k] = row
        out["ci"] = [{"repository": _text(r.get("repository"), 120), "branch": _text(r.get("branch"), 120),
                      "run": r.get("run_number"), "state": _text(r.get("state") or r.get("status"), 40)}
                     for r in list(latest.values())[:5]]
        sources.append("ci")
    except Exception:  # noqa: BLE001
        pass
    try:
        groups = repo.llm_stats("30d", "all", mission=key, source="llm-proxy")
        group = groups["groups"][0] if groups["groups"] else None
        tokens = (group or {}).get("tokens") or {}
        out["usage"] = {"requests": group["requests"] if group else 0, "input_tokens": tokens.get("input", 0),
                        "output_tokens": tokens.get("output", 0),
                        "lower_bound": bool(tokens.get("lower_bound") or groups.get("partial"))}
        sources.append("usage")
    except Exception:  # noqa: BLE001
        pass
    out["sources"] = sources
    if len(json.dumps(out, default=str)) > MAX_BYTES and out["agents"]:
        for agent in out["agents"]:
            agent["summary"] = None
    return out


class MissionOutcomes:
    """Writes the outcome when a mission ends, and closes missions that went silent."""

    def __init__(self, repo, config, *, now=None):
        self.repo, self.config = repo, config
        self._now = now or repo._now
        self._lock = threading.Lock()
        self._swept = 0.0

    @property
    def enabled(self):
        return bool(self.config["outcomes"]["enabled"])

    def on_stage(self, key, old, new):
        """Called after a stage change; writes at most one outcome per transition into a terminal stage."""
        if not self.enabled or new not in TERMINAL or old in TERMINAL or old == new:
            return None
        return self._write(key, final_stage=new)

    def _write(self, key, **kw):
        with self._lock:
            payload = assemble(self.repo, key, max_agents=int(self.config["outcomes"]["max_agents"]), **kw)
            if payload is None:
                return None
            mission = self.repo.get_mission(key)
            self.repo.record_event("mission", key, EVENT, payload, mission_id=mission["mission_id"], feed=False)
            return payload

    def sweep(self, *, force=False):
        """Close missions with no activity for ``stale_close_hours`` as unknown. Throttled; safe to call on reads."""
        if not self.enabled:
            return 0
        now = self._now()
        if not force and now - self._swept < _SWEEP_EVERY:
            return 0
        self._swept = now
        limit = float(self.config["outcomes"]["stale_close_hours"]) * 3600
        closed = 0
        sessions = None
        for mission in self.repo.list_missions():
            key = mission_key(mission)
            if mission.get("stage") in TERMINAL:
                continue
            stage_event = _last_stage_event(self.repo, key)
            outcome = last_outcome(self.repo, key)
            if outcome is not None and (stage_event is None or (stage_event["sequence"] < outcome["sequence"])):
                continue   # already closed since its last change
            if sessions is None:
                sessions = self.repo.list_sessions()
            stamps = [_parse(mission.get("updated_at")), _parse(mission.get("created_at"))]
            stamps += [_parse(s.get("updated_at")) for s in sessions if s.get("mission_key") == key]
            stamps += [_parse(e.get("source_timestamp")) for e in self.repo.stream("mission", key)[-1:]]
            last = max((s for s in stamps if s is not None), default=None)
            if last is None or now - last < limit:
                continue
            ended = dt.datetime.fromtimestamp(last, dt.UTC).isoformat()
            if self._write(key, final_stage="unknown", ended_at=ended, inferred=True) is not None:
                closed += 1
        return closed
