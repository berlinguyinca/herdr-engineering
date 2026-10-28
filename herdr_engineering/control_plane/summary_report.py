"""On-demand full ``/summary`` report for one dashboard session.

The operator asks for a report; a background job runs the same ``summary`` skill the terminal uses
(``herdr_engineering.summary_report``) against the session's record on this host and stores the result
as a durable session event, so it survives a restart and is read back through ``GET .../summary``.
Only sessions whose record lives on the control-plane host can be reported on: nothing here opens a
connection to another machine. The existing model-backed summary card is separate and untouched.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import summary_report as runner
from .redact import redact_text

_EVENTS = {"SummaryReportCompleted": "done", "SummaryReportFailed": "failed"}


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
    local_host_ids: tuple = ()

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
        if (not isinstance(self.local_host_ids, tuple)
                or any(not isinstance(h, str) or not h or len(h) > 253 for h in self.local_host_ids)):
            raise ValueError("summary_report local_host_ids must be a list of host names")

    @classmethod
    def from_mapping(cls, raw):
        raw = dict(raw or {})
        if "local_host_ids" in raw:
            raw["local_host_ids"] = tuple(raw["local_host_ids"] or ())
        return cls(**raw)


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
    def __init__(self, repo, config, *, run=None, hostnames=None, spawn=None):
        self.repo = repo
        self.config = config if isinstance(config, ReportConfig) else ReportConfig.from_mapping(config)
        self._run = run or runner.run_report
        self._local = {h.lower() for h in (hostnames if hostnames is not None
                                           else {socket.gethostname(), socket.getfqdn()})}
        self._local |= {h.lower() for h in self.config.local_host_ids}
        self._spawn = spawn or self._thread
        self._lock = threading.Lock()
        self._running = {}          # session id -> {"operation_id", "started_at"}
        self._unsaved = {}          # outcomes the event log refused; readable until restart

    @property
    def enabled(self):
        return self.config.enabled

    @staticmethod
    def _thread(fn):
        threading.Thread(target=fn, name="summary-report", daemon=True).start()

    # -- reading ------------------------------------------------------------------
    def get(self, session_id):
        """``{"state": idle|running|done|failed, …}``; a report never leaves this module unredacted."""
        with self._lock:
            live = self._running.get(session_id)
            unsaved = self._unsaved.get(session_id)
        if live:
            return {"state": "running", **live}
        if unsaved:
            return dict(unsaved)
        for event in reversed(self.repo.stream("session", session_id)):
            state = _EVENTS.get(event.get("event_type"))
            if state:
                body = _payload(event)
                return {"state": state, **{k: v for k, v in body.items() if k != "actor"}}
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
        host = str(session.get("host_id") or "").lower()
        if host and host not in self._local:
            raise ReportRefused(
                409, f"This session's record is on {session.get('host_id')}, not on the dashboard host.",
                reason="report_host_remote",
                command=f"herdr-eng summary report --agent {agent} --session {target}")
        with self._lock:
            if session_id in self._running:
                return 202, {"ok": True, "status": "running", "replayed": True, "session_id": session_id,
                             **self._running[session_id]}
            if len(self._running) >= self.config.max_concurrent:
                raise ReportRefused(429, "Too many reports are running; try again shortly.",
                                    reason="report_limit")
            operation_id = f"summary-report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
            job = {"operation_id": operation_id, "started_at": _now(), "harness": self.config.harness}
            self._running[session_id] = job
        try:
            self.repo.record_event("session", session_id, "SummaryReportRequested",
                                   {"actor": dict(actor), "agent": agent, "operation_id": operation_id},
                                   session_id=session_id, require_durable=True, publish=False, feed=False)
        except Exception:
            with self._lock:
                self._running.pop(session_id, None)
            raise ReportRefused(503, "audit_persistence_failed") from None
        try:
            self._spawn(lambda: self._work(session_id, agent, target, job))
        except Exception:
            self._finish(session_id, job, "SummaryReportFailed", {"error": "summarizer_failed"})
            raise ReportRefused(503, "could not start the report") from None
        return 202, {"ok": True, "status": "running", "replayed": False, "session_id": session_id, **job}

    # -- the job ------------------------------------------------------------------
    def _work(self, session_id, agent, target, job):
        try:
            result = self._run(agent, target, harness=self.config.harness,
                               timeout=self.config.timeout_seconds)
            body = {"report": result["report"], "harness": result.get("harness"),
                    "generated_at": result.get("generated_at") or _now(),
                    "duration_s": result.get("duration_s")}
            self._finish(session_id, job, "SummaryReportCompleted", body)
        except runner.ReportError as exc:
            self._finish(session_id, job, "SummaryReportFailed",
                         {"error": exc.code, "detail": redact_text(exc.detail or "")[:300]})
        except Exception:
            self._finish(session_id, job, "SummaryReportFailed", {"error": "summarizer_failed"})

    def _finish(self, session_id, job, event_type, body):
        body = {**body, "operation_id": job["operation_id"], "started_at": job["started_at"],
                "finished_at": _now()}
        state = _EVENTS[event_type]
        try:
            self.repo.record_event("session", session_id, event_type, body, session_id=session_id,
                                   require_durable=True, publish=False, feed=False)
            saved = True
        except Exception:
            saved = False
        with self._lock:
            self._running.pop(session_id, None)
            if saved:
                self._unsaved.pop(session_id, None)
            else:
                self._unsaved[session_id] = {"state": state, **body, "persisted": False}
