"""Project intelligence core: provenance-backed normalized events, projects, investigations,
evidence-linked relationships and the timeline read model (spec herdr-project-intelligence-autonomy-v1, P1).

Design rulings (docs/specs/herdr-project-intelligence-autonomy-v1-*/DECISIONS.md):

* D2: the durable event log stays ``fabric_events`` (envelope v2). This module owns only the
  intelligence entities and a connector inbox/outbox; ``IntelligenceStore.append_event`` returns the
  envelope-v2 ``Event`` payload a caller may hand to ``repo.record_event``.
* D1: inferred relationships live here (``Relationship.derivation``/``review_state``), never in
  ``graph.py``. A relationship never grants permission or ownership: predicates are an allowlist.
* D3: every read is filtered by ``Viewer`` (org, project ACL, clearance). Counts, previews and
  filters are computed after filtering so nothing leaks through them.
* D9: everything is off unless ``control_plane.project_intelligence.enabled``; ``kill_switch`` and
  per-source pause block new ingestion and autonomous actions immediately.

This is the in-memory reference store (also the test double). ``0020_project_intelligence.sql``
carries the durable schema.
"""
from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
NAMESPACE = uuid.UUID("5d1c7b2e-6f0a-4c55-9a3e-0c1a2b3d4e5f")

# Clearance order for security labels (visibility never widens by similarity or count).
LABELS = ("public", "internal", "confidential", "restricted")

DERIVATIONS = ("explicit", "inferred", "human_verified", "system_verified")
# A relationship can describe work; it can never be a permission or an ownership grant.
PREDICATES = frozenset({
    "reported_by", "attached_to", "mentions", "addresses_issue", "proposes_fix_for", "tested_by",
    "reviewed_by", "implements", "depends_on", "assigned_to", "requested_by", "contradicts",
    "supersedes", "blocks", "resolved_by", "part_of",
})
# Predicates that name a person's responsibility need a verified source (no inference).
AUTHORITY_PREDICATES = frozenset({"assigned_to", "requested_by"})
ANCHOR_TYPES = frozenset({
    "email_part", "attachment_bytes", "page", "series_point", "code_line", "ci_log_offset",
    "agent_event", "object", "cell_range",
})
FLAGS = ("ingest", "derive", "suggest", "execute", "auto_merge")


class IntelligenceError(ValueError):
    """Invalid input (HTTP 400)."""


class NotFound(LookupError):
    """Unknown or not visible to the viewer (HTTP 404; indistinguishable on purpose)."""


class StoreUnavailable(RuntimeError):
    """The durable store cannot be used (HTTP 503)."""


class IngestionPaused(RuntimeError):
    """Ingestion is paused for this source, or the kill switch is on (HTTP 409)."""


class Conflict(RuntimeError):
    """Conditional-version mismatch (HTTP 409)."""


def config(raw: dict | None) -> dict:
    """Normalise ``control_plane.project_intelligence``. Everything defaults to off."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"enabled": raw.get("enabled") is True, "kill_switch": raw.get("kill_switch") is True}
    for flag in FLAGS:
        out[flag] = raw.get(flag) is True
    out["paused_sources"] = sorted({str(s) for s in raw.get("paused_sources") or [] if isinstance(s, str)})
    return out


def _utc(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IntelligenceError(f"{field_name} is not an ISO-8601 timestamp") from exc
    else:
        raise IntelligenceError(f"{field_name} is required")
    if dt.tzinfo is None:
        raise IntelligenceError(f"{field_name} needs a timezone")
    return dt.astimezone(UTC)


def _label(value: Any) -> str:
    if value not in LABELS:
        raise IntelligenceError(f"security_label must be one of {', '.join(LABELS)}")
    return value


@dataclass(frozen=True)
class Viewer:
    """Who is asking. ``project_ids=None`` means every project in the org (operator)."""

    org_id: str
    project_ids: frozenset[str] | None = None
    clearance: str = "internal"

    def may_see(self, org_id: str, project_ids: tuple[str, ...], label: str) -> bool:
        if org_id != self.org_id or LABELS.index(label) > LABELS.index(self.clearance):
            return False
        if self.project_ids is None:
            return True
        return bool(project_ids) and bool(self.project_ids.intersection(project_ids))


@dataclass(frozen=True)
class EvidenceRef:
    source_ref: str
    anchor_type: str
    anchor_value: str
    version_id: str | None = None
    content_hash: str | None = None
    git_sha: str | None = None
    redaction_policy: str = "none"

    @staticmethod
    def parse(raw: Any) -> EvidenceRef:
        if not isinstance(raw, dict):
            raise IntelligenceError("evidence ref must be an object")
        source_ref, anchor_type, anchor_value = raw.get("source_ref"), raw.get("anchor_type"), raw.get("anchor_value")
        if not isinstance(source_ref, str) or not source_ref:
            raise IntelligenceError("evidence needs source_ref")
        if anchor_type not in ANCHOR_TYPES:
            raise IntelligenceError("evidence anchor_type is not recognised")
        if not isinstance(anchor_value, str) or not anchor_value:
            raise IntelligenceError("evidence needs anchor_value")
        return EvidenceRef(
            source_ref, anchor_type, anchor_value,
            version_id=raw.get("version_id") if isinstance(raw.get("version_id"), str) else None,
            content_hash=raw.get("content_hash") if isinstance(raw.get("content_hash"), str) else None,
            git_sha=raw.get("git_sha") if isinstance(raw.get("git_sha"), str) else None,
            redaction_policy=raw.get("redaction_policy") if isinstance(raw.get("redaction_policy"), str) else "none",
        )

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass(frozen=True)
class SourceEvent:
    event_id: str
    org_id: str
    source_system: str
    source_instance: str
    source_id: str
    source_version: str
    event_type: str
    occurred_at: datetime
    ingested_at: datetime
    project_ids: tuple[str, ...]
    investigation_ids: tuple[str, ...]
    object_ref: str
    actor_ref: str | None
    title: str
    category: str
    evidence: tuple[EvidenceRef, ...]
    security_label: str
    visibility_scope: str
    payload_schema_version: int
    payload: dict
    content_digest: str
    correlation_id: str | None = None
    causation_id: str | None = None
    supersedes_id: str | None = None
    late: bool = False
    tombstoned: bool = False


def event_id_for(org_id: str, source_system: str, source_instance: str, source_id: str,
                 source_version: str, event_type: str) -> str:
    """Stable id from the spec's dedupe key: the same source fact always maps to the same id."""
    key = "\x1f".join((org_id, source_system, source_instance, source_id, source_version, event_type))
    return "pie_" + uuid.uuid5(NAMESPACE, key).hex


def digest_of(payload: dict) -> str:
    import json
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Relationship:
    id: str
    org_id: str
    project_ids: tuple[str, ...]
    from_ref: str
    to_ref: str
    predicate: str
    derivation: str
    evidence: tuple[EvidenceRef, ...]
    security_label: str
    review_state: str  # provisional | confirmed | rejected
    confidence: float | None = None
    extractor_version: str | None = None
    model_id: str | None = None
    verified_by: str | None = None
    superseded_by: str | None = None
    version: int = 1


@dataclass(frozen=True)
class Finding:
    id: str
    org_id: str
    project_ids: tuple[str, ...]
    statement: str
    evidence: tuple[EvidenceRef, ...]
    security_label: str
    extractor_version: str
    model_identifier: str

    @property
    def verified(self) -> bool:
        """A statement without evidence is rendered as unverified, never as fact."""
        return bool(self.evidence)


@dataclass
class _Record:
    kind: str
    id: str
    org_id: str
    name: str
    project_ids: tuple[str, ...]
    status: str = "active"
    owner: str | None = None
    version: int = 1
    security_label: str = "internal"


class IntelligenceStore:
    """In-memory reference implementation. Thread-safe; every mutation is idempotent."""

    def __init__(self, *, clock=None, late_after_seconds: int = 3600):
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._late_after = late_after_seconds
        self._events: dict[str, SourceEvent] = {}
        self._records: dict[str, _Record] = {}
        self._rels: dict[str, Relationship] = {}
        self._findings: dict[str, Finding] = {}
        self._outbox: dict[tuple[str, str], dict] = {}
        self._paused: set[tuple[str, str]] = set()
        self._kill_switch = False
        self._audit: list[dict] = []

    # ---- controls -------------------------------------------------------------------------
    def set_kill_switch(self, on: bool) -> None:
        with self._lock:
            self._kill_switch = bool(on)

    def pause_source(self, org_id: str, source_system: str, paused: bool = True) -> None:
        with self._lock:
            (self._paused.add if paused else self._paused.discard)((org_id, source_system))
            self._audit.append({"action": "pause" if paused else "resume", "org": org_id, "source": source_system})

    # ---- projects / investigations --------------------------------------------------------
    def upsert_record(self, kind: str, *, org_id: str, record_id: str, name: str,
                      project_ids: tuple[str, ...] = (), owner: str | None = None,
                      security_label: str = "internal", expected_version: int | None = None) -> _Record:
        if kind not in ("project", "investigation"):
            raise IntelligenceError("kind must be project or investigation")
        if not name.strip():
            raise IntelligenceError("name is required")
        with self._lock:
            key = f"{kind}:{org_id}:{record_id}"
            cur = self._records.get(key)
            if cur is not None and expected_version is not None and cur.version != expected_version:
                raise Conflict(f"{kind} is at version {cur.version}")
            if cur is None:
                scope = (record_id,) if kind == "project" else tuple(project_ids)
                rec = _Record(kind, record_id, org_id, name, scope, owner=owner, security_label=_label(security_label))
            else:
                rec = replace(cur, name=name, owner=owner if owner is not None else cur.owner, version=cur.version + 1)
            self._records[key] = rec
            return rec

    def records(self, kind: str, viewer: Viewer) -> list[_Record]:
        with self._lock:
            return sorted((r for r in self._records.values() if r.kind == kind
                           and viewer.may_see(r.org_id, r.project_ids, r.security_label)),
                          key=lambda r: r.id)

    # ---- events ---------------------------------------------------------------------------
    def append_event(self, raw: dict, *, cfg: dict) -> tuple[SourceEvent, bool]:
        """Validate and append one normalized event. Returns ``(event, created)``.

        Replaying the same source fact returns the stored event with ``created=False`` and no
        second effect (outbox included). A different digest under the same dedupe key is a
        correction: it must come as an amendment (``supersedes_id``), never a silent rewrite.
        """
        if not cfg.get("enabled") or not cfg.get("ingest"):
            raise IngestionPaused("project intelligence ingestion is not enabled")
        org = _req_str(raw, "org_id")
        system = _req_str(raw, "source_system")
        with self._lock:
            if self._kill_switch or cfg.get("kill_switch") or (org, system) in self._paused \
                    or system in cfg.get("paused_sources", ()):
                raise IngestionPaused(f"ingestion is paused for {system}")
            instance, source_id = _req_str(raw, "source_instance"), _req_str(raw, "source_id")
            version = str(raw.get("source_version") or "")
            etype = _req_str(raw, "event_type")
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            eid = event_id_for(org, system, instance, source_id, version, etype)
            digest = digest_of(payload)
            existing = self._events.get(eid)
            if existing is not None:
                if existing.content_digest != digest:
                    raise Conflict("same source fact with different content; send an amendment")
                return existing, False
            occurred = _utc(raw.get("occurred_at"), "occurred_at")
            now = self._clock()
            evidence = tuple(EvidenceRef.parse(e) for e in raw.get("provenance_refs") or [])
            ev = SourceEvent(
                event_id=eid, org_id=org, source_system=system, source_instance=instance,
                source_id=source_id, source_version=version, event_type=etype, occurred_at=occurred,
                ingested_at=now, project_ids=tuple(sorted(set(raw.get("project_ids") or ()))),
                investigation_ids=tuple(sorted(set(raw.get("investigation_ids") or ()))),
                object_ref=_req_str(raw, "object_ref"), actor_ref=raw.get("actor_ref"),
                title=str(raw.get("title") or etype)[:200], category=str(raw.get("category") or "event"),
                evidence=evidence, security_label=_label(raw.get("security_label", "internal")),
                visibility_scope=str(raw.get("visibility_scope") or "private"),
                payload_schema_version=int(raw.get("payload_schema_version") or SCHEMA_VERSION),
                payload=payload, content_digest=digest, correlation_id=raw.get("correlation_id"),
                causation_id=raw.get("causation_id"), supersedes_id=raw.get("supersedes_id"),
                late=(now - occurred).total_seconds() > self._late_after,
            )
            if ev.supersedes_id and ev.supersedes_id not in self._events:
                raise IntelligenceError("supersedes_id does not name a stored event")
            self._events[eid] = ev
            for consumer in ("timeline", "intelligence"):
                self._outbox.setdefault((eid, consumer), {"event_id": eid, "consumer": consumer, "done": False})
            return ev, True

    def has_source_version(self, org_id: str, source_system: str, source_instance: str, source_id: str, version_prefix: str) -> bool:
        """Whether any stored event already records this source fact at a version starting with ``version_prefix``."""
        with self._lock:
            return any(e.org_id == org_id and e.source_system == source_system and e.source_instance == source_instance
                       and e.source_id == source_id and e.source_version.startswith(version_prefix) for e in self._events.values())

    def account_message_refs(self, org_id: str, account: str) -> list[str]:
        """Distinct evidence source refs ``gmail:<account>:<id>`` held for one connected account."""
        prefix = f"gmail:{account}:"
        with self._lock:
            return sorted({r.source_ref for ev in self._events.values() if ev.org_id == org_id and not ev.tombstoned
                           for r in ev.evidence if r.source_ref.startswith(prefix)})

    def tombstone_source(self, org_id: str, source_ref: str) -> int:
        """Source deleted/revoked: tombstone every event citing it, so derived output is re-evaluated."""
        n = 0
        with self._lock:
            for eid, ev in list(self._events.items()):
                if ev.org_id == org_id and not ev.tombstoned and (
                        ev.object_ref == source_ref or any(e.source_ref == source_ref for e in ev.evidence)):
                    self._events[eid] = replace(ev, tombstoned=True, payload={}, title="[removed at source]")
                    n += 1
            # Derived output that cited the removed source is no longer backed by it: back to provisional review.
            for rid, rel in list(self._rels.items()):
                if rel.org_id == org_id and rel.review_state == "confirmed" and rel.derivation != "system_verified" \
                        and any(e.source_ref == source_ref for e in rel.evidence):
                    self._rels[rid] = replace(rel, review_state="provisional", version=rel.version + 1)
            # A finding stops citing a removed source; one left with no evidence is shown as unverified again.
            for fid, f in list(self._findings.items()):
                if f.org_id == org_id and any(e.source_ref == source_ref for e in f.evidence):
                    self._findings[fid] = replace(f, evidence=tuple(e for e in f.evidence if e.source_ref != source_ref))
        return n

    def outbox_pending(self, consumer: str) -> list[str]:
        with self._lock:
            return sorted(e for (e, c), row in self._outbox.items() if c == consumer and not row["done"])

    def outbox_ack(self, event_id: str, consumer: str) -> None:
        with self._lock:
            row = self._outbox.get((event_id, consumer))
            if row is not None:
                row["done"] = True

    # ---- timeline read model --------------------------------------------------------------
    def timeline(self, viewer: Viewer, *, project_id: str | None = None, investigation_id: str | None = None,
                 source_system: str | None = None, actor_ref: str | None = None, category: str | None = None,
                 as_of: datetime | None = None, limit: int = 100) -> dict:
        """Ordered activity visible to ``viewer``. ``as_of`` replays history by ingestion time.

        Totals are counted after visibility filtering, so they never reveal hidden events.
        """
        limit = max(1, min(int(limit), 500))
        with self._lock:
            events = [e for e in self._events.values() if viewer.may_see(e.org_id, e.project_ids, e.security_label)]
        if as_of is not None:
            as_of = _utc(as_of, "as_of")
            events = [e for e in events if e.ingested_at <= as_of]
        if project_id:
            events = [e for e in events if project_id in e.project_ids]
        if investigation_id:
            events = [e for e in events if investigation_id in e.investigation_ids]
        if source_system:
            events = [e for e in events if e.source_system == source_system]
        if actor_ref:
            events = [e for e in events if e.actor_ref == actor_ref]
        if category:
            events = [e for e in events if e.category == category]
        superseded = {e.supersedes_id for e in events if e.supersedes_id}
        events.sort(key=lambda e: (e.occurred_at, e.event_id))
        items = [{
            "event_id": e.event_id, "title": e.title, "category": e.category,
            "occurred_at": e.occurred_at.isoformat(), "observed_at": e.ingested_at.isoformat(),
            "actor": e.actor_ref, "evidence_refs": [r.as_dict() for r in e.evidence],
            "involved_entities": [e.object_ref], "late_arrival": e.late,
            "superseded": e.event_id in superseded, "tombstoned": e.tombstoned,
            "label": "verified" if e.evidence else "unverified",
        } for e in events[-limit:]]
        return {"items": items, "total": len(events)}

    def get_event(self, viewer: Viewer, event_id: str) -> dict:
        """One event by id under the viewer's scope; unknown and hidden are indistinguishable."""
        with self._lock:
            ev = self._events.get(event_id)
        if ev is None or not viewer.may_see(ev.org_id, ev.project_ids, ev.security_label):
            raise NotFound("event")
        return {"event_id": ev.event_id, "project_ids": list(ev.project_ids), "evidence_refs": [r.as_dict() for r in ev.evidence],
                "label": "verified" if ev.evidence else "unverified", "tombstoned": ev.tombstoned}

    # ---- relationships / findings ---------------------------------------------------------
    def add_relationship(self, raw: dict, *, cfg: dict, actor: str) -> Relationship:
        derivation = raw.get("derivation")
        predicate = raw.get("predicate")
        if derivation not in DERIVATIONS:
            raise IntelligenceError("derivation must be explicit, inferred, human_verified or system_verified")
        if predicate not in PREDICATES:
            raise IntelligenceError("predicate is not an allowed relationship type")
        evidence = tuple(EvidenceRef.parse(e) for e in raw.get("evidence_refs") or [])
        if not evidence:
            raise IntelligenceError("a relationship needs at least one evidence reference")
        confidence = raw.get("confidence")
        if derivation == "inferred":
            if not cfg.get("derive"):
                raise IntelligenceError("derivation is not enabled")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise IntelligenceError("an inferred relationship needs confidence in [0,1]")
            if not raw.get("extractor_version"):
                raise IntelligenceError("an inferred relationship needs extractor_version")
            if predicate in AUTHORITY_PREDICATES:
                raise IntelligenceError(f"{predicate} cannot be inferred; it needs a verified source")
        org = _req_str(raw, "org_id")
        rel = Relationship(
            id="rel_" + uuid.uuid4().hex, org_id=org, project_ids=tuple(sorted(set(raw.get("project_ids") or ()))),
            from_ref=_req_str(raw, "from_ref"), to_ref=_req_str(raw, "to_ref"), predicate=predicate,
            derivation=derivation, evidence=evidence, security_label=_label(raw.get("security_label", "internal")),
            review_state="provisional" if derivation == "inferred" else "confirmed",
            confidence=float(confidence) if confidence is not None else None,
            extractor_version=raw.get("extractor_version"), model_id=raw.get("model_id"),
            verified_by=None if derivation == "inferred" else actor,
        )
        with self._lock:
            # Idempotent on (from, predicate, to, org) for a live relationship.
            for cur in self._rels.values():
                if (cur.org_id, cur.from_ref, cur.predicate, cur.to_ref, cur.security_label, cur.project_ids) == \
                        (org, rel.from_ref, predicate, rel.to_ref, rel.security_label, rel.project_ids) \
                        and cur.review_state != "rejected" and cur.superseded_by is None:
                    return cur
            self._rels[rel.id] = rel
            return rel

    def review_relationship(self, rel_id: str, *, accept: bool, actor: str, expected_version: int,
                            viewer: Viewer) -> Relationship:
        with self._lock:
            cur = self._rels.get(rel_id)
            if cur is None or not viewer.may_see(cur.org_id, cur.project_ids, cur.security_label):
                raise NotFound("relationship")
            if cur.version != expected_version:
                raise Conflict(f"relationship is at version {cur.version}")
            if cur.review_state != "provisional" and cur.derivation in ("system_verified", "explicit") and accept:
                raise Conflict("a verified relationship is not re-reviewed")
            if cur.review_state == "rejected":
                raise Conflict("a rejected relationship is final; propose a new one")
            new = replace(cur, review_state="confirmed" if accept else "rejected", version=cur.version + 1,
                          derivation="human_verified" if accept else cur.derivation, verified_by=actor)
            self._rels[rel_id] = new
            self._audit.append({"action": "review", "rel": rel_id, "accept": accept, "actor": actor})
            return new

    def neighbours(self, ref: str, viewer: Viewer) -> list[Relationship]:
        with self._lock:
            return sorted((r for r in self._rels.values()
                           if ref in (r.from_ref, r.to_ref) and r.review_state != "rejected"
                           and viewer.may_see(r.org_id, r.project_ids, r.security_label)), key=lambda r: r.id)

    def add_finding(self, raw: dict) -> Finding:
        f = Finding(
            id="fnd_" + uuid.uuid4().hex, org_id=_req_str(raw, "org_id"),
            project_ids=tuple(sorted(set(raw.get("project_ids") or ()))), statement=_req_str(raw, "statement"),
            evidence=tuple(EvidenceRef.parse(e) for e in raw.get("evidence_refs") or []),
            security_label=_label(raw.get("security_label", "internal")),
            extractor_version=_req_str(raw, "extractor_version"), model_identifier=_req_str(raw, "model_identifier"))
        with self._lock:
            self._findings[f.id] = f
        return f

    def findings(self, viewer: Viewer) -> list[dict]:
        with self._lock:
            return [{"id": f.id, "statement": f.statement if f.verified else f"[unverified] {f.statement}",
                     "verified": f.verified, "evidence_refs": [r.as_dict() for r in f.evidence]}
                    for f in self._findings.values() if viewer.may_see(f.org_id, f.project_ids, f.security_label)]


def _req_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IntelligenceError(f"{key} is required")
    return value
