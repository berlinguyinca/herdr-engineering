"""Durable Plannotator launch and outcome receipts.

Plannotator owns the review UI and decision. An authenticated host launcher
reports its ready URL and the CLI's JSON result; HerdR never fabricates an
approval or treats a heartbeat as reviewer activity. Snapshots use the existing
Postgres event sink and are reconstructed from that log after restart.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

from .redact import redact_text

ENTITY = "plannotator_review"
TERMINAL = {"approved", "changes_requested", "closed", "failed"}
MAX_TEXT = 200_000


def safe_review_url(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("review_url must be an HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        if parsed.query or parsed.fragment or "\\" in value:
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError("review_url must be HTTP(S), without credentials, query or fragment") from None
    return value


def _text(value, field, limit=512, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{field} must be a string of at most {limit} characters")
    return redact_text(value)


def _snapshot(repo, review_id):
    stream = repo._events.get((ENTITY, review_id), [])
    if not stream:
        return None
    patches = []
    for event in reversed(stream):
        payload = event.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        patches.append(payload)
        if "kind" in payload:
            break
    record = {}
    for payload in reversed(patches):
        record.update(copy.deepcopy(payload))
    return record


class ReviewService:
    def __init__(self, repo):
        self.repo = repo

    def _write(self, record, event_type):
        if not callable(getattr(self.repo._sink, "record_required", None)):
            raise RuntimeError("durable review storage is unavailable")
        # Frequent liveness receipts do not duplicate the plan/feedback body.
        payload = ({k: record[k] for k in ("review_id", "last_seen_at", "status", "review_url")}
                   if event_type == "PlannotatorHeartbeat" else record)
        self.repo.record_event(
            ENTITY, record["review_id"], event_type, copy.deepcopy(payload),
            mission_id=record.get("mission_id"), session_id=record.get("session_id"),
            require_durable=True, publish=event_type != "PlannotatorHeartbeat",
        )
        return self.get(record["review_id"])

    def get(self, review_id):
        with self.repo._event_lock:
            record = _snapshot(self.repo, review_id)
        if record is None:
            return None
        try:
            elapsed = max(0, self.repo._now() - datetime.fromisoformat(record["last_seen_at"]).timestamp())
        except (ValueError, KeyError, TypeError):
            elapsed = None
        record["heartbeat_age_seconds"] = int(elapsed) if elapsed is not None else None
        record["stale"] = record["status"] not in TERMINAL and (elapsed is None or elapsed > 90)
        return record

    def list_reviews(self, *, mission_id=None, session_id=None):
        with self.repo._event_lock:
            ids = [key for kind, key in self.repo._events if kind == ENTITY]
            records = [self.get(key) for key in ids]
        return sorted(
            [r for r in records if (mission_id is None or r.get("mission_id") == mission_id)
             and (session_id is None or r.get("session_id") == session_id)],
            key=lambda r: (r["updated_at"], r["review_id"]), reverse=True,
        )

    def register(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("review must be an object")
        rid = payload.get("review_id") or f"rev_{uuid4().hex}"
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", rid):
            raise ValueError("invalid review_id")
        kind = payload.get("kind", "plan")
        if kind not in {"plan", "code"}:
            raise ValueError("kind must be plan or code")
        plan = _text(payload.get("plan"), "plan", MAX_TEXT)
        # Hash the exact input, before redaction, for receipt binding.
        digest = hashlib.sha256(payload["plan"].encode()).hexdigest() if plan is not None else None
        url = safe_review_url(payload.get("review_url"))
        now = self.repo._stamp()
        record = {
            "review_id": rid, "kind": kind,
            "title": _text(payload.get("title") or f"{kind.title()} review", "title", required=True),
            "instance_id": _text(payload.get("instance_id"), "instance_id", required=True),
            "review_url": url, "plan": plan, "plan_digest": digest,
            "status": "awaiting_review" if url else "starting", "decision": None,
            "feedback": None, "receipt": None, "error": None,
            "provenance": "authenticated_host_launcher", "created_at": now,
            "updated_at": now, "last_seen_at": now, "completed_at": None,
        }
        for key in ("repository", "project_id", "revision", "mission_id", "session_id", "candidate_id", "host_id"):
            record[key] = _text(payload.get(key), key)
        with self.repo._event_lock:
            existing = _snapshot(self.repo, rid)
            if existing:
                fields = ("kind", "instance_id", "plan_digest", "repository", "revision", "project_id", "mission_id", "session_id")
                if any(existing.get(k) != record.get(k) for k in fields):
                    raise ValueError("review_id already belongs to another launch or revision")
                return self.get(rid)
            return self._write(record, "PlannotatorReviewStarted")

    def _bound(self, review_id, payload):
        record = _snapshot(self.repo, review_id)
        if record is None:
            raise KeyError(review_id)
        if not isinstance(payload, dict) or payload.get("instance_id") != record["instance_id"]:
            raise ValueError("receipt must match the launched instance_id")
        for field in ("revision", "plan_digest"):
            if payload.get(field) != record.get(field):
                raise ValueError(f"receipt must match the reviewed {field}")
        return record

    def heartbeat(self, review_id, payload):
        with self.repo._event_lock:
            record = self._bound(review_id, payload)
            if record["status"] in TERMINAL:
                return self.get(review_id)
            if payload.get("review_url") is not None:
                record["review_url"] = safe_review_url(payload["review_url"])
                record["status"] = "awaiting_review"
            record["last_seen_at"] = self.repo._stamp()
            # A launcher heartbeat is transport liveness, not human activity.
            return self._write(record, "PlannotatorHeartbeat")

    def complete(self, review_id, payload):
        with self.repo._event_lock:
            record = self._bound(review_id, payload)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("result must be the Plannotator CLI JSON object")
            decision = result.get("decision")
            if decision not in {"approved", "annotated", "denied", "dismissed"}:
                raise ValueError("unsupported Plannotator decision")
            feedback = _text(result.get("feedback", result.get("message", "")), "feedback", MAX_TEXT)
            receipt = {"decision": decision, "feedback": feedback,
                       "source": "plannotator_cli_json", "verification": "host_reported"}
            if record["status"] in TERMINAL:
                if record.get("receipt") != receipt:
                    raise ValueError("review already has a different final outcome")
                return self.get(review_id)
            record.update(
                status={"approved": "approved", "annotated": "changes_requested", "denied": "changes_requested", "dismissed": "closed"}[decision],
                decision=decision, feedback=feedback, receipt=receipt,
                completed_at=self.repo._stamp(), updated_at=self.repo._stamp(),
                last_seen_at=self.repo._stamp(),
            )
            return self._write(record, "PlannotatorReviewCompleted")

    def fail(self, review_id, payload):
        with self.repo._event_lock:
            record = self._bound(review_id, payload)
            if record["status"] in TERMINAL:
                return self.get(review_id)
            record.update(status="failed", error=_text(payload.get("error"), "error", 2000, required=True),
                          completed_at=self.repo._stamp(), updated_at=self.repo._stamp())
            return self._write(record, "PlannotatorReviewFailed")
