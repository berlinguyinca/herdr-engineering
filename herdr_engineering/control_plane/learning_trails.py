"""Durable pull-request trails for the learning loop.

When a pull request ends (merged or closed, with a merge/close time on its record) one bounded ``PullRequestTrail`` event is written
on the ``pr_trail`` stream of that PR, assembled from what the control plane already holds (the PR trail of ``pr_trail`` and, for each
evidence-linked agent, the ``learning_record`` of ``agent_evolution``). It survives the 24 h live window, so a training pipeline can pull
it later. Rules, the same as the two builders it reads:

* Every figure is ``{"value": ..., "reason": ...}``; a source that could not be read, was truncated or is undated leaves ``null`` and
  the reason. Nothing is estimated here, and nothing is added that the builders did not already redact and cap.
* One record per end (``end_seq``, the PR record's ``end_count``): a PR that ends again after being reopened gets a new record, a
  corrected end time does not; a replay or a second sweep
  writes nothing. A record is never rewritten. The only later change is an explicit ``PullRequestTrailLateEvidence`` event (for
  example, the provider's own merge time arriving after this server had only observed one); the export lists it as its own line.
* Only PRs whose record carries a merge/close time are assembled: without one the record could not be dated and its signals would
  rest on nothing. Hints (agents that only share the PR's mission) are not agents of the record.
* Assembly is a sweep (every maintenance pass, and lazily before an export read), never inside the PR upsert: it walks every session
  and handoff, which must not run under the event lock of an ingest.

``export`` pages the records oldest first by ``learning_seq``, a counter this module keeps, so a cursor stays valid while records
are appended.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading

from .project_links import canonical_repository
from .redact import redact_text

EVENT = "PullRequestTrail"
LATE = "PullRequestTrailLateEvidence"
ENTITY = "pr_trail"
SCHEMA = 1
MAX_BYTES = 131072
_SWEEP_EVERY = 300
_RETRY_AFTER = 3600
_TEXT = 500
_ENDED = frozenset(("merged", "closed"))
_LATE_KEYS = frozenset(("merged_at", "closed_at", "ended_source", "note"))
_CURSOR = re.compile(r"^s[0-9]{1,15}$")


def learning_config(raw):
    """Validate ``control_plane.learning`` (YAML only); raises ValueError naming the key."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("control_plane.learning must be a mapping")
    defaults = {"enabled": True, "max_agents": 12, "sweep_batch": 20, "page_default": 50, "page_max": 200}
    extra = sorted(set(raw) - set(defaults))
    if extra:
        raise ValueError(f"control_plane.learning has unknown keys: {', '.join(extra)}")
    out = {**defaults, **raw}
    if not isinstance(out["enabled"], bool):
        raise ValueError("control_plane.learning.enabled must be true or false")
    for key, low, high in (("max_agents", 1, 50), ("sweep_batch", 1, 100), ("page_default", 1, 500), ("page_max", 1, 500)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"control_plane.learning.{key} must be a whole number from {low} to {high}")
    if out["page_default"] > out["page_max"]:
        raise ValueError("control_plane.learning.page_default must not exceed page_max")
    return out


def _parse(stamp):
    try:
        value = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=dt.UTC)).timestamp()


def _clean(value, depth=0):
    """Every string through the redactor and a length cap, every container bounded in depth: defence in depth on top of the builders."""
    if depth > 8:
        return None
    if isinstance(value, str):
        return redact_text(value)[:_TEXT]
    if isinstance(value, dict):
        return {str(k)[:80]: _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v, depth + 1) for v in value]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:_TEXT]


def parse_repository(value):
    """``(host|None, owner, name)`` of a PR record's repository, or None when it is not ``owner/name`` or ``host/owner/name``."""
    if not isinstance(value, str):
        return None
    canonical = canonical_repository(value)
    parts = (canonical or value.strip()).strip("/").split("/")
    if canonical and len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if not canonical and len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", p) for p in parts):
        return None, parts[0], parts[1]
    return None


class TrailRecords:
    """Assembles, stores and exports pull-request trails. ``trail_for(host, owner, name, number)`` returns a ``pr_trail`` body (or None)
    and ``evolution_for(session_id)`` an ``agent_evolution`` body (or None); both are supplied by the server."""

    def __init__(self, repo, config, *, trail_for=None, evolution_for=None, now=None):
        self.repo, self.config = repo, config
        self.trail_for, self.evolution_for = trail_for, evolution_for
        self._now = now or repo._now
        self._lock = threading.Lock()
        self._swept = 0.0
        self._seq = None
        self._retry: dict = {}

    @property
    def enabled(self):
        return bool(self.config["enabled"])

    # ------------------------------------------------------------------ read model
    def _next_seq(self):
        if self._seq is None:
            top = -1
            for pr_id in self.repo.entity_ids(ENTITY):
                for event in self.repo.stream(ENTITY, pr_id):
                    seq = (event.get("payload") or {}).get("learning_seq")
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        top = max(top, seq)
            self._seq = top
        self._seq += 1
        return self._seq

    def _events(self, pr_id, event_type):
        return [e for e in self.repo.stream(ENTITY, pr_id) if e.get("event_type") == event_type]

    def _record_for(self, pr_id, end_seq):
        for event in self._events(pr_id, EVENT):
            if event["payload"].get("end_seq") == end_seq:
                return event["payload"]
        return None

    # ------------------------------------------------------------------ assembly
    def _agents(self, trail):
        sessions = [s for s in (trail.get("sessions") or []) if isinstance(s, dict) and s.get("session_id")]
        limit = int(self.config["max_agents"])
        if self.evolution_for is None:
            return None, {"state": "unavailable", "reason": "agent evolution is not wired"}, len(sessions)
        agents, unreadable = [], 0
        for session in sessions[:limit]:
            try:
                body = self.evolution_for(session["session_id"])
            except Exception:  # noqa: BLE001 - one unreadable agent must not hide the rest
                body = None
            record = (body or {}).get("learning_record")
            if not isinstance(record, dict):
                unreadable += 1
                agents.append({"session_id": session["session_id"], "rule": session.get("rule"), "learning_record": None,
                               "reason": "the agent's evolution could not be read"})
                continue
            # the PR-wide signals are on the record's own ``signals``; repeating them per agent would multiply the size
            agents.append({"session_id": session["session_id"], "rule": session.get("rule"),
                           "learning_record": {k: v for k, v in record.items() if k != "prs"}})
        omitted = max(0, len(sessions) - limit)
        state = "ok" if not omitted and not unreadable else "partial"
        reason = None if state == "ok" else (f"{omitted} agents were not assembled (control_plane.learning.max_agents)" if omitted
                                             else f"{unreadable} agents could not be read")
        return agents, {"state": state, "reason": reason}, omitted

    def assemble(self, pr):
        """The record for one ended pull request, or None when its trail cannot be built now (retried later)."""
        parsed = parse_repository(pr.get("repository"))
        number = pr.get("number")
        if parsed is None or not isinstance(number, int) or isinstance(number, bool) or self.trail_for is None:
            return None
        host, owner, name = parsed
        try:
            trail = self.trail_for(host, owner, name, number)
        except Exception:  # noqa: BLE001
            trail = None
        if not isinstance(trail, dict):
            return None
        agents, agents_source, agents_omitted = self._agents(trail)
        ended_at = pr.get("merged_at") or pr.get("closed_at")
        view = trail.get("pull_request") or {}
        out = {
            "schema_version": SCHEMA, "kind": "pull_request_trail", "assembled_at": self.repo._stamp(),
            "pr_id": pr.get("pr_id"), "end_seq": pr.get("end_count") or 1, "ended_at": ended_at, "ended_source": pr.get("ended_source"),
            "pull_request": {"host": host, "repository": f"{owner}/{name}", "number": number, "status": pr.get("status"),
                             "url": view.get("url") or pr.get("url"), "branch": view.get("branch") or pr.get("branch"),
                             "base": pr.get("base"), "opened_at": view.get("opened_at"),
                             "merged_at": pr.get("merged_at"), "closed_at": pr.get("closed_at")},
            "state": trail.get("state"), "reason": trail.get("reason"),
            "sources": {**(trail.get("sources") or {}), "agent_evolution": agents_source},
            "omitted": {**(trail.get("omitted") or {}), "agents": agents_omitted},
            "signals": trail.get("signals"),
            "sessions": trail.get("sessions"), "ambiguous": trail.get("ambiguous"),
            "inferred_sessions_count": len(trail.get("inferred_sessions") or []),
            "handoffs": trail.get("handoffs"), "reviews": trail.get("reviews"), "ci_runs": trail.get("ci_runs"),
            "agents": agents,
        }
        out = _clean(out)
        trimmed = []
        for field in ("ci_runs", "handoffs", "reviews"):
            if len(json.dumps(out, default=str)) <= MAX_BYTES:
                break
            out[field] = None
            trimmed.append(field)
        out["trimmed"] = trimmed
        return out

    def _write(self, pr):
        payload = self.assemble(pr)
        if payload is None:
            return None
        payload["learning_seq"] = self._next_seq()
        self.repo.record_event(ENTITY, pr["pr_id"], EVENT, payload, feed=False)
        return payload

    # ------------------------------------------------------------------ sweep
    def sweep(self, *, force=False):
        """Assemble the records that are due (at most ``sweep_batch``), then note a provider-reported end time that corrects an observed one.
        Throttled; safe to call on reads."""
        if not self.enabled:
            return 0
        now = self._now()
        if not force and now - self._swept < _SWEEP_EVERY:
            return 0
        self._swept = now
        written = 0
        with self._lock:
            for pr in self.repo.list_pull_requests():
                if str(pr.get("status") or "").lower() not in _ENDED:
                    continue
                ended_at = pr.get("merged_at") or pr.get("closed_at")
                if not ended_at or not isinstance(pr.get("pr_id"), str):
                    continue
                if self._record_for(pr["pr_id"], pr.get("end_count") or 1) is not None:
                    self._late_from_record(pr)
                    continue
                if self._retry.get(pr["pr_id"], 0) > now or written >= int(self.config["sweep_batch"]):
                    continue
                if self._write(pr) is None:
                    self._retry[pr["pr_id"]] = now + _RETRY_AFTER
                    continue
                written += 1
        return written

    def _late_from_record(self, pr):
        """A provider-reported end time that differs from the one the record was assembled with becomes one late-evidence event."""
        if pr.get("ended_source") != "reported":
            return
        record = self._record_for(pr["pr_id"], pr.get("end_count") or 1)
        if record is None:
            return
        if record.get("ended_source") != "observed":
            return
        reported = pr.get("merged_at") or pr.get("closed_at")
        if reported == record.get("ended_at"):
            return
        if any((e["payload"].get("evidence") or {}).get("ended_at_reported") == reported for e in self._events(pr["pr_id"], LATE)):
            return
        self.late_evidence(pr["pr_id"], record["end_seq"], {"merged_at": pr.get("merged_at"), "closed_at": pr.get("closed_at"),
                                                           "ended_source": "reported"}, _reported=reported)

    def late_evidence(self, pr_id, end_seq, evidence, *, _reported=None):
        """Append evidence that arrived after a record was written. Only the whitelisted keys are kept; the record itself is untouched."""
        if self._record_for(pr_id, end_seq) is None:
            raise KeyError("no trail record for that end")
        kept = {k: _clean(v) for k, v in (evidence or {}).items() if k in _LATE_KEYS}
        if not kept:
            raise ValueError("no recognised evidence")
        if _reported:
            kept["ended_at_reported"] = _reported
        payload = {"schema_version": SCHEMA, "kind": "pull_request_trail_late_evidence", "pr_id": pr_id, "end_seq": end_seq,
                   "assembled_at": self.repo._stamp(), "evidence": kept, "learning_seq": self._next_seq()}
        self.repo.record_event(ENTITY, pr_id, LATE, payload, feed=False)
        return payload

    # ------------------------------------------------------------------ export
    def export(self, *, since=None, cursor=None, limit=None):
        """One page, oldest first: ``{"records": [...], "next_cursor": str|None, "has_more": bool}``. Raises ValueError for bad parameters."""
        page_max = int(self.config["page_max"])
        if limit is None:
            limit = int(self.config["page_default"])
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= page_max:
            raise ValueError(f"limit must be a whole number from 1 to {page_max}")
        since_ts = None
        if since is not None:
            since_ts = _parse(since)
            if since_ts is None:
                raise ValueError("since must be an ISO 8601 time")
        after = -1
        if cursor is not None:
            if not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor):
                raise ValueError("cursor is not valid")
            after = int(cursor[1:])
        rows = []
        for pr_id in self.repo.entity_ids(ENTITY):
            for event in self.repo.stream(ENTITY, pr_id):
                payload = event.get("payload") or {}
                seq = payload.get("learning_seq")
                if event.get("event_type") not in (EVENT, LATE) or not isinstance(seq, int) or isinstance(seq, bool) or seq <= after:
                    continue
                if since_ts is not None and (_parse(payload.get("assembled_at")) or 0) < since_ts:
                    continue
                rows.append(payload)
        rows.sort(key=lambda p: p["learning_seq"])
        page = [{**p, "cursor": f"s{p['learning_seq']}"} for p in rows[:limit]]
        has_more = len(rows) > limit
        return {"schema_version": SCHEMA, "records": page, "count": len(page), "has_more": has_more,
                "next_cursor": page[-1]["cursor"] if page else (cursor if cursor else None)}


def encode_cursor(seq):
    """A cursor for ``learning_seq`` ``seq`` (exposed for tests and clients that want to resume from a known point)."""
    return f"s{int(seq)}"


__all__ = ["EVENT", "LATE", "ENTITY", "TrailRecords", "learning_config", "parse_repository", "encode_cursor"]
