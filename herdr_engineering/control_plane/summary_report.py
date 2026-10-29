"""On-demand full ``/summary`` report for one dashboard session.

The control plane runs in a container that has neither the agents' session records nor a summarizer
CLI, so it never runs a report itself. The operator's request becomes a durable ``SummaryReportRequested``
event and a queued job; the per-host plugin that owns the session claims it (``claim``), runs
``herdr-eng summary report`` where the records and credentials live, and posts the result (``finish``).
The server validates and redacts the result again before storing it as a session event, so it survives a
restart and is read back through ``GET .../summary``. A request nobody claims or finishes in time
fails with a stable code instead of hanging. The model-backed summary card is separate and untouched.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import summary_report as runner
from .redact import redact_text

_EVENTS = {"SummaryReportCompleted": "done", "SummaryReportFailed": "failed"}
QUEUE_GRACE_S = 90          # a host that has not claimed a request by now is not running a worker
RUN_GRACE_S = 60            # slack on top of the summarizer timeout before a claimed job is abandoned
# Failure codes a host may report; anything else is stored as summarizer_failed.
HOST_ERRORS = frozenset({"timeout", "harness_missing", "no_session_record", "malformed_report", "summarizer_failed",
                         "evidence_too_large", "invalid_agent", "invalid_session_id", "unsupported_harness"})


class ReportRefused(Exception):
    """A refusal with an HTTP status and a JSON body safe to return to the caller."""

    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status = status
        self.body = {"ok": False, "error": error, **extra}


@dataclass(frozen=True)
class ReportConfig:
    enabled: bool = False
    harness: str = "claude"
    timeout_seconds: int = 300
    max_concurrent: int = 2

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("summary_report enabled must be a boolean")
        if self.harness != "claude":
            # codex is wired in the runner but has never run live here: opt in only after it has.
            raise ValueError("summary_report harness must be claude")
        for name, lower, upper in (("timeout_seconds", 10, 900), ("max_concurrent", 1, 4)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"summary_report {name} must be an integer between {lower} and {upper}")

    @classmethod
    def from_mapping(cls, raw):
        return cls(**dict(raw or {}))


def _payload(event):
    payload = event.get("payload", {})
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class SummaryReportService:
    def __init__(self, repo, config, *, clock=time.time):
        self.repo = repo
        self.config = config if isinstance(config, ReportConfig) else ReportConfig.from_mapping(config)
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs = {}             # session id -> open job (queued or running)
        self._unsaved = {}          # outcomes the event log refused; readable until restart

    @property
    def enabled(self):
        return self.config.enabled

    # -- reading ------------------------------------------------------------------
    def _view(self, job):
        return {"state": job["state"], "operation_id": job["operation_id"], "started_at": job["started_at"],
                "harness": self.config.harness}

    def _open_request(self, session_id):
        """The session's request that has no outcome yet (rebuilt from the event log after a restart)."""
        for event in reversed(self.repo.stream("session", session_id)):
            kind = event.get("event_type")
            if kind in _EVENTS:
                return None
            if kind == "SummaryReportRequested":
                body = _payload(event)
                if not body.get("operation_id") or not body.get("host_id"):
                    return None
                return {"operation_id": body["operation_id"], "session_id": session_id, "host_id": body["host_id"],
                        "agent": body.get("agent"), "target": body.get("target"), "state": "queued",
                        "started_at": body.get("started_at") or _now(),
                        "queued_at": float(body.get("requested_at") or self._clock()), "claimed_at": None}
        return None

    def _adopt(self, session_id):
        job = self._jobs.get(session_id)
        if job is None:
            job = self._open_request(session_id)
            held = self._unsaved.get(session_id)
            if job is not None and held and held.get("operation_id") == job["operation_id"]:
                return None             # finished, but the event log refused the outcome: it is held in memory
            if job is not None:
                self._jobs[session_id] = job
        return job

    def _expire(self, session_id, job):
        """Fail a job nobody finished; returns True when it was failed (the lock is NOT held)."""
        age = self._clock() - (job["claimed_at"] or job["queued_at"])
        if job["state"] == "queued" and age > QUEUE_GRACE_S:
            self._finish(session_id, job, "SummaryReportFailed",
                         {"error": "host_unreachable", "detail": "the agent's host did not pick up the request"})
            return True
        if job["state"] == "running" and age > self.config.timeout_seconds + RUN_GRACE_S:
            self._finish(session_id, job, "SummaryReportFailed", {"error": "timeout"})
            return True
        return False

    def get(self, session_id):
        """``{"state": idle|queued|running|done|failed, …}``; a report never leaves this module unredacted."""
        with self._lock:
            job = self._adopt(session_id)
            snapshot = dict(job) if job else None
        if snapshot and not self._expire(session_id, snapshot):
            return self._view(snapshot)
        with self._lock:
            unsaved = self._unsaved.get(session_id)
        if unsaved:
            return dict(unsaved)
        for event in reversed(self.repo.stream("session", session_id)):
            state = _EVENTS.get(event.get("event_type"))
            if state:
                body = _payload(event)
                return {"state": state, **{k: v for k, v in body.items() if k not in ("actor", "host_id")}}
        return {"state": "idle"}

    # -- starting -----------------------------------------------------------------
    def _target(self, session):
        conversation = session.get("agent_session")
        if not isinstance(conversation, dict):
            raise ReportRefused(409, "This agent has not identified its conversation yet, so there is no "
                                     "record to report on.", reason="no_identity")
        agent = str(conversation.get("agent") or "").lower()
        value = str(conversation.get("value") or "")
        if conversation.get("kind") == "path":
            value = Path(value).stem
        if agent not in runner.AGENTS or not runner._SESSION_ID.match(value):
            raise ReportRefused(409, "The agent's conversation identity is not one the report can read.",
                                reason="no_identity")
        return agent, value

    def start(self, session_id, *, actor):
        if not self.enabled:
            raise ReportRefused(409, "Full session reports are not enabled.", reason="reports_disabled")
        session = self.repo.get_session(session_id)
        if session is None:
            raise ReportRefused(404, "agent not found")
        agent, target = self._target(session)
        host = str(session.get("host_id") or "")
        if not host:
            raise ReportRefused(409, "This session is not tied to a host, so no host can run the report.",
                                reason="no_host")
        with self._lock:
            job = self._adopt(session_id)
            if job is not None and not self._expired(job):
                return 202, {"ok": True, "status": job["state"], "replayed": True, "session_id": session_id,
                             **self._view(job)}
            if sum(1 for j in self._jobs.values() if not self._expired(j)) >= self.config.max_concurrent:
                raise ReportRefused(429, "Too many reports are running; try again shortly.", reason="report_limit")
            now = self._clock()
            operation_id = f"summary-report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
            job = {"operation_id": operation_id, "session_id": session_id, "host_id": host, "agent": agent,
                   "target": target, "state": "queued", "started_at": _now(), "queued_at": now, "claimed_at": None}
            self._jobs[session_id] = job
        try:
            self.repo.record_event(
                "session", session_id, "SummaryReportRequested",
                {"actor": dict(actor), "agent": agent, "target": target, "host_id": host, "operation_id": operation_id,
                 "started_at": job["started_at"], "requested_at": now},
                session_id=session_id, require_durable=True, publish=False, feed=False)
        except Exception:
            with self._lock:
                self._jobs.pop(session_id, None)
            raise ReportRefused(503, "audit_persistence_failed") from None
        return 202, {"ok": True, "status": "queued", "replayed": False, "session_id": session_id, **self._view(job)}

    def _expired(self, job):
        age = self._clock() - (job["claimed_at"] or job["queued_at"])
        limit = QUEUE_GRACE_S if job["state"] == "queued" else self.config.timeout_seconds + RUN_GRACE_S
        return age > limit

    # -- the host worker's two calls ------------------------------------------------
    def claim(self, host_id):
        """Hand a host its next queued job (one at a time per host) and mark it running."""
        if not self.enabled or not isinstance(host_id, str) or not host_id:
            return []
        with self._lock:
            mine = [j for j in self._jobs.values() if j["host_id"] == host_id]
            if any(j["state"] == "running" and not self._expired(j) for j in mine):
                return []
            ready = sorted((j for j in mine if j["state"] == "queued" and not self._expired(j)),
                           key=lambda j: j["queued_at"])
            if not ready:
                return []
            job = ready[0]
            job["state"], job["claimed_at"] = "running", self._clock()
            return [{"operation_id": job["operation_id"], "session_id": job["session_id"], "agent": job["agent"],
                     "target": job["target"], "harness": self.config.harness,
                     "timeout_seconds": self.config.timeout_seconds}]

    def finish(self, host_id, body):
        """Store a host's result for the open request it names, after validating and redacting it."""
        if not isinstance(body, dict):
            raise ReportRefused(400, "request body must be a JSON object")
        session_id, operation_id = body.get("session_id"), body.get("operation_id")
        if not isinstance(session_id, str) or not isinstance(operation_id, str):
            raise ReportRefused(400, "session_id and operation_id are required")
        with self._lock:
            job = self._adopt(session_id)
        if job is None or job["operation_id"] != operation_id:
            return 200, {"ok": True, "replayed": True}          # already finished (or expired): nothing to store
        if job["host_id"] != host_id:
            raise ReportRefused(403, "this request belongs to another host")
        if body.get("ok") is True:
            try:
                report = runner.validate_report(body.get("report") if isinstance(body.get("report"), str) else "")
            except runner.ReportError as exc:
                self._finish(session_id, job, "SummaryReportFailed", {"error": exc.code, "detail": exc.detail[:300]})
                return 200, {"ok": True, "status": "failed"}
            harness = body.get("harness") if body.get("harness") == self.config.harness else self.config.harness
            duration = body.get("duration_s")
            self._finish(session_id, job, "SummaryReportCompleted",
                         {"report": report, "harness": harness, "generated_at": _now(),
                          "duration_s": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                          else None})
            return 200, {"ok": True, "status": "done"}
        code = body.get("error") if body.get("error") in HOST_ERRORS else "summarizer_failed"
        detail = redact_text(body.get("detail") if isinstance(body.get("detail"), str) else "")[:300]
        self._finish(session_id, job, "SummaryReportFailed", {"error": code, "detail": detail})
        return 200, {"ok": True, "status": "failed"}

    def _finish(self, session_id, job, event_type, body):
        body = {**body, "operation_id": job["operation_id"], "host_id": job["host_id"],
                "started_at": job["started_at"], "finished_at": _now()}
        state = _EVENTS[event_type]
        try:
            self.repo.record_event("session", session_id, event_type, body, session_id=session_id,
                                   require_durable=True, publish=False, feed=False)
            saved = True
        except Exception:
            saved = False
        with self._lock:
            self._jobs.pop(session_id, None)
            if saved:
                self._unsaved.pop(session_id, None)
            else:
                self._unsaved[session_id] = {"state": state, **{k: v for k, v in body.items() if k != "host_id"},
                                             "persisted": False}
