"""Assistant-facing adapter for project intelligence (spec 01 "Assistant tools", 04 "Assistant actions").

* Read tools run under the *caller's* ``Viewer``; the adapter has no authority of its own.
* ``navigate`` emits a typed, allowlisted deep link, never free text. Unknown kinds/filters are refused.
* ``ActionBoard`` records proposals only. An approval binds approver, exact payload hash, expected resource
  version and expiry; any mismatch refuses. Nothing here executes an action: execution stays with the existing
  services (ci_fix, review_gate) behind ``repair_policy`` — an approved proposal is permission to ask them.
* Notifications are planned, deduped and quiet-hour aware; sending is a separate privileged channel (blocker B3).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from . import project_intelligence as pi

ROUTES = {"timeline": "#/intelligence", "event": "#/intelligence", "approval": "#/attention"}
FILTER_KEYS = frozenset({"project", "event", "source", "category", "as_of"})
_ID = 200


def navigate(kind: str, **filters: str) -> str:
    """Typed deep link for browser and iPhone clients; refuses anything outside the allowlisted schema."""
    base = ROUTES.get(kind)
    if base is None:
        raise ValueError(f"unknown navigation kind {kind!r}")
    bad = set(filters) - FILTER_KEYS
    if bad:
        raise ValueError(f"unsupported filter(s): {sorted(bad)}")
    clean = {k: str(v) for k, v in filters.items() if v not in (None, "")}
    if any(len(v) > _ID or "\n" in v for v in clean.values()):
        raise ValueError("filter value is too long or malformed")
    if kind in ("timeline", "event") and "project" not in clean:
        raise ValueError("project is required")
    if kind == "event" and "event" not in clean:
        raise ValueError("event is required")
    return base + (f"?{urlencode(clean)}" if clean else "")


class ReadTools:
    """``herdr.projects.search``, ``herdr.timeline.query``, ``herdr.evidence.open``, ``herdr.dashboard.navigate``."""

    def __init__(self, store: pi.IntelligenceStore):
        self._s = store

    def projects_search(self, viewer: pi.Viewer, text: str = "") -> list[dict]:
        t = text.lower()
        return [{"id": r.id, "name": r.name, "link": navigate("timeline", project=r.id)}
                for r in self._s.records("project", viewer) if t in r.name.lower() or t in r.id.lower()]

    def timeline_query(self, viewer: pi.Viewer, project_id: str, **kw) -> dict:
        out = self._s.timeline(viewer, project_id=project_id, **kw)
        for item in out["items"]:
            item["link"] = navigate("event", project=project_id, event=item["event_id"])
        return out

    def evidence_open(self, viewer: pi.Viewer, project_id: str, event_id: str) -> dict:
        """Looked up by id (not by position in a window). Hidden and absent are indistinguishable."""
        got = self._s.get_event(viewer, event_id)
        if project_id not in got["project_ids"]:
            raise pi.NotFound("event")
        return {"event_id": event_id, "evidence_refs": got["evidence_refs"], "label": got["label"],
                "link": navigate("event", project=project_id, event=event_id)}


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Proposal:
    id: str
    org_id: str
    requester: str
    action: str
    payload: dict
    payload_sha256: str
    resource_version: int
    expires_at: datetime
    state: str = "awaiting_approval"      # awaiting_approval | approved | executing | executed | failed | refused | expired
    approver: str | None = None
    rollback_available: bool = False
    history: list[str] = field(default_factory=list)


ACTIONS = frozenset({"request_ci_repair", "ci_rerun", "propose_issue", "link_entities", "pause_ingestion"})


class ActionBoard:
    def __init__(self, clock=lambda: datetime.now(UTC), ttl: timedelta = timedelta(minutes=30)):
        self._clock, self._ttl = clock, ttl
        self._p: dict[str, Proposal] = {}
        self.on_new: Callable[[Proposal], None] | None = None     # told of each NEW proposal (never an idempotent repeat)
        self._idem: dict[tuple[str, str, str], str] = {}

    def request(self, *, org_id: str, requester: str, action: str, payload: dict, resource_version: int,
                rollback_available: bool = False, idempotency_key: str | None = None) -> Proposal:
        if action not in ACTIONS:
            raise pi.IntelligenceError(f"action {action!r} is not allowed")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 0 < len(idempotency_key) <= 64:
                raise pi.IntelligenceError("idempotency key must be 1-64 characters")
            known = self._p.get(self._idem.get((org_id, requester, idempotency_key), ""))
            if known is not None:
                if known.payload_sha256 != payload_hash(payload) or known.action != action:
                    raise pi.Conflict("idempotency key was already used for a different request")
                return known
        p = Proposal("act_" + uuid.uuid4().hex, org_id, requester, action, payload, payload_hash(payload), resource_version,
                     self._clock() + self._ttl, rollback_available=rollback_available)
        self._p[p.id] = p
        if idempotency_key is not None:
            self._idem[(org_id, requester, idempotency_key)] = p.id
        if self.on_new is not None:
            try:
                self.on_new(p)
            except Exception:  # noqa: BLE001 - a listener must never break filing a proposal
                pass
        return p

    def approve(self, proposal_id: str, *, approver: str, org_id: str, payload_sha256: str, resource_version: int,
                allow_self: bool = False) -> Proposal:
        p = self._p.get(proposal_id)
        if p is None or p.org_id != org_id:
            raise pi.NotFound("proposal")
        if p.state != "awaiting_approval":
            raise pi.Conflict(f"proposal is {p.state}")
        if self._clock() > p.expires_at:
            p.state = "expired"
            raise pi.Conflict("approval window expired")
        # A bad attempt is recorded but never changes the state: otherwise any operator could kill a pending proposal
        # with one wrong hash. Only a matching approval by someone else, a rejection, or expiry ends it.
        if approver == p.requester and not allow_self:     # allow_self: single-operator mode only
            p.history.append("self-approval refused")
            raise pi.Conflict("requester cannot approve their own proposal")
        if payload_sha256 != p.payload_sha256 or resource_version != p.resource_version:
            p.history.append("approval did not match the exact payload/version")
            raise pi.Conflict("approval does not match the proposed payload or resource version")
        p.state, p.approver = "approved", approver
        return p


    def get(self, proposal_id: str, org_id: str) -> Proposal:
        p = self._p.get(proposal_id)
        if p is None or p.org_id != org_id:
            raise pi.NotFound("proposal")
        return p

    def reject(self, proposal_id: str, *, approver: str, org_id: str) -> Proposal:
        p = self._p.get(proposal_id)
        if p is None or p.org_id != org_id:
            raise pi.NotFound("proposal")
        if p.state != "awaiting_approval":
            raise pi.Conflict(f"proposal is {p.state}")
        p.state = "refused"
        p.history.append(f"rejected by {approver}")
        return p

    def pending(self, org_id: str) -> list[Proposal]:
        """Awaiting proposals for an org; expired ones are marked and dropped from the list."""
        now, out = self._clock(), []
        for p in list(self._p.values()):          # a snapshot: requests may insert from other threads
            if p.org_id != org_id:
                continue
            if p.state == "awaiting_approval" and now > p.expires_at:
                p.state = "expired"
            if p.state == "awaiting_approval":
                out.append(p)
        return out


@dataclass(frozen=True)
class Notice:
    key: str
    priority: str          # urgent | normal
    text: str
    link: str


class NotificationPlanner:
    """Plans, never sends. Same key never re-notifies; quiet hours hold normal notices for the digest."""

    def __init__(self, quiet_start: int = 22, quiet_end: int = 7):
        self._qs, self._qe = quiet_start, quiet_end
        self._sent: set[str] = set()
        self.digest: list[Notice] = []

    def _quiet(self, now: datetime) -> bool:
        h = now.hour
        return (h >= self._qs or h < self._qe) if self._qs > self._qe else self._qs <= h < self._qe

    def plan(self, n: Notice, now: datetime) -> str:
        if n.key in self._sent:
            return "duplicate"
        self._sent.add(n.key)
        if n.priority == "urgent" or not self._quiet(now):
            return "send_now"
        self.digest.append(n)
        return "held_for_digest"


READ_OPS = frozenset({"projects_search", "timeline_query", "evidence_open"})


def execute_read(tools: ReadTools, viewer: pi.Viewer | None, payload: dict) -> dict:
    """Run one allowlisted read operation as ``viewer``. No viewer mapping means no data (deny-by-default)."""
    if viewer is None:
        raise pi.NotFound("no project-intelligence scope is configured for this user")
    op = payload.get("operation")
    if op not in READ_OPS:
        raise ValueError(f"operation must be one of {sorted(READ_OPS)}")
    if op == "projects_search":
        return {"projects": tools.projects_search(viewer, str(payload.get("text") or "")[:100])}
    project = str(payload.get("project") or "")
    if not project:
        raise ValueError("project is required")
    if op == "evidence_open":
        return tools.evidence_open(viewer, project, str(payload.get("event") or ""))
    kw = {k: payload[k] for k in ("source", "category", "as_of") if isinstance(payload.get(k), str) and payload[k]}
    return tools.timeline_query(viewer, project, **kw)
