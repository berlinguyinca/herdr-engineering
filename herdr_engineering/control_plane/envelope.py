"""Event envelope v2: validation, server-side normalisation, per-source ledger.

Implements the "Server rules" of ``docs/contracts/event-envelope-v2.md``.
``normalize_envelope`` turns an ingest body into a trusted envelope (actor and
``received_at`` are server-set); ``SourceLedger`` provides idempotency,
``(source, source_sequence)`` collision detection, gap tracking and per-source
freshness. Everything here is in-memory and Postgres-free; the repo hydrates
the ledger from replayed events and mirrors its state to the durable sink.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from .federation import apply_clamp, clamp_required

SCHEMA_VERSION = 2
VISIBILITY_SCOPES = ("private", "mission", "room", "org")
DEFAULT_FRESHNESS_SECONDS = 300
MAX_EVIDENCE_REFS = 64
_ID_FIELDS = ("mission_id", "org_id", "project_id", "room_id", "workspace_id",
              "task_id", "agent_id", "correlation_id", "causation_id")
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_STR = 512

# Entity types whose state is derived by dedicated handlers. Their v2 envelope
# is stored as an envelope-only row: persisted for idempotency, sequence and
# replay, but kept out of the global feed and out of state reconstruction.
ENVELOPE_ONLY_TYPES = frozenset({"host", "session", "mission", "service",
                                 "inspector", "mission_activity", "lineage"})
MAX_SEQUENCE = 2**63 - 1          # PostgreSQL bigint
MAX_GAPS_PER_SOURCE = 64
MAX_SOURCES = 1024
MAX_SOURCES_PER_ACTOR = 32      # T16 M6: one principal can never push another's gap/freshness state out


class EnvelopeConflict(Exception):
    """The envelope contradicts something already stored (HTTP 409).

    ``reason`` is the machine-readable cause in the response body (``conflict`` or ``sequence_conflict``);
    ``last_sequence`` is set only for ``sequence_conflict``: the highest sequence accepted from the CALLER's own
    (principal, source) domain, so a restarted emitter can resume (never another source's state)."""

    def __init__(self, message: str, *, reason: str = "conflict", last_sequence: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.last_sequence = last_sequence


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


def normalize_envelope(body: dict, principal: dict | None, received_at: str, *, clamp=None) -> dict | None:
    """Return the trusted v2 envelope for ``body``, or None for a v1 body.

    ``principal`` is the authenticated caller (``{kind, id, authenticated_by}``);
    a client-supplied ``actor`` is never trusted and survives only as
    ``claimed_actor``. ``received_at`` always comes from the server clock.

    ``clamp`` (``federation.Clamp``) is the per-principal visibility clamp: an envelope from a clamped principal
    kind (today: ``peer``) is rewritten to the clamp's scope and room, and the sender's claims survive only as
    ``claimed_*``. A clamped kind with no clamp, and a v1 body from one, is refused (PermissionError): the clamp
    cannot be skipped by forgetting to pass it.
    """
    if clamp_required(principal) and (clamp is None or clamp.kind != principal.get("kind")):
        raise PermissionError("not permitted to modify this lineage object")
    version = body.get("schema_version")
    if version is None:
        if clamp is not None:
            raise PermissionError("not permitted to modify this lineage object")
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("schema_version must be a positive integer")
    if version > SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version}; this server accepts up to {SCHEMA_VERSION}")
    if version == 1:
        if clamp is not None:
            raise PermissionError("not permitted to modify this lineage object")
        return None

    event_id = body.get("event_id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > _MAX_STR:
        raise ValueError("event_id is required for schema_version 2")
    source = body.get("source")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source.lower().strip()):
        raise ValueError("source is required for schema_version 2")
    source = source.lower().strip()
    sequence = body.get("source_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= MAX_SEQUENCE:
        raise ValueError("source_sequence must be an integer between 0 and 2^63-1")

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
    if clamp is not None:
        apply_clamp(env, body, clamp)       # BEFORE the hash: a replay of the same event hashes the same
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


def _actor_id(actor: Any) -> str:
    """The principal half of a sequence domain ('' for an unauthenticated actor)."""
    if isinstance(actor, str):
        try:
            actor = json.loads(actor)
        except ValueError:
            actor = None
    value = actor.get("id") if isinstance(actor, dict) else None
    return value if isinstance(value, str) else ""


class SourceLedger:
    """Idempotency, sequence-collision and gap/freshness state per sequence domain.

    A sequence domain is ``(actor id, source)``: two principals may use the same
    ``source`` string without burning each other's sequences, gaps or freshness.

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
        self._events: OrderedDict[tuple, tuple] = OrderedDict()   # (actor, id) -> (hash, source, seq, actor)
        self._sequences: dict[tuple[str, str, int], str] = {}   # (actor, source, seq) -> id
        self._sources: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------ admission
    def check(self, env: dict) -> str:
        """``"new"`` or ``"duplicate"``; raises EnvelopeConflict on contradiction."""
        known = self._events.get((_actor_id(env.get("actor")), env["event_id"]))
        if known is not None:
            # known[0] is None only for rows written before content digests
            # were stored in the envelope metadata: acknowledge by identity.
            if known[0] is None and known[1:] == (
                    env["source"], env["source_sequence"], _actor_id(env.get("actor"))):
                return "duplicate"
            if known[0] == env["content_hash"]:
                return "duplicate"
            raise EnvelopeConflict(
                f"event_id {env['event_id']!r} already stored with different content")
        owner = self._sequences.get((_actor_id(env.get("actor")), env["source"], env["source_sequence"]))
        if owner is not None and owner != env["event_id"]:
            raise EnvelopeConflict(
                f"sequence {env['source_sequence']} of source {env['source']!r} "
                f"already belongs to event {owner!r}", reason="sequence_conflict",
                last_sequence=self.last_sequence(env.get("actor"), env["source"]))
        return "new"

    def last_sequence(self, actor, source: str) -> int | None:
        """Highest sequence accepted from this principal's own ``source`` (None if nothing is remembered)."""
        st = self._sources.get((_actor_id(actor), source))
        return st["last_sequence"] if st else None

    def commit(self, env: dict, seen_at: str | None = None, *, persist: bool = True) -> None:
        source, seq, eid = env["source"], env["source_sequence"], env["event_id"]
        actor = _actor_id(env.get("actor"))
        domain = (actor, source)
        self._events[(actor, eid)] = (env["content_hash"], source, seq, actor)
        self._sequences[(actor, source, seq)] = eid
        while len(self._events) > self._max_events:
            (_, old_id), (_, old_source, old_seq, old_actor) = self._events.popitem(last=False)
            if self._sequences.get((old_actor, old_source, old_seq)) == old_id:
                del self._sequences[(old_actor, old_source, old_seq)]
        if domain not in self._sources:
            mine = [st for st in self._sources.values() if st["actor"] == actor]
            if len(mine) >= MAX_SOURCES_PER_ACTOR:
                victim = min(mine, key=lambda st: st["last_seen_at"] or "")      # evict your own first
                del self._sources[(victim["actor"], victim["source"])]
            elif len(self._sources) >= MAX_SOURCES:
                counts: dict[str, int] = {}
                for st in self._sources.values():
                    counts[st["actor"]] = counts.get(st["actor"], 0) + 1
                heaviest = max(counts, key=lambda a: (counts[a], a == actor))      # the biggest holder pays
                victim = min((st for st in self._sources.values() if st["actor"] == heaviest),
                             key=lambda st: st["last_seen_at"] or "")
                del self._sources[(victim["actor"], victim["source"])]
        state = self._sources.setdefault(
            domain, {"source": source, "actor": actor, "last_sequence": None,
                     "last_seen_at": None, "gaps": []})
        state["last_seen_at"] = seen_at or env["received_at"]
        last = state["last_sequence"]
        if last is None:
            state["last_sequence"] = seq          # first arrival sets the baseline
        elif seq > last + 1:
            state["gaps"].append({"from": last + 1, "to": seq - 1, "seen_at": state["last_seen_at"]})
            if len(state["gaps"]) > MAX_GAPS_PER_SOURCE:
                # Collapse the two oldest into one coarse (conservative) range.
                first, second = state["gaps"][0], state["gaps"][1]
                state["gaps"][:2] = [{"from": first["from"], "to": second["to"],
                                      "seen_at": second["seen_at"]}]
                state["gaps_truncated"] = True
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
                meta = e.get("payload") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except ValueError:
                        meta = {}
                digest = ((meta.get("_envelope") or {}).get("content_sha256")
                          if isinstance(meta, dict) else None)
                env = {"event_id": e["event_id"], "source": e["source"], "actor": e.get("actor"),
                       "source_sequence": int(e["source_sequence"]),
                       "content_hash": digest, "received_at": str(e.get("ingest_timestamp") or "")}
                self.commit(env, persist=False)

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

    def source_state(self, source: str, actor: str | None = None) -> dict:
        """State of one sequence domain. Without ``actor``: the single domain of
        that source name, or (several principals share the name) the freshest."""
        with self._lock_ctx():
            if actor is not None:
                key = (actor, source)
            else:
                cands = [k for k in self._sources if k[1] == source]
                key = max(cands, key=lambda k: self._sources[k]["last_seen_at"] or "") if cands \
                    else ("", source)
            state = self._sources.get(key) or {
                "source": source, "actor": key[0], "last_sequence": None,
                "last_seen_at": None, "gaps": []}
            return self._view(state)

    def source_states(self, actor: str | None = None) -> list[dict]:
        """Every domain, or (T16 M4) only those of one principal."""
        with self._lock_ctx():
            return [self._view(s) for (a, _), s in sorted(self._sources.items())
                    if actor is None or a == actor]

    def _lock_ctx(self):
        return self.lock
