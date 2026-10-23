"""Event envelope v2: validation, server-side normalisation, per-source ledger.

Implements the "Server rules" of ``docs/contracts/event-envelope-v2.md``.
``normalize_envelope`` turns an ingest body into a trusted envelope (actor and
``received_at`` are server-set); ``SourceLedger`` provides idempotency,
``(source, source_sequence)`` collision detection, gap tracking and per-source
freshness. Everything here is in-memory and Postgres-free; the repo hydrates
the ledger from replayed events and mirrors its state to the durable sink.
"""
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 2
VISIBILITY_SCOPES = ("private", "mission", "room", "org")
DEFAULT_FRESHNESS_SECONDS = 300
MAX_EVIDENCE_REFS = 64
_ID_FIELDS = ("mission_id", "org_id", "project_id", "room_id", "workspace_id",
              "task_id", "agent_id", "correlation_id", "causation_id")
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_STR = 512

# The envelope of the ingest call in progress. ``ControlPlaneRepo.record_event``
# attaches it to the first event it records for the matching entity type, so
# typed handlers (host/session/mission/service) and the generic path share one
# mechanism without each learning about envelopes.
current_envelope: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "herdr_current_envelope", default=None)


class EnvelopeConflict(Exception):
    """The envelope contradicts something already stored (HTTP 409)."""


def _opt_str(body: dict, name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_STR:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _evidence_refs(value: Any) -> list[dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_REFS:
        raise ValueError("evidence_refs must be a list of references")
    refs = []
    for ref in value:
        if (not isinstance(ref, dict) or not isinstance(ref.get("kind"), str)
                or not isinstance(ref.get("uri"), str)
                or not ref["kind"] or not ref["uri"]):
            raise ValueError("evidence_refs entries need string kind and uri")
        out = {"kind": ref["kind"], "uri": ref["uri"]}
        for optional in ("digest", "revision"):
            if ref.get(optional) is not None:
                if not isinstance(ref[optional], str):
                    raise ValueError(f"evidence_refs {optional} must be a string")
                out[optional] = ref[optional]
        refs.append(out)
    return refs


def _utc(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must carry a UTC offset")
    return parsed.astimezone(UTC).isoformat()


def normalize_envelope(body: dict, principal: dict | None, received_at: str) -> dict | None:
    """Return the trusted v2 envelope for ``body``, or None for a v1 body.

    ``principal`` is the authenticated caller (``{kind, id, authenticated_by}``);
    a client-supplied ``actor`` is never trusted and survives only as
    ``claimed_actor``. ``received_at`` always comes from the server clock.
    """
    version = body.get("schema_version")
    if version is None:
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("schema_version must be a positive integer")
    if version > SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version}; this server accepts up to {SCHEMA_VERSION}")
    if version == 1:
        return None

    event_id = body.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > _MAX_STR:
        raise ValueError("event_id is required for schema_version 2")
    source = body.get("source")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source.lower().strip()):
        raise ValueError("source is required for schema_version 2")
    source = source.lower().strip()
    sequence = body.get("source_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("source_sequence must be an integer >= 0")

    requested = body.get("visibility_scope")
    scope = requested if isinstance(requested, str) and requested in VISIBILITY_SCOPES else "private"

    env: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "source": source,
        "source_sequence": sequence,
        "received_at": received_at,
        "occurred_at": _utc(body["occurred_at"], "occurred_at") if body.get("occurred_at") is not None else None,
        "visibility_scope": scope,
        "evidence_refs": _evidence_refs(body.get("evidence_refs")),
        "actor": dict(principal) if principal else {
            "kind": "unknown", "id": None, "authenticated_by": "none"},
        "claimed_actor": body.get("actor") if "actor" in body else None,
    }
    for name in _ID_FIELDS:
        env[name] = _opt_str(body, name)
    env["content_hash"] = _content_hash(body, env)
    return env


def _content_hash(body: dict, env: dict) -> str:
    """Digest of the emitter-controlled content (never actor or received_at)."""
    content = {
        "entity_type": body.get("entity_type"), "entity_id": body.get("entity_id"),
        "event_type": body.get("event_type"), "payload": body.get("payload") or {},
        "source": env["source"], "source_sequence": env["source_sequence"],
        "occurred_at": env["occurred_at"], "visibility_scope": env["visibility_scope"],
        "evidence_refs": env["evidence_refs"],
        **{name: env[name] for name in _ID_FIELDS},
    }
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def envelope_event_fields(env: dict) -> dict:
    """The stored-event keys an envelope contributes (see events.V2_COLUMNS)."""
    return {name: env.get(name) for name in (
        "source", "source_sequence", "causation_id", "correlation_id", "actor",
        "visibility_scope", "evidence_refs", "org_id", "project_id", "room_id",
        "workspace_id", "task_id", "agent_id")}


class SourceLedger:
    """Idempotency, sequence-collision and gap/freshness state per source.

    ``check`` is read-only; ``commit`` records an envelope once it has been
    applied, so a failed ingest leaves no trace and can be retried. Callers
    hold ``lock`` across check -> apply -> commit. ``max_events`` bounds the
    in-memory event index (oldest evicted); the durable unique indexes are the
    backstop beyond it.
    """

    def __init__(self, *, now=None, freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
                 max_events: int = 100_000, persist=None):
        self.lock = threading.RLock()
        self._now = now or time.time
        self.freshness_seconds = freshness_seconds
        self._max_events = max_events
        self._persist = persist
        self._events: OrderedDict[str, tuple[str, str, int]] = OrderedDict()
        self._sequences: dict[tuple[str, int], str] = {}
        self._sources: dict[str, dict] = {}

    # ------------------------------------------------------------ admission
    def check(self, env: dict) -> str:
        """``"new"`` or ``"duplicate"``; raises EnvelopeConflict on contradiction."""
        known = self._events.get(env["event_id"])
        if known is not None:
            # known[0] is None for an event rebuilt from the durable log: the
            # stored row cannot reproduce the emitter's hash, so a retry with
            # the same (source, sequence) is acknowledged, not compared.
            if known[0] is None and (known[1], known[2]) == (env["source"], env["source_sequence"]):
                return "duplicate"
            if known[0] == env["content_hash"]:
                return "duplicate"
            raise EnvelopeConflict(
                f"event_id {env['event_id']!r} already stored with different content")
        owner = self._sequences.get((env["source"], env["source_sequence"]))
        if owner is not None and owner != env["event_id"]:
            raise EnvelopeConflict(
                f"sequence {env['source_sequence']} of source {env['source']!r} "
                f"already belongs to event {owner!r}")
        return "new"

    def commit(self, env: dict, seen_at: str | None = None, *, persist: bool = True) -> None:
        source, seq, eid = env["source"], env["source_sequence"], env["event_id"]
        self._events[eid] = (env["content_hash"], source, seq)
        self._sequences[(source, seq)] = eid
        while len(self._events) > self._max_events:
            old_id, (_, old_source, old_seq) = self._events.popitem(last=False)
            if self._sequences.get((old_source, old_seq)) == old_id:
                del self._sequences[(old_source, old_seq)]
        state = self._sources.setdefault(
            source, {"source": source, "last_sequence": None, "last_seen_at": None, "gaps": []})
        state["last_seen_at"] = seen_at or env["received_at"]
        last = state["last_sequence"]
        if last is None:
            state["last_sequence"] = seq          # first arrival sets the baseline
        elif seq > last + 1:
            state["gaps"].append({"from": last + 1, "to": seq - 1, "seen_at": state["last_seen_at"]})
            state["last_sequence"] = seq
        elif seq == last + 1:
            state["last_sequence"] = seq
        else:
            self._close_gap(state, seq)           # late arrival: may fill a gap
        if persist and self._persist is not None:
            try:
                self._persist(copy.deepcopy(state))
            except Exception:
                pass  # mirroring state must never fail an acknowledged ingest

    @staticmethod
    def _close_gap(state: dict, seq: int) -> None:
        for gap in state["gaps"]:
            if gap["from"] <= seq <= gap["to"]:
                state["gaps"].remove(gap)
                for lo, hi in ((gap["from"], seq - 1), (seq + 1, gap["to"])):
                    if lo <= hi:
                        state["gaps"].append({"from": lo, "to": hi, "seen_at": gap["seen_at"]})
                state["gaps"].sort(key=lambda g: g["from"])
                return

    def hydrate(self, events: list[dict]) -> None:
        """Rebuild from replayed event dicts (restart); arrival order preserved."""
        with self.lock:
            self._events.clear()
            self._sequences.clear()
            self._sources.clear()
            rows = [e for e in events if e.get("source") is not None
                    and e.get("source_sequence") is not None]
            rows.sort(key=lambda e: str(e.get("ingest_timestamp") or ""))
            for e in rows:
                env = {"event_id": e["event_id"], "source": e["source"],
                       "source_sequence": int(e["source_sequence"]),
                       "content_hash": "", "received_at": str(e.get("ingest_timestamp") or "")}
                self.commit(env, persist=False)
                # A replayed event has no hash to compare; mark it unknown so a
                # same-id retry is acknowledged and a collision is still caught.
                self._events[e["event_id"]] = (None, env["source"], env["source_sequence"])

    # ---------------------------------------------------------------- views
    def _view(self, state: dict) -> dict:
        seen = state["last_seen_at"]
        age = None
        if seen:
            try:
                age = max(0.0, self._now() - datetime.fromisoformat(seen).timestamp())
            except ValueError:
                age = None
        stale = age is None or age > self.freshness_seconds
        return {**copy.deepcopy(state), "open_gaps": len(state["gaps"]),
                "age_seconds": age, "freshness_window_seconds": self.freshness_seconds,
                "status": "stale" if stale else "healthy"}

    def source_state(self, source: str) -> dict:
        with self.lock:
            state = self._sources.get(source) or {
                "source": source, "last_sequence": None, "last_seen_at": None, "gaps": []}
            return self._view(state)

    def source_states(self) -> list[dict]:
        with self.lock:
            return [self._view(s) for _, s in sorted(self._sources.items())]
