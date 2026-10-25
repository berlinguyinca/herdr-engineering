"""Server-federation groundwork (ADR-0008 phase P1): the ``peer`` principal, its clamp and its namespace.

This module carries NO network code and nothing here is reachable unless an operator turns
``control_plane.federation`` on in the central YAML (default off, hot-reread, fail closed, no environment
variable). It defines three things a later phase (P2 registry, P3 puller) builds on:

* the ``peer`` principal kind: minted only from that YAML, never ``operator``/``collab``/``external``, never from
  an API key (``api.py`` maps a key whose role spells a peer kind to ``unknown``);
* the per-principal visibility clamp (:class:`Clamp`, :func:`clamp_for_principal`, :func:`apply_clamp`): an event
  from a clamped principal is rewritten to the clamp's scope and bound to ONE local room, and what the sender
  claimed survives only as ``claimed_*`` provenance;
* the reserved ``peer:<peer>.`` lineage node namespace, writable by nobody locally.

The clamp table is keyed by principal kind so another untrusted kind can be added by adding a row (and a test).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PEER_KIND = "peer"
PEER_USER_KIND = "peer-user"           # P3 shadow principal for a remote human; it never ingests
PEER_AUTH = "peer_key"
PEER_KINDS = frozenset({PEER_KIND, PEER_USER_KIND})
# Principal kinds only the server itself can mint. An API key whose configured role spells one of them is an
# ordinary unprivileged key (``api.py:_principal``), never that principal.
UNMINTABLE_KINDS = frozenset({"external", "collab"}) | PEER_KINDS



def kind_for_role(role: str) -> str:
    """The principal kind an API key of configured ``role`` authenticates as: a role that spells a kind only the
    server can mint (external, collab, peer, peer-user) is an ordinary unprivileged ``unknown`` key."""
    return "unknown" if role in UNMINTABLE_KINDS else role


PEER_NODE_TYPE = "peer"
PEER_NODE_PREFIX = "peer:"             # the whole prefix is reserved; a peer's own part is ``peer:<peer>.``
PEER_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")      # no '.', ':' or '*': a peer cannot own another's namespace
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{12,64}\Z")
_PRINCIPAL_RE = re.compile(r"peer:([a-z][a-z0-9-]{0,31}):([0-9a-f]{12})\Z")
_ORIGIN_KEYS = ("server", "actor", "event_id", "sequence")
_MAX_CLAIM = 512

# kind -> the scope its inbound events are rewritten to. Exactly this scope, never the sender's: a peer that
# asks for ``org`` is narrowed, one that asks for ``private`` lands in the mirror room as the ADR requires.
CLAMP_SCOPES = {PEER_KIND: "room"}
# the sender-claimed local ids a clamped event may not carry into the local namespace
_LOCAL_ID_FIELDS = ("org_id", "project_id", "workspace_id", "mission_id", "task_id", "agent_id",
                    "correlation_id", "causation_id")


class FederationConfigError(ValueError):
    """``control_plane.federation`` is invalid (startup: refuse to run; hot-reread: federation is OFF)."""


def peer_node_namespace(peer_id: str) -> str:
    return f"{PEER_NODE_PREFIX}{peer_id}."


def is_peer_node(node_id: Any) -> bool:
    """Is this lineage node id inside the reserved ``peer:`` namespace? (Case-insensitive, whatever follows.)"""
    return isinstance(node_id, str) and node_id.casefold().startswith(PEER_NODE_PREFIX)


def in_peer_namespace(node_id: Any, peer_id: str) -> bool:
    return isinstance(node_id, str) and node_id.startswith(peer_node_namespace(peer_id))


def peer_principal(peer_id: str, fingerprint: str) -> dict:
    """The one way a peer actor is built (P2/P3 call it after verifying the pinned key)."""
    if not PEER_ID_RE.fullmatch(peer_id or "") or not _FINGERPRINT_RE.fullmatch(fingerprint or ""):
        raise ValueError("invalid peer id or key fingerprint")
    return {"kind": PEER_KIND, "id": f"peer:{peer_id}:{fingerprint[:12]}", "authenticated_by": PEER_AUTH}


def peer_of(actor: Any) -> tuple[str, str] | None:
    """``(peer id, fingerprint prefix)`` of a well-formed peer actor, else None."""
    if not isinstance(actor, dict) or actor.get("kind") != PEER_KIND or actor.get("authenticated_by") != PEER_AUTH:
        return None
    m = _PRINCIPAL_RE.fullmatch(actor.get("id") or "")
    return (m.group(1), m.group(2)) if m else None


def is_peer_actor(actor: Any) -> bool:
    return isinstance(actor, dict) and actor.get("kind") in PEER_KINDS


# --------------------------------------------------------------------------- the clamp
@dataclass(frozen=True)
class Clamp:
    """What an inbound event from a clamped principal is rewritten to. ``scope`` is exact (never the sender's)
    and ``room_id`` is the single local room the event is bound to."""
    kind: str
    scope: str
    room_id: str


def clamp_for_principal(principal: dict | None, *, bind_room_id: str | None) -> Clamp | None:
    """The clamp for ``principal`` (None for a kind with no clamp). A clamped kind with no room to bind to is an
    error, never a pass-through: PermissionError."""
    kind = (principal or {}).get("kind")
    scope = CLAMP_SCOPES.get(kind)
    if scope is None:
        return None
    if not isinstance(bind_room_id, str) or not bind_room_id:
        raise PermissionError("not permitted to modify this lineage object")
    return Clamp(kind, scope, bind_room_id)


def clamp_required(principal: dict | None) -> bool:
    return (principal or {}).get("kind") in CLAMP_SCOPES


def _claim(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"origin.{name} must be a string or integer")
    if isinstance(value, str) and (not value or len(value) > _MAX_CLAIM):
        raise ValueError(f"origin.{name} must be a short non-empty string")
    return value


def origin_claim(body: dict) -> dict:
    """The sender's provenance as ``claimed_*`` data (ADR Decision 4). Evidence of what the peer said, never an
    identity: nothing authoritative reads it."""
    origin = body.get("origin")
    if not isinstance(origin, dict) or set(origin) - set(_ORIGIN_KEYS):
        raise ValueError("a peer event must carry origin {server, event_id, actor?, sequence?}")
    out = {f"origin_{k}": _claim(origin.get(k), k) for k in _ORIGIN_KEYS}
    if not isinstance(out["origin_server"], str) or not isinstance(out["origin_event_id"], str):
        raise ValueError("origin.server and origin.event_id are required")
    if out["origin_sequence"] is not None and (isinstance(out["origin_sequence"], str) or out["origin_sequence"] < 0):
        raise ValueError("origin.sequence must be a non-negative integer")
    return out


def apply_clamp(env: dict, body: dict, clamp: Clamp) -> dict:
    """Rewrite ``env`` in place to ``clamp`` (before the content hash is taken, so a replay hashes identically).

    * ``visibility_scope`` becomes ``clamp.scope`` whatever was asked; the ask is kept as ``claimed_visibility_scope``;
    * ``room_id`` becomes ``clamp.room_id``; the sender's room and every other sender-chosen local id
      (org/project/workspace/mission/task/agent, correlation/causation) is dropped to ``claimed_ids`` and the envelope carries None;
    * ``evidence_refs`` moves to ``claimed_evidence_refs``;
    * the origin block is kept as ``claimed_origin``.
    """
    env["claimed_visibility_scope"] = body.get("visibility_scope") if isinstance(body.get("visibility_scope"), str) else None
    env["visibility_scope"] = clamp.scope
    env["claimed_ids"] = {k: env.get(k) for k in ("room_id", *_LOCAL_ID_FIELDS) if env.get(k) is not None}
    for k in _LOCAL_ID_FIELDS:
        env[k] = None
    # evidence references name the SENDER's artifacts; a mirrored event must not point at a local one
    env["claimed_evidence_refs"], env["evidence_refs"] = env.get("evidence_refs"), None
    env["room_id"] = clamp.room_id
    env["claimed_origin"] = origin_claim(body)
    env["clamp"] = clamp.kind
    return env


# --------------------------------------------------------------------------- YAML
@dataclass(frozen=True)
class PeerPolicy:
    peer_id: str
    enabled: bool
    key_fingerprint: str
    mirror_room: str

    def matches(self, fingerprint12: str) -> bool:
        return self.key_fingerprint[:12] == fingerprint12


@dataclass(frozen=True)
class FederationPolicy:
    enabled: bool = False
    peers: tuple = ()

    def peer(self, peer_id: str) -> PeerPolicy | None:
        return next((p for p in self.peers if p.peer_id == peer_id), None)


OFF = FederationPolicy()
_PEER_KEYS = frozenset({"enabled", "key_fingerprint", "mirror_room"})


def parse_federation(section: Any) -> FederationPolicy:
    """Validate ``control_plane.federation``. Raises :class:`FederationConfigError`; nothing is repaired.

    ``None`` (absent) is OFF. ``enabled`` must be a real boolean. Every peer id is a plain id; the one value that
    grants reach (``mirror_room``) is an explicit room id: ``"*"`` (or any glob) is refused outright,
    because a peer must never hold the operator opt-in that ``lineage_key_scopes`` allows an ordinary key.
    """
    from . import ids
    if section is None:
        return OFF
    if not isinstance(section, dict) or set(section) - {"enabled", "peers"}:
        raise FederationConfigError("control_plane.federation must hold only enabled and peers")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise FederationConfigError("control_plane.federation.enabled must be true or false")
    raw = section.get("peers", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FederationConfigError("control_plane.federation.peers must map a peer id to its settings")
    peers = []
    for peer_id, cfg in raw.items():
        where = f"control_plane.federation.peers.{peer_id}"
        if not isinstance(peer_id, str) or not PEER_ID_RE.fullmatch(peer_id):
            raise FederationConfigError(f"{where}: a peer id is lowercase letters, digits and '-' (no '*', '.', ':')")
        if not isinstance(cfg, dict) or set(cfg) - _PEER_KEYS:
            raise FederationConfigError(f"{where}: allowed keys are {sorted(_PEER_KEYS)}")
        on = cfg.get("enabled", False)
        if not isinstance(on, bool):
            raise FederationConfigError(f"{where}.enabled must be true or false")
        fp = cfg.get("key_fingerprint")
        if not isinstance(fp, str) or not _FINGERPRINT_RE.fullmatch(fp):
            raise FederationConfigError(f"{where}.key_fingerprint must be 12-64 lowercase hex characters")
        room = cfg.get("mirror_room")
        if not isinstance(room, str) or "*" in room or not ids.is_valid_id(room, "room"):
            raise FederationConfigError(f"{where}.mirror_room must be one explicit room id (a peer never holds \"*\")")
        peers.append(PeerPolicy(peer_id, on, fp, room))
    return FederationPolicy(enabled, tuple(peers))


def policy_from_source(source) -> FederationPolicy:
    """Hot read: ``source()`` returns the raw section. ANY failure (unreadable file, invalid section, a source
    that raises) is OFF, so a bad edit can only close federation, never open it."""
    if source is None:
        return OFF
    try:
        return parse_federation(source())
    except Exception:   # noqa: BLE001 - fail closed on anything
        return OFF
