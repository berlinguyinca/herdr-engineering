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
import math
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from .federation import apply_clamp, clamp_required

SCHEMA_VERSION = 2
VISIBILITY_SCOPES = ("private", "mission", "room", "org")
DEFAULT_FRESHNESS_SECONDS = 300
MAX_EVIDENCE_REFS = 64
_ID_FIELDS = ("mission_id", "org_id", "project_id", "room_id", "workspace_id",
              "task_id", "agent_id", "correlation_id", "causation_id", "trace_id")
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_STR = 512

# Agent Operations (docs/specs/2026-11-12-agent-operations-map): additive, optional envelope fields. Absent = unknown.
AGENT_OPS_PROVENANCE = frozenset({"observed", "derived", "inferred", "unknown"})
AGENT_OPS_ID_FIELDS = ("host_id", "session_id", "model_id", "parent_session_id", "handoff_id")
_STATUS_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_OPS_EVENT_ID_RE = re.compile(r"[A-Za-z0-9._:~-]{1,128}")    # a cursor / SSE `id:` is built from it: no CR/LF/NUL
_OPS_ID_RE = re.compile(r"[A-Za-z0-9._:@-]{1,160}")
_OPS_MODEL_RE = re.compile(r"[A-Za-z0-9._:@/+-]{1,160}")       # provider/model ids carry a slash
LABEL_MAX = 80
_LABEL_REDACTIONS = (
    (re.compile(r"(?i)\b(?:authorization:\s*)?bearer\s+\S+"), "[redacted]"),
    (re.compile(r"(?i)\b[\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key)[\w-]*\s*(?:[=:]|\bis\b|\bwas\b)\s*\S+"),
     "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?"), "[redacted]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "[redacted]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}"), "[redacted]"),
    (re.compile(r"[A-Za-z]:\\\S+"), "[path]"),
    (re.compile(r"(?<![\w:])(?:~|\.{1,2})?(?:/[^\s/]+)+/?"), "[path]"),
    (re.compile(r"(?<![\w:/.-])[\w.-]+(?:/[\w.-]+)+/?"), "[path]"),      # relative: src/app/main.py
)

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


def sanitize_label(value: str) -> str:
    """A short human label with file paths and secret-looking tokens removed (metadata only, never content)."""
    if not isinstance(value, str):
        raise ValueError("sanitized_label must be a string")
    # controls first (zero-width / bidi / format characters would otherwise split a secret past the patterns)
    value = "".join(" " if ch.isspace() else "" if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in value)
    for pattern, replacement in _LABEL_REDACTIONS:
        value = pattern.sub(replacement, value)
    value = " ".join(value.split())
    return value if len(value) <= LABEL_MAX else value[:LABEL_MAX - 1] + "\u2026"


def _agent_ops(body: dict) -> dict | None:
    """The optional Agent Operations block, or None when the emitter sent none of its fields."""
    provenance = body.get("provenance")
    if provenance is not None and (not isinstance(provenance, str) or provenance not in AGENT_OPS_PROVENANCE):
        raise ValueError("provenance must be one of " + ", ".join(sorted(AGENT_OPS_PROVENANCE)))
    ops: dict[str, Any] = {name: _opt_str(body, name) for name in AGENT_OPS_ID_FIELDS}
    if any(v is not None and not (_OPS_MODEL_RE if name == "model_id" else _OPS_ID_RE).fullmatch(v)
           for name, v in ops.items()):
        raise ValueError("agent operations ids must match [A-Za-z0-9._:@-]{1,160} (model_id also allows / and +)")
    status = body.get("status")
    if status is not None and (not isinstance(status, str) or not _STATUS_RE.fullmatch(status)):
        raise ValueError("status must be a short lowercase token")
    ops["status"] = status
    label = body.get("sanitized_label")
    ops["sanitized_label"] = None if label is None else sanitize_label(label)
    if provenance is None and all(v is None for v in ops.values()):
        return None
    ops["provenance"] = provenance or "unknown"
    return {k: v for k, v in ops.items() if v is not None}


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
    ops = _agent_ops(body)
    if ops is not None:
        if not _OPS_EVENT_ID_RE.fullmatch(event_id):
            raise ValueError("event_id of an agent operations event must match [A-Za-z0-9._:~-]{1,128}")
        if any(env[n] is not None and not _OPS_ID_RE.fullmatch(env[n]) for n in ("agent_id", "task_id", "room_id")):
            raise ValueError("agent_id, task_id and room_id of an agent operations event must match "
                             "[A-Za-z0-9._:@-]{1,160}")
        env["agent_ops"] = ops
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
        **({"agent_ops": env["agent_ops"]} if "agent_ops" in env else {}),
    }
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def envelope_event_fields(env: dict) -> dict:
    """The stored-event keys an envelope contributes (see events.V2_COLUMNS)."""
    return {name: env.get(name) for name in (
        "source", "source_sequence", "causation_id", "correlation_id", "actor",
        "visibility_scope", "evidence_refs", "org_id", "project_id", "room_id",
        "workspace_id", "task_id", "agent_id", "trace_id")}


def _actor_id(actor: Any) -> str:
    """The principal half of a sequence domain ('' for an unauthenticated actor)."""
    if isinstance(actor, str):
        try:
            actor = json.loads(actor)
        except ValueError:
            actor = None
    value = actor.get("id") if isinstance(actor, dict) else None
    return value if isinstance(value, str) else ""


def _checkpoint_require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid projection checkpoint schema")


def _checkpoint_object(value, fields, optional=()):
    _checkpoint_require(type(value) is dict and set(fields) <= value.keys()
                        and value.keys() <= set(fields) | set(optional))


def _checkpoint_json(value):
    """Copy strict JSON without coercing tuple keys, objects or nonfinite floats."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("projection checkpoint must contain strict JSON values")
    try:
        check(value)
        return copy.deepcopy(value)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("invalid projection checkpoint") from exc


def _checkpoint_header(state, domain, fields):
    state = _checkpoint_json(state)
    _checkpoint_object(state, ("version", "domain", *fields))
    _checkpoint_require(type(state["version"]) is int and state["version"] == 1
                        and state["domain"] == domain)
    return state


def _checkpoint_int(value, minimum=0):
    _checkpoint_require(type(value) is int and value >= minimum)


def _checkpoint_time(value):
    try:
        _checkpoint_require(type(value) in (int, float) and math.isfinite(value))
    except OverflowError as exc:
        raise ValueError("invalid checkpoint number") from exc


def _checkpoint_stamp(value):
    _checkpoint_require(type(value) is str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _checkpoint_require(parsed.tzinfo is not None and math.isfinite(parsed.timestamp()))
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid checkpoint timestamp") from exc


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

    def export_checkpoint(self) -> dict:
        """Versioned bounded admission state; ordered arrays preserve eviction ties."""
        with self.lock:
            return _checkpoint_json({
                "version": 1, "domain": "source_ledger",
                "events": [[a, eid, digest, source, seq, owner]
                           for (a, eid), (digest, source, seq, owner) in self._events.items()],
                "sources": list(self._sources.values()),
            })

    def restore_checkpoint(self, state) -> None:
        """Replace admission state without persisting or changing runtime bindings."""
        state = _checkpoint_header(state, "source_ledger", ("events", "sources"))
        _checkpoint_require(type(state["events"]) is list and type(state["sources"]) is list)
        events, sequences, sources = OrderedDict(), {}, {}
        for row in state["events"]:
            _checkpoint_require(type(row) is list and len(row) == 6)
            actor, eid, digest, source, seq, owner = row
            _checkpoint_require(all(type(v) is str for v in (actor, eid, source, owner))
                                and bool(eid) and bool(source) and owner == actor
                                and (digest is None or type(digest) is str))
            _checkpoint_int(seq)
            _checkpoint_require((actor, eid) not in events and (actor, source, seq) not in sequences)
            events[(actor, eid)] = (digest, source, seq, owner)
            sequences[(actor, source, seq)] = eid
        counts = {}
        for row in state["sources"]:
            _checkpoint_object(row, ("actor", "source", "last_sequence", "last_seen_at", "gaps"),
                               ("gaps_truncated",))
            actor, source = row["actor"], row["source"]
            _checkpoint_require(type(actor) is str and type(source) is str and bool(source)
                                and (actor, source) not in sources and type(row["gaps"]) is list)
            if row["last_sequence"] is not None:
                _checkpoint_int(row["last_sequence"])
            if row["last_seen_at"] is not None:
                _checkpoint_stamp(row["last_seen_at"])
            if "gaps_truncated" in row:
                _checkpoint_require(type(row["gaps_truncated"]) is bool)
            for gap in row["gaps"]:
                _checkpoint_object(gap, ("from", "to", "seen_at"))
                _checkpoint_int(gap["from"])
                _checkpoint_int(gap["to"], gap["from"])
                _checkpoint_stamp(gap["seen_at"])
                _checkpoint_require(row["last_sequence"] is not None and gap["to"] < row["last_sequence"])
            ordered_gaps = sorted(row["gaps"], key=lambda gap: gap["from"])
            _checkpoint_require(all(left["to"] < right["from"]
                                    for left, right in zip(ordered_gaps, ordered_gaps[1:])))
            _checkpoint_require(len(row["gaps"]) <= MAX_GAPS_PER_SOURCE)
            counts[actor] = counts.get(actor, 0) + 1
            sources[(actor, source)] = row
        _checkpoint_require(len(sources) <= MAX_SOURCES
                            and all(n <= MAX_SOURCES_PER_ACTOR for n in counts.values()))
        # Apply the current cache budget, retaining the newest admission entries.
        while len(events) > self._max_events:
            (actor, _), (_, source, seq, _) = events.popitem(last=False)
            sequences.pop((actor, source, seq), None)
        with self.lock:
            self._events, self._sequences, self._sources = events, sequences, sources
            self.lock = threading.RLock()

    def replay_tail(self, events) -> None:
        """Apply rows in caller-supplied durable replay order, retaining the prefix.

        The caller orders by replay_position (legacy bootstrap ordering is its
        responsibility). Timestamps remain freshness evidence, never a sort key.
        """
        pending = []
        for row in events:
            if row.get("source") is None or row.get("source_sequence") is None:
                continue
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError as exc:
                    raise ValueError("invalid replay payload") from exc
            _checkpoint_require(type(payload) is dict)
            meta = payload.get("_envelope") or {}
            _checkpoint_require(type(meta) is dict)
            seq = row["source_sequence"]
            _checkpoint_int(seq)
            actor = row.get("actor")
            if isinstance(actor, str):
                try:
                    actor = json.loads(actor)
                except ValueError as exc:
                    raise ValueError("invalid replay actor") from exc
            _checkpoint_require(actor is None or type(actor) is dict)
            _checkpoint_require(type(row.get("event_id")) is str and bool(row["event_id"])
                                and type(row["source"]) is str and bool(row["source"]))
            received = row.get("ingest_timestamp")
            if isinstance(received, datetime):
                received = received.isoformat()
            _checkpoint_stamp(received)
            digest = meta.get("content_sha256")
            _checkpoint_require(digest is None or type(digest) is str)
            pending.append({"event_id": row["event_id"], "source": row["source"],
                            "actor": actor, "source_sequence": seq,
                            "content_hash": digest, "received_at": received})
        with self.lock:
            for env in pending:
                if self.check(env) == "new":
                    self.commit(env, persist=False)

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

    def hydrate(self, events: list[dict], *, preserve_order: bool = False) -> None:
        """Replace the ledger from durable rows without persistence callbacks.

        The default retains legacy ingest-timestamp ordering. With preserve_order,
        consume the caller's authoritative admission order, including backdates;
        database ordering columns are never copied into the ledger checkpoint.
        """
        with self.lock:
            self._events.clear()
            self._sequences.clear()
            self._sources.clear()
            rows = [e for e in events if e.get("source") is not None
                    and e.get("source_sequence") is not None]
            if not preserve_order:
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
                received = e.get("ingest_timestamp")
                received = received.isoformat() if isinstance(received, datetime) else str(received or "")
                env = {"event_id": e["event_id"], "source": e["source"], "actor": e.get("actor"),
                       "source_sequence": int(e["source_sequence"]),
                       "content_hash": digest, "received_at": received}
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
