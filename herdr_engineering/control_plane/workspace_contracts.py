"""Unified workspace contracts (program herdr-unified-workspace-20261028, phase P01).

LIBRARY ONLY. No live route calls anything here yet; the capability ``unified_workspace_v1``
is advertised only when ``control_plane.unified_workspace.enabled`` is true (default off).
Nothing in graph.py / collab.py / events.py is changed: this module REUSES their vocabularies
(``graph.NODE_TYPES``/``EDGE_TYPES``/``can_see``, ``collab.ROLE_ACTIONS``/``AGENT_ACTIONS``,
the collab ``_idem`` fingerprint rule, ``assistant.AssistantGrant``/``AssistantApproval``
semantics, events envelope v2) and adds the cross-cutting rules P02..P09 build on:

1. canonical ids + alias table                     (``urn_for``, ``AliasTable``)
2. entity/edge semantic validation, ETag + CAS     (``validate_entity``, ``apply_update``)
3. idempotency tokens                              (``IdempotencyStore``)
4. capability/action broker                        (``Broker``: discover / authorize / approve / invoke)
5. untrusted-content tainting                      (``Untrusted``, ``summarize``)
6. ACL re-check hooks                              (``traverse``, ``search``, ``export_items``, ``SubscriptionGuard``)
7. subscribe/replay cursor contract                (``ReplayCursor``)

Deliberately free of third-party imports: JSON-Schema validation of the documents lives in
``schemas/workspace-v1.schema.json`` and is exercised by the tests (``jsonschema`` is a dev extra).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .collab import AGENT_ACTIONS, ROLE_ACTIONS
from .graph import EDGE_TYPES, NODE_TYPES

FLAG = "unified_workspace_v1"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "workspace-v1.schema.json"


def enabled(control_plane_cfg: Mapping | None) -> bool:
    """YAML-only flag (``control_plane.unified_workspace.enabled``); strict ``True`` only, default off."""
    section = (control_plane_cfg or {}).get("unified_workspace")
    return isinstance(section, Mapping) and section.get("enabled") is True


class ContractError(ValueError):
    """A document or id violates the contract."""


# --------------------------------------------------------------------------- kinds and graph mapping
ENTITY_KINDS = ("project", "team", "room", "mission", "issue", "task", "agent", "agent_session", "host",
                "artifact", "artifact_version", "review", "decision", "external_action")
EDGE_KINDS = ("contains", "assigned_to", "depends_on", "delegated_to", "spawned", "reviews", "implements",
              "produced", "derived_from", "discussed_in", "supersedes", "references", "approved_by",
              "notified_via")

# contract kind -> (graph.py node type, "exists" | "added").  "added" kinds are NOT in graph.NODE_TYPES
# today; P02 adds them (owned kinds, never COMMONS) when it wires the projection. artifact_version is a
# node of its own because versions are immutable and individually linkable (A05/A06); graph.py has no
# such type yet, so it is "added" too.
GRAPH_NODE_MAP: dict[str, tuple[str, str]] = {
    "project": ("project", "added"), "team": ("team", "added"),
    "room": ("room", "exists"), "mission": ("mission", "exists"), "issue": ("issue", "exists"),
    "task": ("task", "exists"), "agent": ("agent", "exists"), "agent_session": ("session", "exists"),
    "host": ("host", "exists"), "artifact": ("artifact", "exists"),
    "artifact_version": ("artifact_version", "added"),
    "review": ("review", "exists"), "decision": ("decision", "exists"),
    "external_action": ("external_action", "added"),
}
# contract edge -> (graph.py edge type, "exists" | "added"). Aliases onto existing kinds, never a twin.
GRAPH_EDGE_MAP: dict[str, tuple[str, str]] = {
    "contains": ("contains", "added"), "assigned_to": ("assigned_to", "exists"),
    "depends_on": ("dependency", "exists"), "delegated_to": ("delegation", "exists"),
    "spawned": ("spawn", "exists"), "reviews": ("reviewed", "exists"),
    "implements": ("implements", "exists"), "produced": ("produced", "exists"),
    "derived_from": ("derived_from", "added"), "discussed_in": ("discussed_in", "added"),
    "supersedes": ("supersession", "exists"), "references": ("references", "exists"),
    "approved_by": ("approved_by", "added"), "notified_via": ("notified_via", "added"),
}
ADDED_NODE_TYPES = frozenset(g for g, s in GRAPH_NODE_MAP.values() if s == "added")
ADDED_EDGE_TYPES = frozenset(g for g, s in GRAPH_EDGE_MAP.values() if s == "added")
EXTENDED_NODE_TYPES = frozenset(NODE_TYPES) | ADDED_NODE_TYPES     # what P02 registers; live NODE_TYPES untouched
EXTENDED_EDGE_TYPES = frozenset(EDGE_TYPES) | ADDED_EDGE_TYPES

_ANY = frozenset(ENTITY_KINDS)
_WORK = frozenset({"task", "issue", "mission"})
_ART = frozenset({"artifact", "artifact_version"})
# edge type -> (allowed from kinds, allowed to kinds). "supersedes" additionally requires equal kinds.
EDGE_ENDPOINTS: dict[str, tuple[frozenset, frozenset]] = {
    "contains": (frozenset({"project", "team", "room", "mission", "issue", "task", "artifact"}),
                 frozenset({"room", "mission", "issue", "task", "artifact", "artifact_version"})),
    "assigned_to": (frozenset({"task", "issue"}), frozenset({"agent", "team"})),
    "depends_on": (_WORK, _WORK),
    "delegated_to": (frozenset({"task", "agent", "mission"}), frozenset({"agent", "agent_session"})),
    "spawned": (frozenset({"agent", "task", "mission", "agent_session"}),
                frozenset({"agent_session", "agent", "task"})),
    "reviews": (frozenset({"review"}), _ART | _WORK),
    "implements": (frozenset({"task"}) | _ART, frozenset({"issue", "task"})),
    "produced": (frozenset({"task", "agent_session", "mission", "agent"}), _ART),
    "derived_from": (_ART, _ART),
    "discussed_in": (_WORK | _ART | {"decision"}, frozenset({"room"})),
    "supersedes": (_ANY, _ANY),
    "references": (_ANY, _ANY),
    "approved_by": (frozenset({"external_action", "review", "task"}), frozenset({"decision"})),
    "notified_via": (frozenset({"task", "external_action", "review"}), frozenset({"external_action", "room"})),
}
IMMUTABLE_KINDS = frozenset({"artifact_version"})      # a version is never updated, only superseded (A06)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def schema_for(name: str, root: dict | None = None) -> dict:
    """Root schema narrowed to one ``$defs`` entry: ``{**root, "$ref": "#/$defs/<name>"}``."""
    root = root or load_schema()
    if name not in root["$defs"]:
        raise ContractError(f"unknown schema {name!r}")
    return {**root, "$ref": f"#/$defs/{name}"}


# --------------------------------------------------------------------------- canonical ids + aliases
_NS = uuid.UUID("0b1f2c3e-4d5a-4b6c-8d7e-9f0a1b2c3d4e")           # fixed: every host derives the same urn
_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_URN_RE = re.compile(rf"urn:herdr:([a-z_]+):({_UUID_RE})")
_ALIAS_RE = re.compile(r"([a-z][a-z0-9_.-]{0,63}):(\S.{0,511})")


def urn_for(kind: str, native_id: str) -> str:
    """Deterministic canonical id for an existing entity: native ids stay authoritative (HCN room id,
    artifact_id, artifact version id, github ref) and every host maps the same native id to the same urn.
    Never derive from a display name: a renamed room keeps its urn."""
    if kind not in ENTITY_KINDS:
        raise ContractError(f"unknown kind {kind!r}")
    if not isinstance(native_id, str) or not native_id.strip() or len(native_id) > 512:
        raise ContractError("native id must be a non-empty string")
    return f"urn:herdr:{kind}:{uuid.uuid5(_NS, kind + ':' + native_id)}"


def new_urn(kind: str) -> str:
    if kind not in ENTITY_KINDS:
        raise ContractError(f"unknown kind {kind!r}")
    return f"urn:herdr:{kind}:{uuid.uuid4()}"


def parse_urn(value: Any) -> tuple[str, str]:
    """(kind, uuid). Strict: lowercase, known kind, no surrounding whitespace."""
    m = _URN_RE.fullmatch(value) if isinstance(value, str) else None
    if not m or m.group(1) not in ENTITY_KINDS:
        raise ContractError("not a canonical herdr urn")
    return m.group(1), m.group(2)


class AliasConflict(ContractError):
    pass


class AliasTable:
    """(tenant, system, value) -> canonical urn. An alias is bound once and forever: rebinding to a
    different urn is a conflict (identity is immutable across hosts, renames and restarts)."""

    def __init__(self) -> None:
        self._by_alias: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def bind(self, tenant: str, urn: str, system: str, value: str) -> None:
        parse_urn(urn)
        if not _ALIAS_RE.fullmatch(f"{system}:{value}"):
            raise ContractError("malformed alias")
        with self._lock:
            prior = self._by_alias.setdefault((tenant, system, value), urn)
            if prior != urn:
                raise AliasConflict("alias already bound to another entity")

    def resolve(self, tenant: str, system: str, value: str) -> str | None:
        return self._by_alias.get((tenant, system, value))

    def canonicalize(self, tenant: str, ref: str) -> str:
        """Accept a canonical urn (verified) or ``system:value``; raise ContractError otherwise."""
        if not isinstance(ref, str):
            raise ContractError("reference must be a string")
        if ref.startswith("urn:"):
            parse_urn(ref)
            return ref
        m = _ALIAS_RE.fullmatch(ref)
        if not m:
            raise ContractError("unresolvable reference")
        system, value = ref.split(":", 1)
        urn = self.resolve(tenant, system, value)
        if urn is None:
            raise ContractError("unresolvable reference")
        return urn


# --------------------------------------------------------------------------- validation, ETag, CAS
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})")
_REF_RE = re.compile(r"(herdr://|urn:herdr:|https://)\S{1,2000}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_etag(doc: Mapping) -> str:
    """Strong validator over every field except ``etag`` itself (so version/updated_at/body all count)."""
    return "sha256:" + hashlib.sha256(canonical_json({k: v for k, v in doc.items() if k != "etag"}).encode()).hexdigest()


def _ts(value: Any, what: str) -> None:
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        raise ContractError(f"{what}: bad timestamp")


def validate_scope(scope: Any) -> None:
    if not isinstance(scope, Mapping) or not scope.get("tenant_id"):
        raise ContractError("scope.tenant_id required")
    if scope.get("visibility") not in ("private", "mission", "room", "org"):
        raise ContractError("scope.visibility must be one of private|mission|room|org")
    if scope["visibility"] == "room" and not scope.get("room_id"):
        raise ContractError("room visibility needs scope.room_id (a bare room scope is operator-only in graph.can_see)")
    if scope["visibility"] == "mission" and not (scope.get("mission_id") and scope.get("room_id")):
        raise ContractError("mission visibility needs scope.mission_id and scope.room_id")


def validate_refs(refs: Any) -> None:
    for r in refs or ():
        if not isinstance(r, str) or not _REF_RE.fullmatch(r):
            raise ContractError("reference URI must be herdr://, urn:herdr: or https://")


def validate_entity(doc: Mapping) -> None:
    """Semantic checks JSON Schema cannot express (id/kind agreement, etag integrity, scope rules)."""
    kind, _ = parse_urn(doc.get("id"))
    if doc.get("kind") != kind:
        raise ContractError("kind does not match the urn")
    validate_scope(doc.get("scope"))
    _ts(doc.get("created_at"), "created_at")
    _ts(doc.get("updated_at"), "updated_at")
    validate_refs(doc.get("refs"))
    if doc.get("etag") != compute_etag(doc):
        raise ContractError("etag does not match content")


def validate_edge(doc: Mapping) -> None:
    if doc.get("type") not in EDGE_KINDS:
        raise ContractError("unknown edge type")
    fk, _ = parse_urn(doc.get("from_id"))
    tk, _ = parse_urn(doc.get("to_id"))
    allowed_from, allowed_to = EDGE_ENDPOINTS[doc["type"]]
    if fk not in allowed_from or tk not in allowed_to:
        raise ContractError(f"{doc['type']} not allowed from {fk} to {tk}")
    if doc["from_id"] == doc["to_id"]:
        raise ContractError("self edge")
    if doc["type"] == "supersedes" and fk != tk:
        raise ContractError("supersedes joins two entities of the same kind")
    validate_scope(doc.get("scope"))
    _ts(doc.get("created_at"), "created_at")


def validate_event(doc: Mapping) -> None:
    for u in doc.get("entity_refs") or ():
        parse_urn(u)
    validate_scope(doc.get("scope"))
    _ts(doc.get("occurred_at"), "occurred_at")
    _ts(doc.get("received_at"), "received_at")
    if doc.get("actor") and doc["actor"].get("id") != doc.get("actor_id"):
        raise ContractError("actor_id must equal actor.id")
    if doc.get("payload_ref") is not None:
        validate_refs([doc["payload_ref"]])


def event_from_envelope(ev: Any, *, tenant_id: str, entity_urns: Iterable[str] = ()) -> dict:
    """Project an ``events.Event`` (envelope v2) onto the contract event. ``trace_id`` is absent on purpose:
    events.py has no such column (P02 gap); correlation falls back to the entity id so the field is never empty."""
    actor = ev.actor if isinstance(ev.actor, dict) else {"kind": "system", "id": ev.source or "unknown"}
    vis = ev.visibility_scope if ev.visibility_scope in ("private", "mission", "room", "org") else "private"
    scope: dict[str, Any] = {"tenant_id": tenant_id, "visibility": vis}
    if vis in ("room", "mission") and ev.room_id:
        scope["room_id"] = ev.room_id
    if vis == "mission" and ev.mission_id:
        scope["mission_id"] = ev.mission_id
    out = {
        "event_id": ev.event_id, "schema_version": ev.schema_version, "event_type": ev.event_type,
        "source": ev.source or "unknown", "source_sequence": ev.source_sequence if ev.source_sequence is not None else ev.sequence,
        "actor_id": actor.get("id", "unknown"), "actor": actor, "scope": scope,
        "occurred_at": ev.source_timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "received_at": ev.ingest_timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "correlation_id": ev.correlation_id or ev.entity_id or ev.event_id,
        "entity_refs": list(entity_urns),
    }
    if ev.causation_id:
        out["causation_id"] = ev.causation_id
    return out


class PreconditionRequired(ContractError):       # 428: mutation without If-Match
    pass


class PreconditionFailed(ContractError):         # 412: stale ETag
    pass


class ImmutableEntity(ContractError):            # 409: versions and tombstoned docs are never updated
    pass


_FROZEN = frozenset({"id", "kind", "created_at", "version", "etag", "updated_at"})


def apply_update(current: Mapping, patch: Mapping, *, if_match: str | None, now: datetime | None = None,
                 actor: Mapping | None = None) -> dict:
    """Compare-and-swap: ``if_match`` must equal the current etag. Returns the new document (version+1,
    fresh updated_at/etag). Identity, kind, creation time and tenant are not patchable."""
    if current.get("kind") in IMMUTABLE_KINDS:
        raise ImmutableEntity("immutable: create a new version and a supersedes edge")
    if current.get("tombstone"):
        raise ImmutableEntity("tombstoned")
    if not if_match:
        raise PreconditionRequired("If-Match required")
    if if_match != current.get("etag"):
        raise PreconditionFailed("stale etag")
    if _FROZEN & set(patch):
        raise ContractError("patch touches a read-only field")
    if "scope" in patch and patch["scope"].get("tenant_id") != current["scope"]["tenant_id"]:
        raise ContractError("tenant cannot change")
    new = {**current, **patch}
    new["version"] = current["version"] + 1
    new["updated_at"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    if actor is not None:
        new["actor"] = dict(actor)
    new["etag"] = compute_etag(new)
    return new


# --------------------------------------------------------------------------- idempotency
class IdempotencyConflict(ContractError):        # 409: same key, different request
    pass


class InDoubt(ContractError):
    """A prior attempt with this key started an external effect and did not report back. Reconcile with the
    provider; never blindly retry (A03/A10: no duplicate irreversible action)."""


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


class IdempotencyStore:
    """Mirror of collab's ``_idem``: ``(principal, scope, key) -> (fingerprint, result)``. Same key + same
    fingerprint replays the stored result without calling ``fn``; same key + other fingerprint is a conflict.
    A failing ``fn`` is not remembered (internal retry is safe) unless ``external`` - then the key is in doubt."""

    def __init__(self) -> None:
        self._d: dict[tuple, tuple[str, Any, str]] = {}
        self._lock = threading.RLock()

    def run(self, principal: str, scope: str, key: str, fp: str, fn: Callable[[], Any], *, external: bool = False):
        k = (principal, scope, key)
        with self._lock:
            prior = self._d.get(k)
            if prior is not None:
                pfp, result, state = prior
                if pfp != fp:
                    raise IdempotencyConflict("idempotency key reused with a different request")
                if state == "in_doubt":
                    raise InDoubt("previous attempt outcome unknown; reconcile before retrying")
                return result, True
            if external:
                self._d[k] = (fp, None, "in_doubt")
            try:
                result = fn()
            except BaseException:
                if not external:
                    self._d.pop(k, None)
                raise
            self._d[k] = (fp, result, "done")
            return result, False


# --------------------------------------------------------------------------- taint
@dataclass(frozen=True)
class Untrusted:
    """Text that came from room messages, attachments or tool results. Taint is sticky and transitive."""
    value: str
    sources: frozenset[str]

    def __str__(self) -> str:                    # prints as data, never as an instruction channel
        return self.value


def taint(value: str, source: str) -> Untrusted:
    return Untrusted(str(value), frozenset({source}))


def taint_of(*items: Any) -> frozenset[str]:
    out: set[str] = set()
    for it in items:
        if isinstance(it, Untrusted):
            out |= it.sources
        elif isinstance(it, Mapping):
            out |= taint_of(*it.values())
        elif isinstance(it, (list, tuple, set, frozenset)):
            out |= taint_of(*it)
    return frozenset(out)


def summarize(parts: Iterable[Any], summarizer: Callable[[list[str]], str]) -> Any:
    """A summary of tainted material is tainted with the UNION of its inputs' taint: summarization cannot
    launder room text into a trusted instruction. Fully trusted inputs give a plain str."""
    parts = list(parts)
    text = summarizer([str(p) for p in parts])
    t = taint_of(*parts)
    return Untrusted(text, t) if t else text


def _plain(obj: Any) -> Any:
    if isinstance(obj, Untrusted):
        return obj.value
    if isinstance(obj, Mapping):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- ACL re-check hooks
class NotFoundOrForbidden(LookupError):
    """One indistinguishable error for 'missing' and 'not yours': no existence, metadata or count leak."""

    def __init__(self) -> None:
        super().__init__("not found")


CanSee = Callable[[Any, str], bool]        # (viewer, ref) -> bool; must fail closed


def require_visible(viewer: Any, ref: str, can_see: CanSee) -> None:
    if not can_see(viewer, ref):
        raise NotFoundOrForbidden


def filter_visible(viewer: Any, refs: Iterable[str], can_see: CanSee) -> list[str]:
    return [r for r in refs if can_see(viewer, r)]


def traverse(viewer: Any, root: str, neighbors: Callable[[str], Iterable[tuple[str, str]]],
             can_see_node: CanSee, can_see_edge: CanSee, *, depth: int = 2, max_nodes: int = 200) -> dict:
    """Breadth-first expansion that filters BEFORE expansion: an invisible root is NotFound; an edge is
    examined only if visible, and its far end is added (and later expanded) only if also visible. Counts,
    degrees and ``has_more`` are computed over the visible subgraph only."""
    require_visible(viewer, root, can_see_node)
    seen = {root}
    order = [root]
    edges: dict[str, tuple[str, str]] = {}
    frontier = [root]
    truncated = False
    for _ in range(max(0, depth)):
        nxt: list[str] = []
        for node in frontier:
            for edge_id, other in neighbors(node):
                if not can_see_edge(viewer, edge_id) or not can_see_node(viewer, other):
                    continue
                if other not in seen:
                    if len(order) >= max_nodes:
                        truncated = True            # a VISIBLE node was dropped: the only has_more signal
                        continue
                    seen.add(other)
                    order.append(other)
                    nxt.append(other)
                edges[edge_id] = (node, other)
        frontier = nxt
    degree = {n: 0 for n in order}
    for a, b in edges.values():
        degree[a] += 1
        degree[b] = degree.get(b, 0) + 1
    return {"nodes": order, "edges": edges, "degree": degree, "has_more": truncated}


def search(viewer: Any, entries: Iterable[Mapping], match: Callable[[Mapping], bool], can_see: CanSee,
           *, limit: int = 50) -> dict:
    """ACL first, text match second; ``total`` counts visible matches only."""
    hits = [e for e in entries if can_see(viewer, e["ref"]) and match(e)]
    return {"hits": hits[:limit], "total": len(hits)}


def export_items(viewer: Any, items: Iterable[Mapping], can_see: CanSee) -> dict:
    """Export re-checks ACL per item and strips references to entities the viewer cannot see, so a manifest
    never names, counts or links what was left out."""
    out = []
    for it in items:
        if not can_see(viewer, it["ref"]):
            continue
        clean = dict(it)
        for field_name in ("refs", "entity_refs"):
            if field_name in clean:
                clean[field_name] = [r for r in clean[field_name] if can_see(viewer, r)]
        out.append(clean)
    return {"items": out, "count": len(out)}


class SubscriptionGuard:
    """Revocation is effective on delivery: every event is re-authorized at emit time, and the first denial
    closes the subscription (mirrors room_state's re-authorize on every loop turn)."""

    def __init__(self, viewer: Any, room_ref: str, can_see: CanSee) -> None:
        require_visible(viewer, room_ref, can_see)
        self.viewer, self.room_ref, self._can_see = viewer, room_ref, can_see
        self.closed = False

    def deliver(self, event_ref: str) -> bool:
        if self.closed:
            return False
        if not (self._can_see(self.viewer, self.room_ref) and self._can_see(self.viewer, event_ref)):
            self.closed = True
            return False
        return True


# --------------------------------------------------------------------------- subscribe / replay cursor
class ReplayCursor:
    """Client side of the collab.py cursor contract (``read_events`` / ``room_state``).

    Server semantics, pinned by tests against the real CollabService:
    * ``read_events(cursor, limit)`` returns events with ``sequence > cursor`` (never below the member's join
      sequence), oldest first, at most ``limit``; ``next_cursor`` is the last sequence DELIVERED OR SKIPPED
      (events hidden from this viewer advance the cursor but do not appear); ``has_more`` says another page
      exists. Re-reading the same cursor returns the same events (at-least-once, no server-side consumption).
    * ``room_state(since, cursor, wait)``: ``changed: false`` iff the room sequence still equals ``since``.
    * Sequences visible to one viewer are NOT contiguous, so gaps are repaired by re-reading from the last
      committed cursor, never by counting.
    Client rules: advance only on the response to the request issued from the CURRENT cursor (a stale or
    reordered page is rejected whole), drop ``sequence <= cursor`` (duplicates), never regress. Applying the
    same page twice is a no-op, which makes the graph projection idempotent (A12).
    """

    def __init__(self, cursor: int = 0) -> None:
        self.cursor = cursor

    def apply_page(self, requested_cursor: int, page: Mapping) -> list[dict]:
        if requested_cursor != self.cursor:
            return []                                    # stale/reordered response: ignore, re-request
        fresh = sorted((e for e in page.get("events", ()) if e["sequence"] > self.cursor), key=lambda e: e["sequence"])
        nxt = page.get("next_cursor", self.cursor)
        if fresh:
            nxt = max(nxt, fresh[-1]["sequence"])
        self.cursor = max(self.cursor, nxt)
        return fresh


# --------------------------------------------------------------------------- capability/action broker
LOW, MEDIUM, HIGH, CRITICAL = 1, 2, 3, 4


@dataclass(frozen=True)
class ActionDef:
    action_id: str
    room_action: str                   # must be in collab.ROLE_ACTIONS (and AGENT_ACTIONS when an agent acts)
    risk: int
    read_only: bool = False
    effect: str = "internal"           # internal | shell | external
    account_tool: str | None = None    # assistant tool/grant class required for shell/external effects
    artifact_action: str | None = None  # needs an artifact grant containing this action


CATALOG: dict[str, ActionDef] = {a.action_id: a for a in (
    ActionDef("read_room", "read", LOW, read_only=True),
    ActionDef("post_message", "post", LOW),
    ActionDef("read_artifact", "read", LOW, read_only=True, artifact_action="read"),
    ActionDef("submit_task", "task", MEDIUM),
    ActionDef("invoke_skill", "execute", MEDIUM),
    ActionDef("spawn_helpers", "spawn_helpers", MEDIUM),
    ActionDef("edit_artifact", "execute", MEDIUM, artifact_action="write"),
    ActionDef("publish_artifact", "execute", HIGH, artifact_action="publish"),
    ActionDef("attach_shell", "execute", HIGH, effect="shell", account_tool="terminal"),
    ActionDef("send_email", "execute", CRITICAL, effect="external", account_tool="send_message"),
    ActionDef("invite_member", "invite", MEDIUM),
    ActionDef("grant_agent", "grant_agent", MEDIUM),
)}
assert all(a.room_action in set().union(*ROLE_ACTIONS.values()) for a in CATALOG.values())

MAX_DELEGATION_DEPTH = 4


@dataclass(frozen=True)
class Delegation:
    """An agent's authority. ``delegator`` is a user id for a root delegation, or the parent agent's id for a
    sub-delegation. Effective actions are the intersection up the chain, capped by AGENT_ACTIONS."""
    agent_id: str
    delegator: str
    actions: frozenset
    expires_at: datetime
    parent: Delegation | None = None

    def root_user(self) -> str:
        d, n = self, 0
        while d.parent is not None:
            if d.delegator != d.parent.agent_id:
                return ""                                  # laundering: child not delegated by its parent
            d, n = d.parent, n + 1
            if n > MAX_DELEGATION_DEPTH:
                return ""
        return d.delegator

    def effective(self, now: datetime) -> frozenset:
        acts, d = frozenset(self.actions) & AGENT_ACTIONS, self
        while True:
            if d.expires_at <= now:
                return frozenset()
            if d.parent is None:
                return acts
            d = d.parent
            acts = acts & frozenset(d.actions) & AGENT_ACTIONS


@dataclass(frozen=True)
class AccountGrant:
    """User-scoped external/shell authority (assistant.AssistantGrant shape). Never derived from a room."""
    grant_id: str
    user_id: str
    tool_id: str
    scope: str
    expires_at: datetime
    revoked: bool = False
    preauthorized: bool = False

    def active(self, now: datetime) -> bool:
        return not self.revoked and self.expires_at > now


def account_grant_from_assistant(g: Any) -> AccountGrant:
    return AccountGrant(g.grant_id, g.user_id, g.tool_id, g.scope, g.expires_at, g.revoked_at is not None,
                        bool((g.constraints or {}).get("preauthorized")))


class GrantSource(Protocol):
    """Authoritative lookups. The broker NEVER trusts roles or grants carried by a request."""
    def room_role(self, user_id: str, room_id: str) -> str | None: ...
    def room_grant(self, room_id: str) -> frozenset | None: ...
    def agent_delegation(self, agent_id: str, room_id: str) -> Delegation | None: ...
    def artifact_grant(self, subject_id: str, artifact_id: str) -> frozenset | None: ...
    def account_grants(self, user_id: str) -> Iterable[AccountGrant]: ...


@dataclass
class StaticGrantSource:
    roles: dict = field(default_factory=dict)              # (user, room) -> role
    room_grants: dict = field(default_factory=dict)        # room -> frozenset | absent = unrestricted
    delegations: dict = field(default_factory=dict)        # (agent, room) -> Delegation
    artifact_grants: dict = field(default_factory=dict)    # (subject, artifact) -> frozenset
    accounts: dict = field(default_factory=dict)           # user -> [AccountGrant]

    def room_role(self, user_id, room_id):
        return self.roles.get((user_id, room_id))

    def room_grant(self, room_id):
        return self.room_grants.get(room_id)

    def agent_delegation(self, agent_id, room_id):
        return self.delegations.get((agent_id, room_id))

    def artifact_grant(self, subject_id, artifact_id):
        return self.artifact_grants.get((subject_id, artifact_id))

    def account_grants(self, user_id):
        return list(self.accounts.get(user_id, ()))


@dataclass(frozen=True)
class ToolRequest:
    action_id: str
    user_id: str                        # the human the action is for (account owner / delegator)
    room_id: str | None = None
    agent_id: str | None = None         # set when an agent acts on the user's behalf
    artifact_id: str | None = None
    scope: str = ""
    args: Mapping = field(default_factory=dict)
    taint: frozenset = frozenset()      # taint of the context the proposal was derived from
    idempotency_key: str = ""


@dataclass(frozen=True)
class Decision:
    outcome: str                        # allow | needs_approval | deny
    reasons: tuple[str, ...] = ()
    binding: str = ""                   # what an approval must be bound to (args + subject + policy snapshot)
    waivable: bool = False              # False: a preauthorized grant cannot replace the human approval
    grant_id: str = ""


def _deny(*why: str) -> Decision:
    return Decision("deny", tuple(why))


@dataclass(frozen=True)
class Approver:
    user_id: str
    kind: str                           # "user" only; an agent, service or content can never approve


@dataclass
class ApprovalRecord:
    approval_id: str
    binding: str
    approver: str
    expires_at: datetime
    used: bool = False


class ApprovalError(ContractError):
    pass


class ApprovalLedger:
    """Single-use, expiring approvals bound to an exact request digest (cf. assistant.AssistantApproval)."""

    def __init__(self, ttl: timedelta = timedelta(hours=1)) -> None:
        self.ttl = ttl
        self._a: dict[str, ApprovalRecord] = {}
        self._lock = threading.Lock()

    def issue(self, decision: Decision, approver: Approver, request: ToolRequest, now: datetime) -> str:
        if decision.outcome != "needs_approval":
            raise ApprovalError("nothing to approve")
        if approver.kind != "user" or approver.user_id != request.user_id:
            raise ApprovalError("only the human the action is for can approve it")
        aid = "appr_" + uuid.uuid4().hex
        with self._lock:
            self._a[aid] = ApprovalRecord(aid, decision.binding, approver.user_id, now + self.ttl)
        return aid

    def consume(self, approval_id: str | None, binding: str, now: datetime) -> None:
        with self._lock:
            rec = self._a.get(approval_id or "")
            if rec is None:
                raise ApprovalError("approval required")
            if rec.used:
                raise ApprovalError("approval already used")
            if rec.expires_at <= now:
                raise ApprovalError("approval expired")
            if rec.binding != binding:
                raise ApprovalError("approval does not match this request or the current policy")
            rec.used = True


class AuditLog:
    """Append-only, hash-chained (tamper-evident) record of actor / intent / evidence / outcome."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, **fields: Any) -> dict:
        prev = self.entries[-1]["hash"] if self.entries else ""
        body = {**fields, "prev": prev}
        body["hash"] = hashlib.sha256(canonical_json(body).encode()).hexdigest()
        self.entries.append(body)
        return body

    def verify(self) -> bool:
        prev = ""
        for e in self.entries:
            body = {k: v for k, v in e.items() if k != "hash"}
            if body.get("prev") != prev or hashlib.sha256(canonical_json(body).encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True


class InvocationDenied(ContractError):
    pass


@dataclass
class InvocationResult:
    result: Untrusted                   # tool output is untrusted by definition
    replayed: bool


class Broker:
    """discover / authorize / approve / invoke.

    allowed = user role  ∩  agent delegation chain (∩ AGENT_ACTIONS)  ∩  room grant  ∩  artifact grant
              ∩ action capability  ∩  account grant (shell/external only)  ∩  risk policy.
    Room membership never produces a shell or external-account capability. Fail closed on anything unknown."""

    def __init__(self, source: GrantSource, *, ledger: ApprovalLedger | None = None,
                 audit: AuditLog | None = None, idem: IdempotencyStore | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.source, self.ledger = source, ledger or ApprovalLedger()
        self.audit, self.idem = audit or AuditLog(), idem or IdempotencyStore()
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- effective room authority ------------------------------------------------------------
    def _room_actions(self, req: ToolRequest, now: datetime) -> frozenset | None:
        if req.room_id is None:
            return None
        role = self.source.room_role(req.user_id, req.room_id)
        if role not in ROLE_ACTIONS:
            return frozenset()
        acts = ROLE_ACTIONS[role]
        if req.agent_id is not None:
            d = self.source.agent_delegation(req.agent_id, req.room_id)
            if d is None or d.agent_id != req.agent_id or d.root_user() != req.user_id:
                return frozenset()
            acts = acts & d.effective(now)
        grant = self.source.room_grant(req.room_id)
        return acts if grant is None else acts & frozenset(grant)

    def authorize(self, req: ToolRequest, now: datetime | None = None) -> Decision:
        now = now or self._clock()
        act = CATALOG.get(req.action_id)
        if act is None:
            return _deny("unknown_action")
        room_acts = self._room_actions(req, now)
        if room_acts is not None and act.room_action not in room_acts:
            return _deny("room_authority")
        if room_acts is None and req.agent_id is not None:
            return _deny("agent_requires_room")             # agent authority exists only inside a room delegation
        if act.artifact_action:
            if not req.artifact_id:
                return _deny("artifact_required")
            for subject in {req.user_id, req.agent_id} - {None}:
                g = self.source.artifact_grant(subject, req.artifact_id)
                if g is None or act.artifact_action not in g:
                    return _deny("artifact_grant")
        account: AccountGrant | None = None
        if act.effect != "internal":
            for g in self.source.account_grants(req.user_id):
                if g.user_id == req.user_id and g.tool_id == act.account_tool and g.scope == req.scope and g.active(now):
                    account = g
                    break
            if account is None:
                return _deny("account_grant")
        tset = frozenset(req.taint) | taint_of(dict(req.args))
        binding = fingerprint({
            "a": act.action_id, "u": req.user_id, "ag": req.agent_id, "r": req.room_id, "art": req.artifact_id,
            "s": req.scope, "args": _plain(dict(req.args)),
            "policy": {"room": sorted(room_acts) if room_acts is not None else None,
                       "grant": account.grant_id if account else None, "risk": act.risk}})
        gid = account.grant_id if account else ""
        if act.risk >= CRITICAL:
            return Decision("needs_approval", ("critical_risk",), binding, False, gid)
        if tset and not act.read_only:
            return Decision("needs_approval", ("untrusted_context",), binding, False, gid)
        if act.risk >= HIGH and not (account and account.preauthorized):
            return Decision("needs_approval", ("high_risk",), binding, True, gid)
        return Decision("allow", (), binding, True, gid)

    def discover(self, user_id: str, room_id: str | None, *, agent_id: str | None = None,
                 artifact_id: str | None = None, scope: str = "") -> list[dict]:
        """Only what the subject could actually attempt; denied actions are not named (no capability leak)."""
        out = []
        for aid in CATALOG:
            d = self.authorize(ToolRequest(aid, user_id, room_id, agent_id, artifact_id, scope))
            if d.outcome != "deny":
                out.append({"action_id": aid, "requires_approval": d.outcome == "needs_approval"})
        return out

    def approve(self, req: ToolRequest, approver: Approver) -> str:
        now = self._clock()
        decision = self.authorize(req, now)
        try:
            aid = self.ledger.issue(decision, approver, req, now)
        except ApprovalError:
            self.audit.append(event="approval.refused", actor=approver.user_id, action=req.action_id)
            raise
        self.audit.append(event="approval.issued", actor=approver.user_id, action=req.action_id, approval=aid)
        return aid

    def invoke(self, req: ToolRequest, handler: Callable[[dict], Any], *, approval_id: str | None = None) -> InvocationResult:
        now = self._clock()
        act = CATALOG.get(req.action_id)
        decision = self.authorize(req, now)            # ALWAYS re-evaluated at use time (revocation, expiry)
        who = req.agent_id or req.user_id
        if decision.outcome == "deny":
            self.audit.append(event="invoke.denied", actor=who, action=req.action_id, reasons=list(decision.reasons))
            raise InvocationDenied("denied")
        idem_key = req.idempotency_key
        if not idem_key:
            raise InvocationDenied("idempotency_key required")

        def run() -> Untrusted:
            if decision.outcome == "needs_approval":
                try:
                    self.ledger.consume(approval_id, decision.binding, now)
                except ApprovalError as exc:
                    self.audit.append(event="invoke.denied", actor=who, action=req.action_id, reasons=[str(exc)])
                    raise InvocationDenied(str(exc)) from exc
            out = handler(_plain(dict(req.args)))
            self.audit.append(event="invoke.done", actor=who, intent=req.action_id, on_behalf_of=req.user_id,
                              room=req.room_id, approval=approval_id, binding=decision.binding)
            return out if isinstance(out, Untrusted) else Untrusted(str(out), frozenset({f"tool:{req.action_id}"}))

        result, replayed = self.idem.run(f"user:{req.user_id}", req.room_id or "-", idem_key, decision.binding, run,
                                         external=bool(act and act.effect == "external"))
        return InvocationResult(result, replayed)

    def delegate(self, parent_actions: frozenset, requested: frozenset, *, context_taint: frozenset = frozenset()) -> frozenset:
        """Grant a sub-agent authority: never more than the parent holds, never beyond AGENT_ACTIONS, and
        never at all on the say-so of untrusted content."""
        if context_taint:
            raise InvocationDenied("delegation cannot be derived from untrusted content")
        return frozenset(requested) & frozenset(parent_actions) & AGENT_ACTIONS
