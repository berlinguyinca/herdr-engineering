"""Offline event specialist domain. No transport, credentials, scheduler or grant issuer.

All input is supplied by a trusted application boundary. Eligibility is a proposal,
never a substitute for authenticated approval and a durable transport outbox.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace

QUESTIONS = {
    "capacity": "date availability and capacity",
    "patio": "patio, heaters and rain backup",
    "children": "children welcome and any restrictions",
    "mingling": "mingling space and high tops",
    "service": "counter service options",
    "tabs": "separate tabs",
    "wine": "wine options",
    "costs": "fees, minimum spend, deposit and cancellation terms",
}


def _line(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("nonempty single-line text required")
    return value


def _email(value: str) -> str:
    value = _line(value)
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
        raise ValueError("one plain email address required")
    local, domain = value.rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def _time(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative timestamp required")


@dataclass(frozen=True)
class Requirements:
    date_time: str | None = None
    timezone: str | None = None
    adults: int | None = None
    children: int | None = None
    budget: float | None = None
    currency: str | None = None

    def __post_init__(self):
        for value in (self.adults, self.children):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("guest counts must be nonnegative integers")
        if self.budget is not None:
            _time(self.budget)
        for value in (self.date_time, self.timezone, self.currency):
            if value is not None:
                _line(value)

    def missing(self) -> tuple[str, ...]:
        return tuple(k for k, v in asdict(self).items() if v is None)


@dataclass(frozen=True)
class Contact:
    email: str
    source: str
    verified_at: float
    valid_until: float

    def __post_init__(self):
        object.__setattr__(self, "email", _email(self.email))
        _line(self.source)
        _time(self.verified_at)
        _time(self.valid_until)
        if self.valid_until <= self.verified_at:
            raise ValueError("contact verification window required")


@dataclass(frozen=True)
class Venue:
    id: str
    name: str
    city: str | None
    identity: str = "unverified"
    contact: Contact | None = None

    def __post_init__(self):
        _line(self.id)
        _line(self.name)
        if self.city is not None:
            _line(self.city)
        if self.identity not in {"verified", "unverified", "ambiguous", "closed"}:
            raise ValueError("invalid identity state")


@dataclass(frozen=True)
class Draft:
    user_id: str
    task_id: str
    venue_id: str
    sender: str
    recipient: str | None
    subject: str
    body: str
    followup: bool = False

    def __post_init__(self):
        for value in (self.user_id, self.task_id, self.venue_id, self.subject):
            _line(value)
        object.__setattr__(self, "sender", _email(self.sender))
        if self.recipient is not None:
            object.__setattr__(self, "recipient", _email(self.recipient))
        if not self.body.strip():
            raise ValueError("draft body required")

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @property
    def key(self) -> str:
        return f"event-email:{self.digest}"


@dataclass(frozen=True)
class Grant:
    """Local policy fixture only. Real grants require an authenticated platform issuer."""

    user_id: str
    task_id: str
    venue_id: str
    sender: str
    recipient: str
    channel: str
    draft_hashes: tuple[str, ...]
    expires_at: float
    max_followups: int
    min_interval: float
    followup_until: float
    revoked: bool = False

    def __post_init__(self):
        object.__setattr__(self, "sender", _email(self.sender))
        object.__setattr__(self, "recipient", _email(self.recipient))
        if (not isinstance(self.draft_hashes, tuple)
                or any(not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                       for digest in self.draft_hashes)
                or len(set(self.draft_hashes)) != len(self.draft_hashes)):
            raise ValueError("immutable unique SHA-256 draft approvals required")
        for value in (self.expires_at, self.min_interval, self.followup_until):
            _time(value)
        if type(self.max_followups) is not int or self.max_followups < 0 or self.min_interval <= 0:
            raise ValueError("bounded followup policy required")


@dataclass(frozen=True)
class Receipt:
    key: str
    venue_id: str
    recipient: str
    thread_id: str
    message_id: str
    at: float
    status: str
    followup: bool

    def __post_init__(self):
        for value in (self.key, self.venue_id, self.thread_id, self.message_id):
            _line(value)
        object.__setattr__(self, "recipient", _email(self.recipient))
        _time(self.at)
        if self.status not in {"sent", "uncertain", "bounced"}:
            raise ValueError("invalid delivery status")


@dataclass(frozen=True)
class Evidence:
    venue_id: str
    question: str
    value: str
    kind: str
    source: str
    observed_at: float

    def __post_init__(self):
        for value in (self.venue_id, self.value, self.source):
            _line(value)
        _time(self.observed_at)
        if self.question not in QUESTIONS or self.kind not in {"official_fact", "estimate", "explicit_reply"}:
            raise ValueError("invalid question or evidence kind")


@dataclass(frozen=True)
class Coordinator:
    user_id: str
    task_id: str
    requirements: Requirements
    venue_ids: tuple[str, ...] = ()
    resource_refs: tuple[str, ...] = ()
    receipts: tuple[Receipt, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    replied_venues: tuple[str, ...] = ()
    stopped_venues: tuple[str, ...] = ()

    def __post_init__(self):
        _line(self.user_id)
        _line(self.task_id)
        for field in ("venue_ids", "resource_refs"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise ValueError("immutable unique task scope required")
            for value in values:
                _line(value)
        if any(r.venue_id not in self.venue_ids for r in self.receipts) or any(
                e.venue_id not in self.venue_ids for e in self.evidence) or any(
                v not in self.venue_ids for v in (*self.replied_venues, *self.stopped_venues)):
            raise ValueError("checkpoint outside selected venue scope")
        keys = [r.key for r in self.receipts]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate receipt key")

    def draft(self, venue: Venue, sender: str, *, followup: bool = False) -> Draft:
        self._venue_scope(venue.id)
        r = asdict(self.requirements)
        values = {k: v if v is not None else f"[{k} required]" for k, v in r.items()}
        intro = "Following up on our event inquiry" if followup else "We are considering an event"
        body = (
            f"Hello {venue.name} team,\n\n{intro} at {venue.name}"
            f"{f' in {venue.city}' if venue.city else ''}. "
            f"Our proposed date/time is {values['date_time']} ({values['timezone']}), "
            f"with {values['adults']} adults and {values['children']} children. "
            f"Our budget is {values['budget']} {values['currency']}.\n\n"
            "Could you please reply by email about:\n"
            + "\n".join(f"- {text}?" for text in QUESTIONS.values())
            + "\n\nThis is an information request. Thank you!"
        )
        return Draft(self.user_id, self.task_id, venue.id, sender,
                     venue.contact.email if venue.contact else None,
                     f"{'Follow-up: ' if followup else ''}Event inquiry for {venue.name}", body, followup)

    def blockers(self, venue: Venue, draft: Draft, grant: Grant | None, now: float) -> tuple[str, ...]:
        """Evaluate a proposal; callers cannot infer permission to send from an empty result."""
        _time(now)
        self._venue_scope(venue.id)
        blocked = []
        if self.requirements.missing():
            blocked.append("requirements_missing")
        if (draft.user_id, draft.task_id, draft.venue_id) != (self.user_id, self.task_id, venue.id):
            blocked.append("draft_scope_mismatch")
        if venue.identity != "verified":
            blocked.append("identity_unverified")
        contact = venue.contact
        if not contact or not contact.verified_at <= now < contact.valid_until or draft.recipient != contact.email:
            blocked.append("contact_unverified")
        if (not grant or grant.revoked or grant.channel != "email" or now >= grant.expires_at
                or (grant.user_id, grant.task_id, grant.venue_id, grant.sender, grant.recipient)
                != (self.user_id, self.task_id, venue.id, draft.sender, draft.recipient)
                or draft.digest not in grant.draft_hashes):
            blocked.append("approval_required")
        history = [r for r in self.receipts if r.venue_id == venue.id]
        if any(r.key == draft.key for r in history):
            blocked.append("duplicate_message")
        if any(r.status == "uncertain" for r in history):
            blocked.append("delivery_uncertain")
        if venue.id in self.stopped_venues or any(r.status == "bounced" for r in history):
            blocked.append("venue_stopped")
        if venue.id in self.replied_venues:
            blocked.append("reply_received")
        if not draft.followup and history:
            blocked.append("already_contacted")
        if draft.followup:
            if not any(not r.followup and r.status == "sent" for r in history):
                blocked.append("initial_delivery_required")
            if grant:
                if sum(r.followup for r in history) >= grant.max_followups:
                    blocked.append("followup_limit")
                if history and now < max(r.at for r in history) + grant.min_interval:
                    blocked.append("followup_too_soon")
                if now >= grant.followup_until:
                    blocked.append("followup_window_closed")
        return tuple(blocked)

    def record(self, receipt: Receipt, draft: Draft) -> Coordinator:
        """Ingest a trusted adapter receipt. This never initiates delivery."""
        self._venue_scope(receipt.venue_id)
        if ((draft.user_id, draft.task_id) != (self.user_id, self.task_id)
                or (receipt.key, receipt.venue_id, receipt.recipient, receipt.followup)
                != (draft.key, draft.venue_id, draft.recipient, draft.followup)):
            raise ValueError("receipt/draft scope mismatch")
        for old in self.receipts:
            if old.key == receipt.key:
                if old == receipt:
                    return self
                raise ValueError("conflicting receipt requires transport reconciliation")
        return replace(self, receipts=(*self.receipts, receipt))

    def observe(self, evidence: Evidence) -> Coordinator:
        self._venue_scope(evidence.venue_id)
        if evidence.kind == "explicit_reply":
            raise ValueError("explicit reply requires correlated receipt")
        return replace(self, evidence=tuple(dict.fromkeys((*self.evidence, evidence))))

    def reply(self, venue_id: str, sender: str, thread_id: str, in_reply_to: str,
              claims: tuple[Evidence, ...], *, stop: bool = False) -> Coordinator:
        """Trusted adapter attests metadata; raw email text is never parsed as commands."""
        self._venue_scope(venue_id)
        sender = _email(sender)
        matching = [r for r in self.receipts if r.venue_id == venue_id and r.status == "sent"
                    and r.recipient == sender and r.thread_id == thread_id and r.message_id == in_reply_to]
        if len(matching) != 1 or any(e.venue_id != venue_id or e.kind != "explicit_reply"
                               or e.observed_at < matching[0].at for e in claims):
            raise ValueError("uncorrelated reply; quarantine for review")
        return replace(self, evidence=tuple(dict.fromkeys((*self.evidence, *claims))),
                       replied_venues=tuple(sorted(set((*self.replied_venues, venue_id)))),
                       stopped_venues=tuple(sorted(set((*self.stopped_venues, venue_id)))) if stop else self.stopped_venues)

    def _venue_scope(self, venue_id: str) -> None:
        if venue_id not in self.venue_ids:
            raise ValueError("venue outside selected task scope")

    def unanswered(self, venue_id: str) -> tuple[str, ...]:
        self._venue_scope(venue_id)
        return tuple(q for q in QUESTIONS if not any(e.venue_id == venue_id and e.question == q
                                                     and e.kind == "explicit_reply" for e in self.evidence))

    def comparison(self, venue_id: str) -> dict:
        self._venue_scope(venue_id)
        result = {}
        for question in QUESTIONS:
            claims = [asdict(e) for e in self.evidence if e.venue_id == venue_id and e.question == question]
            status = "unknown"
            if claims:
                status = "answered" if any(c["kind"] == "explicit_reply" for c in claims) else "researched"
                if len({c["value"] for c in claims}) > 1:
                    status = "conflict"
            result[question] = {"status": status, "evidence": claims}
        return result

    def dumps(self) -> str:
        return json.dumps({"version": 1, **asdict(self)}, sort_keys=True, allow_nan=False)

    @classmethod
    def loads(cls, checkpoint: str) -> Coordinator:
        """Trusted private checkpoint projection; not an authorization import format."""
        data = json.loads(checkpoint)
        if data.pop("version", None) != 1:
            raise ValueError("unsupported checkpoint version")
        data["requirements"] = Requirements(**data["requirements"])
        data["receipts"] = tuple(Receipt(**r) for r in data["receipts"])
        data["evidence"] = tuple(Evidence(**e) for e in data["evidence"])
        for key in ("venue_ids", "resource_refs", "replied_venues", "stopped_venues"):
            data[key] = tuple(data[key])
        return cls(**data)

    def browser_proposal(self, request_ref: str, resource_refs: tuple[str, ...], *,
                         account_ref: str | None = None, operation: str = "observe") -> tuple[dict, dict]:
        """Emit intended browser v1 envelopes locally; no browser capability is invoked."""
        if not set(resource_refs).issubset(self.resource_refs):
            raise ValueError("browser resource outside selected task scope")
        refs = (request_ref, self.user_id, self.task_id, *resource_refs)
        if account_ref is not None:
            refs = (*refs, account_ref)
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", ref) for ref in refs):
            raise ValueError("opaque browser references required")
        if operation not in {"observe", "prepare"} or len(resource_refs) > 32 or len(set(resource_refs)) != len(resource_refs):
            raise ValueError("unsupported browser proposal")
        scope = {"user_ref": self.user_id, "account_ref": account_ref, "task_ref": self.task_id}
        request = {
            "schema_version": "herdr.browser-capability.v1", "kind": "request",
            "request_ref": request_ref, "scope": dict(scope), "browser_binding_ref": None,
            "grant_ref": None, "operation": operation, "resource_refs": list(resource_refs),
            "goal": "Prepare venue observations or local event drafts within the event task scope.",
            "procedure_pin": None, "idempotency_key": request_ref,
            "postcondition_refs": ["postcondition:no-external-write", "postcondition:scope-unchanged"],
        }
        result = {
            "schema_version": "herdr.browser-capability.v1", "kind": "result",
            "request_ref": request_ref, "scope": dict(scope), "status": "unbound",
            "evidence_refs": [], "proposal_refs": [], "human_handoff_ref": None,
            "retry_policy": "none", "reason_code": "adapter_unbound",
        }
        return request, result
