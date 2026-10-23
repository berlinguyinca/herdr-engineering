"""Durable lineage graph projection (T02, docs/contracts/lineage-events.md).

A pure in-memory projection derived from ``lineage`` envelope events stored in
``fabric_events``. It owns no table: on restart the repo replays the stored
events through :meth:`GraphProjection.apply` and gets the same graph.

Two layers, deliberately separate:

* :meth:`GraphProjection.admit` runs BEFORE an event is persisted (live ingest
  only). It rejects invalid payloads, writers that do not own the object, and
  anything that would exceed a capacity cap. A rejected event is never stored.
* :meth:`GraphProjection.apply` is a pure, COMMUTATIVE and idempotent merge. It
  trusts that every stored event was admitted, performs no authorization, and
  therefore yields the same graph for any replay order or grouping.

Merge rules:

* Every mutable field (node ``label``/``state``, each ``attrs`` key, an edge's
  add and remove) is a per-source last-writer-wins register: inside ONE source
  the higher ``source_sequence`` wins. Across sources the winners are compared
  by the SERVER-set receipt time (then source name, sequence, event id), so a
  hostile or buggy source cannot win forever by claiming a huge sequence.
* ``visibility_scope`` of a node/edge is the NARROWEST scope any event that
  defined it carried (private < mission < room < org); it never widens.
* Ownership: the owner of a node/edge is the actor of its earliest non-operator
  event (else the earliest operator event). Writers = owner + ``co_writers``
  (an attr only operators may set). ``private`` objects are visible to writers.
* An edge is identified by ``(edge_type, from_id, to_id)``; for ``reviewed``
  and ``validated`` the binding (``commit``, else ``plan_digest``) is part of
  the identity so a re-review at a new commit is a distinct edge.
* An edge is *removed* when its effective remove outranks its effective add.
  A ``reviewed``/``validated`` remove without a binding is a wildcard kept in a
  separate store and resolved at read time; it never mutates existing edges.
* ``stale`` is derived per caller over the edges THAT caller can see: see
  :meth:`GraphProjection._is_stale`.

Nothing here infers edges; only explicit ``edge.add`` events create them.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

NODE_TYPES = frozenset(
    "room workspace mission plan task agent attempt session tab pane handoff "
    "thread message decision approach worktree commit pr ci review finding "
    "artifact gate issue".split())
EDGE_TYPES = frozenset(
    "spawn handoff delegation consultation dependency issue_link fork "
    "supersession reviewed produced validated".split())
BOUND_EDGE_TYPES = frozenset({"reviewed", "validated"})
LINEAGE_EVENT_TYPES = ("node.upsert", "node.state", "edge.add", "edge.remove")
# COMMONS kinds are shared identities that several independent emitters legitimately
# mention (a commit, a PR, an issue, a CI run, a published artifact): anyone may create
# them and write their fields; nobody can hide one by writing it. Every other kind is
# OWNED by whoever created it. See docs/contracts/lineage-events.md.
COMMONS_KINDS = frozenset({"commit", "pr", "issue", "ci", "artifact"})

# Narrowest first.
SCOPE_RANK = {"private": 0, "mission": 1, "room": 2, "org": 3}

MAX_ID = 512
MAX_LABEL = 512
MAX_ATTRS_BYTES = 16 * 1024          # per event
MAX_ATTR_KEYS = 64                   # per event
MAX_TOTAL_ATTR_KEYS = 128            # per node/edge, across all events
MAX_TOTAL_ATTRS_BYTES = 32 * 1024    # per node/edge, across all events
MAX_REASON = 512
MAX_WRITERS = 8                      # co_writers and supersedes list length
MAX_SOURCES_PER_OBJECT = 64          # distinct (source, actor) writers per object
MAX_SOURCES_PER_ACTOR_OBJECT = 4
MAX_BINDINGS = 20                    # bindings listed per edge view
SCAN_BUDGET = 3000                   # incident edges examined per node per read
NEIGHBOR_SCAN_BUDGET = 500
MAX_CONTRIBUTIONS = 5                # foreign values listed per commons field
MAX_INSTANCES_PER_TRIPLE = 8         # per-writer instances of a commons-from edge
MAX_OBJECT_ATTR_KEYS = 1024          # hard cap per object across all writers
MAX_OBJECT_ATTRS_BYTES = 256 * 1024
NODE_RESERVE_FRACTION = 0.8          # share of the node cap non-operators may fill
MAX_WILDCARD_REMOVES = 10_000
MAX_OBJECTS_PER_PRINCIPAL = 25_000   # fair-share quota of the global pool (operators exempt)
MAX_TOTAL_NODES = 200_000
MAX_TOTAL_EDGES = 1_000_000
DEFAULT_DEPTH, MAX_DEPTH = 2, 5
DEFAULT_NODES, MAX_NODES = 200, 1000
DEFAULT_EDGES, MAX_EDGES = 500, 2000
MAX_NODE_EDGES = 200
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_STATE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_ID_RE = re.compile(r"([a-z]+):(\S.*)")


# ------------------------------------------------------------- validation
def _node_id(value: Any, name: str) -> tuple[str, str]:
    """Return ``(node_type, node_id)``; ValueError unless a known kind prefix."""
    if not isinstance(value, str) or not value or len(value) > MAX_ID:
        raise ValueError(f"{name} must be a node id of the form <kind>:<id>")
    m = _ID_RE.fullmatch(value)
    if not m or m.group(1) not in NODE_TYPES or any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError(f"{name} must be <kind>:<id> with a known kind")
    return m.group(1), value


def _str_list(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if (not isinstance(value, list) or len(value) > MAX_WRITERS
            or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in value)):
        raise ValueError(f"attrs.{name} must be a list of at most {MAX_WRITERS} short strings")
    return value


def _attrs(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ATTR_KEYS:
        raise ValueError(f"attrs must be a JSON object with at most {MAX_ATTR_KEYS} keys")
    if not all(isinstance(k, str) and 0 < len(k) <= 128 for k in value):
        raise ValueError("attrs keys must be short strings")
    try:
        blob = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("attrs must be JSON") from None
    if len(blob) > MAX_ATTRS_BYTES:
        raise ValueError("attrs too large")
    for name in ("co_writers", "supersedes"):
        if name in value:
            _str_list(value[name], name)
    return value


def _opt_text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be a string of at most {limit} characters")
    return value


def binding_of(attrs: dict) -> tuple[str, str] | None:
    """Revision binding: the commit when present, else the plan digest.

    A plan_digest that accompanies a commit is just an attribute; it does not
    make a different binding.
    """
    for kind in ("commit", "plan_digest"):
        val = attrs.get(kind)
        if val is not None:
            if not isinstance(val, str) or not val or len(val) > 128:
                raise ValueError(f"attrs.{kind} must be a non-empty string")
            return (kind, val)
    return None


def validate_lineage(event_type: str, payload: Any, *, strict: bool = True) -> dict:
    """Validate and normalise a lineage payload (ValueError => HTTP 400).

    Pure: stores nothing. ``strict`` (live ingest) rejects top-level ``_``-prefixed keys,
    which are reserved for server metadata; replay passes ``strict=False`` because the stored
    row carries the server's own ``_envelope``.
    """
    if event_type not in LINEAGE_EVENT_TYPES:
        raise ValueError(f"unknown lineage event_type {event_type!r}")
    if not isinstance(payload, dict):
        raise ValueError("lineage payload must be a JSON object")
    if strict and any(isinstance(k, str) and k.startswith("_") for k in payload):
        raise ValueError("payload keys starting with '_' are reserved")
    if event_type == "node.upsert":
        node_type = payload.get("node_type")
        if node_type not in NODE_TYPES:
            raise ValueError(f"unknown node_type {node_type!r}")
        kind, nid = _node_id(payload.get("node_id"), "node_id")
        if kind != node_type:
            raise ValueError("node_id prefix must equal node_type")
        state = payload.get("state")
        if state is not None and (not isinstance(state, str) or not _STATE_RE.fullmatch(state)):
            raise ValueError("invalid state")
        label = _opt_text(payload.get("label"), "label", MAX_LABEL)
        attrs = _attrs(payload.get("attrs"))
        return {"op": "node.upsert", "node_type": node_type, "node_id": nid, "label": label,
                "state": state, "attrs": attrs,
                "ensure": label is None and state is None and not attrs}
    if event_type == "node.state":
        kind, nid = _node_id(payload.get("node_id"), "node_id")
        state = payload.get("state")
        if not isinstance(state, str) or not _STATE_RE.fullmatch(state):
            raise ValueError("invalid state")
        return {"op": "node.state", "node_type": kind, "node_id": nid, "state": state,
                "reason": _opt_text(payload.get("reason"), "reason", MAX_REASON)}
    edge_type = payload.get("edge_type")
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge_type {edge_type!r}")
    from_kind, from_id = _node_id(payload.get("from_id"), "from_id")
    to_kind, to_id = _node_id(payload.get("to_id"), "to_id")
    if from_id == to_id:
        raise ValueError("an edge cannot connect a node to itself")
    if edge_type == "issue_link" and to_kind not in ("issue", "pr"):
        raise ValueError("issue_link must point at an issue or pr")
    op = {"op": event_type, "edge_type": edge_type, "from_id": from_id, "to_id": to_id,
          "from_type": from_kind, "to_type": to_kind}
    attrs = _attrs(payload.get("attrs"))
    op["attrs"] = attrs
    bound = binding_of(attrs) if edge_type in BOUND_EDGE_TYPES else None
    op["binding"] = bound
    if event_type == "edge.add":
        if edge_type in BOUND_EDGE_TYPES and bound is None:
            raise ValueError(f"{edge_type} edges require attrs.commit or attrs.plan_digest")
        op["ensure"] = not attrs or set(attrs) <= {"commit"} and edge_type in BOUND_EDGE_TYPES
        return op
    writer = attrs.get("writer")
    if writer is not None and (not isinstance(writer, str) or not 0 < len(writer) <= 128):
        raise ValueError("attrs.writer must be a short string")
    op["writer"] = writer
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("edge.remove requires a reason")
    op["reason"] = _opt_text(reason, "reason", MAX_REASON)
    return op


# ------------------------------------------------------------- visibility
def _is_operator(actor: dict | None) -> bool:
    return bool(actor) and actor.get("kind") == "operator" \
        and actor.get("authenticated_by") not in (None, "none")


def can_see(principal: dict | None, scope: str, writers: frozenset | None) -> bool:
    """Fail-closed read check for one node or edge.

    There is no per-principal grant model yet (see the contract doc), so:

    * ``org``: any authenticated caller.
    * ``room`` / ``mission``: only an ``operator`` principal (the sole role
      with fleet-wide trust today); there is no room/mission membership.
    * ``private``: only the object's writers (owner + operator-set
      ``co_writers``); nobody else, operators included.
    * anything unrecognised or an unauthenticated principal: hidden.
    """
    if not principal or principal.get("authenticated_by") in (None, "none") \
            or principal.get("kind") in (None, "unknown"):
        return False
    if scope == "org":
        return True
    if scope in ("room", "mission"):
        return principal.get("kind") == "operator"
    if scope == "private":
        pid = principal.get("id")
        return bool(pid) and bool(writers) and pid in writers
    return False


# ------------------------------------------------------------- projection
def _ts(ev: dict) -> float:
    raw = ev.get("ingest_timestamp") or ev.get("received_at")
    if isinstance(raw, datetime):
        return raw.timestamp()
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _lseq(ev: dict) -> int:
    """Server-assigned monotonic admission number. Read ONLY from the event dict's own
    ``lineage_seq`` key, which the server sets (live ingest, or the repo copying it out of
    the envelope metadata it persisted); never from the client-controlled payload."""
    val = ev.get("lineage_seq")
    return val if isinstance(val, int) and not isinstance(val, bool) else 0


def _rank(ev: dict) -> tuple:
    """Total order over events: admission number, then receipt time, source, sequence, id.

    The admission number is assigned by the server in a monotonic counter, so it
    cannot be influenced by a client's sequence, source name or the wall clock.
    """
    return (_lseq(ev), _ts(ev), str(ev["source"]), int(ev["source_sequence"]), str(ev["event_id"]))


def _mode(ev: dict) -> str:
    """Admission decision: normal | foreign | noop. Server-set top-level key only (see _lseq)."""
    val = ev.get("lineage_mode")
    return val if val in ("foreign", "noop") else "normal"


def _ekey(op: dict, writer_id: str | None) -> tuple:
    """Edge instance key ``(type, from, to, binding, writer)``. ``binding`` only for
    reviewed/validated; ``writer`` only when the ``from`` node is a shared COMMONS node
    (then every writer has its own instance of the triple and none can squat another's)."""
    bound = op["edge_type"] in BOUND_EDGE_TYPES
    return (op["edge_type"], op["from_id"], op["to_id"], op["binding"] if bound else None,
            writer_id if (op["from_type"] in COMMONS_KINDS and not bound) else None)


def _writer_key(ev: dict) -> tuple:
    return (str(ev["source"]), (ev.get("actor") or {}).get("id"))


def _narrow(a: str, b: str) -> str:
    return a if SCOPE_RANK.get(a, 0) <= SCOPE_RANK.get(b, 0) else b


class _Reg:
    """Last-writer-wins register per (source, actor), merged across writers by admission order.

    Inside one (source, actor) the emitter's own ``source_sequence`` decides, so
    a late-arriving older event loses. Between different writers only the
    server's admission order counts: a client-chosen sequence can never beat
    another principal, even on a shared source name.
    """
    __slots__ = ("by",)

    def __init__(self):
        self.by: dict[tuple, tuple] = {}     # writer key -> (seq, event_id, rank, value, key, is_op)

    def set(self, ev: dict, value: Any = None) -> None:
        key, seq, eid = _writer_key(ev), int(ev["source_sequence"]), str(ev["event_id"])
        cur = self.by.get(key)
        if cur is None or (seq, eid) > (cur[0], cur[1]):
            self.by[key] = (seq, eid, _rank(ev), value, key, _is_operator(ev.get("actor")))

    def best(self) -> tuple | None:
        return max(self.by.values(), key=lambda t: t[2]) if self.by else None

    def value(self) -> Any:
        b = self.best()
        return b[3] if b else None

    def primary(self, owner: str | None) -> tuple | None:
        """The authoritative entry for a shared (commons) field, chosen by who wrote it,
        never by who wrote last: an operator's value, else the creator's, else the earliest
        writer's. Everyone else's values are only ``contributions``."""
        ents = list(self.by.values())
        if not ents:
            return None
        ops = [e for e in ents if e[5]]
        if ops:
            return max(ops, key=lambda t: t[2])
        own = [e for e in ents if e[4][1] == owner]
        if own:
            return max(own, key=lambda t: t[2])
        return min(ents, key=lambda t: t[2])

    def contributions(self, primary: tuple | None, limit: int = MAX_CONTRIBUTIONS) -> list:
        """Latest value of every writer other than the primary's, newest admission first."""
        latest: dict = {}
        for e in self.by.values():
            if primary is not None and e[4][1] == primary[4][1]:
                continue
            cur = latest.get(e[4][1])
            if cur is None or e[2] > cur[2]:
                latest[e[4][1]] = e
        return sorted(latest.values(), key=lambda t: t[2], reverse=True)[:limit]


def _prov(entry: tuple | None, extra: dict | None = None) -> dict | None:
    """Provenance of one register entry: who wrote it, from which source, in which event, when."""
    if entry is None:
        return None
    out = {"value": entry[3], "writer": entry[4][1], "source": entry[4][0], "event_id": entry[1],
           "admission": entry[2][0],
           "admitted_at": datetime.fromtimestamp(entry[2][1], UTC).isoformat() if entry[2][1] else None}
    if extra:
        out.update(extra)
    return out


def _after(x: tuple, y: tuple) -> bool:
    """Is register entry ``x`` newer than ``y``? (same writer: sequence; else admission order)"""
    if x[4] == y[4]:
        return (x[0], x[1]) > (y[0], y[1])
    return x[2] > y[2]


class _Claims:
    """Scope, ownership and first/last event bookkeeping shared by nodes and edges."""

    def _init_claims(self) -> None:
        self.scope: str | None = None
        self.claim: tuple | None = None    # (rank, actor_id, is_operator) of the first admitted event
        self.first = self.last = None      # (rank, event_id)
        self.sources: set[tuple] = set()   # (source, actor) pairs that wrote a register
        self.attrs: dict[str, _Reg] = {}
        self._w: frozenset | None = None   # cached writers()

    def _touch(self, ev: dict) -> None:
        rank = _rank(ev)
        scope = ev.get("visibility_scope")
        scope = scope if scope in SCOPE_RANK else "private"
        self.scope = scope if self.scope is None else _narrow(self.scope, scope)
        actor = ev.get("actor") or {}
        claim = (rank, actor.get("id"), _is_operator(actor))
        if self.claim is None or claim[0] < self.claim[0]:
            self.claim = claim
            self._w = None
        if self.first is None or rank < self.first[0]:
            self.first = (rank, ev["event_id"])
        if self.last is None or rank > self.last[0]:
            self.last = (rank, ev["event_id"])

    def owner(self) -> str | None:
        """Actor of the FIRST admitted event, whatever its role (pinned: admission
        numbers only grow). An operator-created object is operator-owned until an
        operator sets ``co_writers``."""
        return self.claim[1] if self.claim else None

    def co_writers(self) -> tuple:
        reg = self.attrs.get("co_writers")
        val = reg.value() if reg else None
        if isinstance(val, str):
            val = [val]
        return tuple(sorted(v for v in (val or []) if isinstance(v, str)))

    def writers(self) -> frozenset:
        if self._w is None:
            out = set(self.co_writers())
            if self.owner():
                out.add(self.owner())
            self._w = frozenset(out)
        return self._w

    def _put_attrs(self, attrs: dict, ev: dict) -> None:
        for k, v in attrs.items():
            self.attrs.setdefault(k, _Reg()).set(ev, v)
        if "co_writers" in attrs:
            self._w = None

    def attr_values(self) -> dict:
        return {k: r.value() for k, r in sorted(self.attrs.items())}


class _Node(_Claims):
    def __init__(self, node_id: str, node_type: str):
        self.id, self.node_type = node_id, node_type
        self._init_claims()
        self.label, self.state = _Reg(), _Reg()   # state value = (state, reason)
        self.commons = node_type in COMMONS_KINDS
        self.scope_by: dict[str, tuple] = {}      # commons only: actor -> (narrowest scope, is_operator)

    def note_scope(self, ev: dict) -> None:
        """Commons nodes: each writer states its own scope (its narrowest)."""
        if not self.commons:
            return
        actor = ev.get("actor") or {}
        scope = ev.get("visibility_scope")
        scope = scope if scope in SCOPE_RANK else "private"
        key = actor.get("id")
        cur = self.scope_by.get(key)
        self.scope_by[key] = (scope if cur is None else _narrow(cur[0], scope), _is_operator(actor))

    def eff_scope(self) -> str | None:
        """Owned nodes: the narrowest scope any admitted event carried. COMMONS nodes: the
        WIDEST scope any non-operator writer asked for (pre-creating a shared identity as
        private cannot hide it from its real emitters); an operator who has written
        moderates and, if so, decides (narrowest of the operators' own scopes)."""
        if not self.commons:
            return self.scope
        if not self.scope_by:
            return None
        ops = [s for s, is_op in self.scope_by.values() if is_op]
        if ops:
            return min(ops, key=SCOPE_RANK.get)
        return max((s for s, _ in self.scope_by.values()), key=SCOPE_RANK.get)

    def pick(self, reg: _Reg) -> tuple | None:
        return reg.primary(self.owner()) if self.commons else reg.best()

    def attr_values(self) -> dict:
        return {k: (e[3] if (e := self.pick(r)) else None) for k, r in sorted(self.attrs.items())}


class _Edge(_Claims):
    def __init__(self, key: tuple):
        self.key = key
        self.edge_type, self.from_id, self.to_id, self.binding, self.writer = key
        self._init_claims()
        self.adds, self.removes = _Reg(), _Reg()  # remove value = (reason, scope, actor_id)


class _Wild(_Claims):
    """Wildcard removal for a bound triple: resolved at read time only."""

    def __init__(self):
        self._init_claims()
        self.removes = _Reg()


class _Group:
    """Visible live reviewed/validated edges into one subject, indexed once per read."""
    __slots__ = ("by_from", "by_owner", "sup_from", "sup_owner", "bindings", "truncated")


def from_stored(event: dict) -> dict:
    """Event dict for replay from a stored row.

    The admission number and decision are read from the envelope metadata the SERVER wrote
    (it overwrites any client ``_envelope`` when persisting) and become explicit event keys;
    ``apply`` never looks inside the payload for them.
    """
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    meta = payload.get("_envelope") if isinstance(payload, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    return {**event, "payload": payload, "lineage_seq": meta.get("lineage_seq"),
            "lineage_mode": meta.get("lineage_mode")}


class GraphProjection:
    """Thread-safe lineage graph. Mutated only through :meth:`apply`."""

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: dict[str, _Node] = {}
        self._edges: dict[tuple, _Edge] = {}
        self._wild: dict[tuple, _Wild] = {}
        # Insertion-ordered key sets; incident edges are walked newest first.
        self._out: dict[str, dict] = {}
        self._in: dict[str, dict] = {}
        self._by_triple: dict[tuple, dict] = {}     # (type, from, to) -> keys (all bindings)
        self._subject: dict[tuple, dict] = {}       # (to_id, type) -> keys, bound types only
        self._edges_by_owner: dict[str, set] = {}   # actor id -> edge keys it owns
        self._owned: dict[str, int] = {}            # actor id -> objects it owns (operators exempt)
        self._seq = 0
        self.events_applied = 0
        self.events_rejected = 0

    # ----------------------------------------------------------- admission
    def next_seq(self) -> int:
        """Next monotonic admission number (call under the ingest lock after admit)."""
        with self._lock:
            self._seq += 1
            return self._seq

    def admit(self, actor: dict | None, event_type: str, payload: Any, source: str | None = None) -> dict:
        """Pre-persist gate for a live event. Raises ValueError (invalid / over
        capacity / nothing to remove for an operator) or PermissionError.

        Returns the normalised op with ``op["mode"]``, the admission decision that
        is persisted with the event and honoured on replay:

        * ``normal``  - an ordinary write by a writer (or any creation).
        * ``foreign`` - a non-writer's field write to a COMMONS node: its field
          registers merge, but it never touches scope, ownership or event ids.
        * ``noop``    - an "ensure" (no label/state/attrs) of an object someone
          else owns: accepted and acknowledged, applied as nothing.

        A non-operator is refused with ONE indistinguishable PermissionError
        (HTTP 403 ``lineage_forbidden``) when it is not a writer of the target,
        cannot see an endpoint, is not a writer of the edge's ``from`` node,
        removes something that does not exist, or is at its object quota.
        """
        op = validate_lineage(event_type, payload, strict=True)
        operator = _is_operator(actor)
        aid = (actor or {}).get("id")
        deny = PermissionError("not permitted to modify this lineage object")
        attrs = op.get("attrs") or {}
        op["mode"] = "normal"
        with self._lock:
            if not operator and (not aid or "co_writers" in attrs):
                raise deny
            at_quota = not operator and self._owned.get(aid, 0) >= MAX_OBJECTS_PER_PRINCIPAL
            if op["op"] in ("node.upsert", "node.state"):
                rec = self._nodes.get(op["node_id"])
                exists = rec is not None and rec.claim is not None
                if not exists:
                    # A state write to a missing COMMONS node creates it, exactly like an upsert,
                    # so it is indistinguishable from the same write to a hidden existing one.
                    if op["op"] == "node.state" and op["node_type"] not in COMMONS_KINDS:
                        raise (ValueError("node not found") if operator else deny)
                    self._check_create(operator, at_quota, nodes=True)
                    recs = []
                elif operator or aid in rec.writers():
                    recs = [rec]
                else:
                    if at_quota:
                        raise deny
                    if op["op"] == "node.upsert" and op["ensure"]:
                        op["mode"] = "noop"
                        return op
                    if op["node_type"] not in COMMONS_KINDS:
                        raise deny
                    op["mode"] = "foreign"
                    recs = [rec]
            else:
                frm, to = self._nodes.get(op["from_id"]), self._nodes.get(op["to_id"])
                bound = op["edge_type"] in BOUND_EDGE_TYPES
                if not operator:
                    # the author of an endpoint can always use it, whatever its scope
                    if any(n is None or n.claim is None or not (
                            aid in n.writers() or self._node_visible(actor, n)) for n in (frm, to)):
                        raise deny
                    if not (aid in frm.writers() or (frm.node_type in COMMONS_KINDS and not bound)):
                        raise deny
                wildcard = (op["op"] == "edge.remove" and bound and op["binding"] is None)
                if op["op"] == "edge.add":
                    key = _ekey(op, aid)
                    rec = self._edges.get(key)
                    if rec is None or rec.claim is None:
                        self._check_create(operator, at_quota, nodes=False)
                        if key[4] is not None and len(self._by_triple.get(key[:3], ())) >= MAX_INSTANCES_PER_TRIPLE:
                            raise ValueError("too many writers for this edge")
                        recs = []
                    elif operator or aid in rec.writers():
                        recs = [rec]
                    elif at_quota or not op["ensure"]:
                        raise deny
                    else:
                        op["mode"] = "noop"
                        return op
                elif wildcard:
                    tri = (op["edge_type"], op["from_id"], op["to_id"], None)
                    existing = [e for e in (self._edges[k] for k in self._by_triple.get(tri[:3], ()))
                                if self._live(e)]
                    if not existing:
                        raise (ValueError("edge not found") if operator else deny)
                    wild = self._wild.get(tri[:3])
                    if wild is None and len(self._wild) >= MAX_WILDCARD_REMOVES:
                        raise ValueError("too many wildcard removes")
                    recs = ([wild] if wild is not None and wild.claim is not None else []) + existing
                else:
                    target = op["writer"] or aid
                    rec = self._edges.get(_ekey(op, target))
                    if rec is None or not self._live(rec):
                        raise (ValueError("edge not found") if operator else deny)
                    recs = [rec]
                    if (not operator and rec.writer is not None and aid not in rec.writers()
                            and (aid in frm.writers() or aid in to.writers())):
                        # an owner of either endpoint may tombstone another writer's instance
                        recs = []
                if not operator:
                    for rec in recs:
                        if aid not in rec.writers():
                            raise deny
            if attrs or op["op"] in ("node.state", "edge.remove") or op.get("label") or op.get("state"):
                pair = (source, aid)
                for rec in recs:
                    if pair not in rec.sources and (
                            len(rec.sources) >= MAX_SOURCES_PER_OBJECT
                            or sum(1 for p in rec.sources if p[1] == aid) >= MAX_SOURCES_PER_ACTOR_OBJECT):
                        raise ValueError("too many distinct writers for this lineage object")
                if len(recs) == 1 and attrs:
                    self._check_attr_caps(recs[0], attrs, aid)
                elif attrs:
                    self._check_attr_caps(None, attrs, aid)
        return op

    def _check_create(self, operator: bool, at_quota: bool, *, nodes: bool) -> None:
        if not operator and at_quota:
            raise PermissionError("not permitted to modify this lineage object")
        if nodes:
            cap = MAX_TOTAL_NODES if operator else int(MAX_TOTAL_NODES * NODE_RESERVE_FRACTION)
            if len(self._nodes) >= cap:
                raise ValueError("graph node capacity reached")
        elif len(self._edges) >= MAX_TOTAL_EDGES:
            raise ValueError("graph edge capacity reached")

    @staticmethod
    def _check_attr_caps(rec: _Claims | None, attrs: dict, writer: str | None) -> None:
        """Cap attrs per object. A COMMONS node is written by many keys, so each key is held to
        its OWN registers (one writer cannot fill the budget of another) under a hard object cap."""
        def dump(v):
            return json.dumps(v, sort_keys=True, separators=(",", ":"))

        def check(table, kcap, bcap, what):
            if len(table) > kcap:
                raise ValueError(f"too many attrs keys (max {kcap})")
            if sum(len(k) + len(v) for k, v in table.items()) > bcap:
                raise ValueError(f"attrs for this {what} exceed the total size cap")

        new = {k: dump(v) for k, v in attrs.items()}
        commons = isinstance(rec, _Node) and rec.commons
        if commons:
            mine = {}
            for k, reg in rec.attrs.items():
                ents = [e for e in reg.by.values() if e[4][1] == writer]
                if ents:
                    mine[k] = dump(max(ents, key=lambda t: t[2])[3])
            mine.update(new)
            check(mine, MAX_TOTAL_ATTR_KEYS, MAX_TOTAL_ATTRS_BYTES, "writer's share")
            whole = {k: dump(v) for k, v in rec.attr_values().items()}
            whole.update(new)
            check(whole, MAX_OBJECT_ATTR_KEYS, MAX_OBJECT_ATTRS_BYTES, "object")
        else:
            merged = {k: dump(v) for k, v in (rec.attr_values() if rec else {}).items()}
            merged.update(new)
            check(merged, MAX_TOTAL_ATTR_KEYS, MAX_TOTAL_ATTRS_BYTES, "object")

    # ----------------------------------------------------------- mutation
    def apply(self, ev: dict) -> None:
        """Apply one stored lineage event dict. Commutative and idempotent.

        Does not authorize or enforce capacity: only admitted events are stored,
        and the admission ``mode`` travels with the event. ``ValueError`` if the
        payload no longer validates.
        """
        op = validate_lineage(ev.get("event_type") or "", ev.get("payload") or {}, strict=False)
        mode = _mode(ev)
        with self._lock:
            self.events_applied += 1
            if mode == "noop":
                return
            if op["op"] in ("node.upsert", "node.state"):
                self._apply_node(op, ev, mode)
            else:
                self._apply_edge(op, ev)

    def _node(self, node_id: str, node_type: str) -> _Node:
        node = self._nodes.get(node_id)
        if node is None:
            node = self._nodes[node_id] = _Node(node_id, node_type)
        return node

    def _touch(self, rec: _Claims, ev: dict, key: tuple | None = None) -> None:
        """Record an event on ``rec``. Ownership is charged to the pinned owner, so the
        quota and the by-owner index depend only on the final state, never on replay order."""
        before = (rec.claim[1], rec.claim[2]) if rec.claim else None
        rec._touch(ev)
        after = (rec.claim[1], rec.claim[2])
        if before != after:
            if before is not None:
                self._release(before, key)
            if after[0] and not after[1]:
                self._owned[after[0]] = self._owned.get(after[0], 0) + 1
            if key is not None and after[0]:
                self._edges_by_owner.setdefault(after[0], set()).add(key)

    def _release(self, owner: tuple, key: tuple | None) -> None:
        aid, is_op = owner
        if aid and not is_op:
            self._owned[aid] = self._owned.get(aid, 1) - 1
            if self._owned[aid] <= 0:
                del self._owned[aid]
        if key is not None and aid and aid in self._edges_by_owner:
            self._edges_by_owner[aid].discard(key)

    def _apply_node(self, op: dict, ev: dict, mode: str) -> None:
        node = self._node(op["node_id"], op["node_type"])
        pair = _writer_key(ev)
        if mode == "normal":
            self._touch(node, ev)
        node.note_scope(ev)
        if op["op"] == "node.upsert":
            if op["label"] is not None:
                node.label.set(ev, op["label"])
            if op["state"] is not None:
                node.state.set(ev, (op["state"], None))
            node._put_attrs(op["attrs"], ev)
            if op["label"] is not None or op["state"] is not None or op["attrs"]:
                node.sources.add(pair)
        else:
            node.state.set(ev, (op["state"], op["reason"]))
            node.sources.add(pair)

    def _apply_edge(self, op: dict, ev: dict) -> None:
        etype, frm, to = op["edge_type"], op["from_id"], op["to_id"]
        # Endpoints may arrive before their node.upsert (replay order). A placeholder is
        # created (type from the id prefix) but is NOT touched by the edge event: it has
        # no scope, so it stays invisible until a node event defines it.
        for nid, ntype in ((frm, op["from_type"]), (to, op["to_type"])):
            self._node(nid, ntype)
        bound = op["binding"]
        pair = _writer_key(ev)
        actor_id = (ev.get("actor") or {}).get("id")
        if op["op"] == "edge.add":
            key = _ekey(op, actor_id)
            edge = self._edge(key)
            self._touch(edge, ev, key)
            edge.adds.set(ev)
            edge._put_attrs(op["attrs"], ev)
            edge.sources.add(pair)
            return
        rem = (op["reason"], ev.get("visibility_scope") if ev.get("visibility_scope") in SCOPE_RANK
               else "private", (ev.get("actor") or {}).get("id"))
        if etype in BOUND_EDGE_TYPES and bound is None:
            wild = self._wild.setdefault((etype, frm, to), _Wild())
            self._touch(wild, ev)
            wild.removes.set(ev, rem)
            wild.sources.add(pair)
            return
        key = _ekey(op, op["writer"] or actor_id)       # a tombstone names the instance it removes
        edge = self._edge(key)
        self._touch(edge, ev, key)
        edge.removes.set(ev, rem)
        edge.sources.add(pair)

    def _edge(self, key: tuple) -> _Edge:
        edge = self._edges.get(key)
        if edge is None:
            edge = self._edges[key] = _Edge(key)
            self._out.setdefault(key[1], {})[key] = None
            self._in.setdefault(key[2], {})[key] = None
            self._by_triple.setdefault(key[:3], {})[key] = None
            if key[0] in BOUND_EDGE_TYPES:
                self._subject.setdefault((key[2], key[0]), {})[key] = None
        return edge

    # --------------------------------------------------------- derivation
    def _effective_remove(self, edge: _Edge) -> tuple | None:
        """Best remove entry across the edge's own and any wildcard remove."""
        cands = [r for r in (edge.removes.best(),
                             self._wild[edge.key[:3]].removes.best() if edge.key[:3] in self._wild else None)
                 if r is not None]
        return max(cands, key=lambda t: t[2]) if cands else None

    def _is_removed(self, edge: _Edge) -> bool:
        rem = self._effective_remove(edge)
        add = edge.adds.best()
        return rem is not None and (add is None or _after(rem, add))

    def _live(self, edge: _Edge) -> bool:
        return edge.adds.best() is not None

    @staticmethod
    def _supersedes(edge: _Edge) -> set:
        reg = edge.attrs.get("supersedes")
        val = reg.value() if reg else None
        return set([val] if isinstance(val, str) else (val or []))

    def _group(self, principal: Any, to_id: str, etype: str, ctx: dict) -> _Group:
        """Index the live edges of one subject that ``principal`` can see (built once per read).

        Visibility is applied while indexing, so every aggregate - and therefore
        every ``stale`` flag and ``bindings`` list - is relative to the caller;
        a hidden edge cannot be inferred from them.
        """
        ck = (to_id, etype)
        grp = ctx.get(ck)
        if grp is not None:
            return grp
        grp = _Group()
        by_from: dict[str, list] = {}
        by_owner: dict[str, list] = {}
        grp.sup_from, grp.sup_owner = {}, {}
        seen: set[str] = set()
        for key in self._subject.get(ck, ()):
            e = self._edges[key]
            if e.binding is None or not self._live(e) or self._is_removed(e):
                continue
            if principal is not None and not self._edge_visible(principal, e):
                continue
            ent = (e.adds.best(), e, e.binding[1])
            by_from.setdefault(e.from_id, []).append(ent)
            if e.owner():
                by_owner.setdefault(e.owner(), []).append(ent)
            seen.add(e.binding[1])
            for v in self._supersedes(e):
                grp.sup_from.setdefault(e.from_id, {}).setdefault(v, set()).add(e.binding[1])
                if e.owner():
                    grp.sup_owner.setdefault(e.owner(), {}).setdefault(v, set()).add(e.binding[1])
        for lst in list(by_from.values()) + list(by_owner.values()):
            lst.sort(key=lambda t: t[0][2], reverse=True)
        grp.by_from, grp.by_owner = by_from, by_owner
        ordered = sorted(seen)
        grp.truncated = len(ordered) > MAX_BINDINGS
        grp.bindings = ordered[:MAX_BINDINGS]
        ctx[ck] = grp
        return grp

    def _is_stale(self, edge: _Edge, principal: Any, ctx: dict) -> bool:
        """Is ``edge`` (reviewed/validated) outdated, as seen by ``principal``?

        Only edges the principal can see take part. Staleness is by binding
        identity against RELATED edges only (same ``from_id``, or whose owner is a
        writer of ``edge``): another live edge O of the same type and subject with a
        different binding makes ``edge`` stale when
        * O declares ``attrs.supersedes`` containing edge's binding value; or
        * neither side declares a supersession of the other and O's add was
          admitted later than edge's (implicit order).
        An edge that itself declares it supersedes O is never made stale by O.
        """
        if edge.edge_type not in BOUND_EDGE_TYPES or not self._live(edge) or edge.binding is None:
            return False
        grp = self._group(principal, edge.to_id, edge.edge_type, ctx)
        mine = edge.binding[1]
        writers = edge.writers()
        for table, ids in ((grp.sup_from, (edge.from_id,)), (grp.sup_owner, tuple(writers))):
            for i in ids:
                if any(b != mine for b in table.get(i, {}).get(mine, ())):
                    return True
        my_sup = self._supersedes(edge)
        my_add = edge.adds.best()
        lists = [grp.by_from.get(edge.from_id, [])] + [grp.by_owner.get(w, []) for w in writers]
        for lst in lists:
            steps = 0
            for add, other, value in lst:
                if other is edge or value == mine or value in my_sup:
                    continue
                steps += 1
                if _after(add, my_add):
                    return True
                if add[2] < my_add[2] or steps >= 16:
                    break
        return False

    # ------------------------------------------------------------ renders
    def _node_view(self, node: _Node, internal: bool = False) -> dict:
        """Plain values are the PRIMARY ones (see ``_Reg.primary`` for commons); ``fields`` gives
        each one's provenance and ``contributions`` the other writers' values, so one writer's
        value is never shown under another writer's name."""
        state, label = node.pick(node.state), node.pick(node.label)
        fields: dict = {"label": _prov(label), "state": None, "attrs": {}}
        if state:
            fields["state"] = _prov(state, {"value": state[3][0], "reason": state[3][1]})
        contrib: dict = {}
        for name, reg, ent in (("label", node.label, label), ("state", node.state, state)):
            lst = reg.contributions(ent) if node.commons else []
            if lst:
                contrib[name] = [_prov(e, {"value": e[3][0], "reason": e[3][1]} if name == "state" else None)
                                 for e in lst]
        cattrs: dict = {}
        for k, reg in sorted(node.attrs.items()):
            ent = node.pick(reg)
            fields["attrs"][k] = _prov(ent)
            lst = reg.contributions(ent) if node.commons else []
            if lst:
                cattrs[k] = [_prov(e) for e in lst]
        if cattrs:
            contrib["attrs"] = cattrs
        out = {
            "id": node.id, "node_type": node.node_type,
            "label": label[3] if label else None,
            "state": state[3][0] if state else None,
            "state_reason": state[3][1] if state else None,
            "attrs": node.attr_values(),
            "fields": fields,
            "visibility_scope": node.eff_scope() or "private",
            "first_event_id": node.first[1] if node.first else None,
            "last_event_id": node.last[1] if node.last else None,
            "source": node.claim[0][2] if node.claim else None,
        }
        if contrib:
            out["contributions"] = contrib
        if internal:
            out["owner"], out["co_writers"] = node.owner(), list(node.co_writers())
        return out

    def _edge_view(self, edge: _Edge, principal: Any = None, ctx: dict | None = None) -> dict:
        ctx = {} if ctx is None else ctx
        add = edge.adds.best()
        rem = self._effective_remove(edge)
        removed = self._is_removed(edge)
        out = {
            "edge_type": edge.edge_type, "from_id": edge.from_id, "to_id": edge.to_id,
            "attrs": edge.attr_values(),
            "stale": self._is_stale(edge, principal, ctx), "removed": removed,
            "visibility_scope": edge.scope or "private",
            "provenance": {"event_id": add[1], "source": add[2][2], "source_sequence": add[0],
                           "writer": add[4][1]},
            "fields": {k: _prov(r.best()) for k, r in sorted(edge.attrs.items())},
        }
        if edge.writer is not None:
            out["writer"] = edge.writer
        if edge.binding is not None:
            grp = self._group(principal, edge.to_id, edge.edge_type, ctx)
            out["binding"] = {edge.binding[0]: edge.binding[1]}
            listed = list(grp.bindings)
            if edge.binding[1] not in listed:
                listed = sorted(listed + [edge.binding[1]])
            out["bindings"] = listed
            out["bindings_truncated"] = grp.truncated
        if removed and rem:
            reason, scope, actor_id = rem[3]
            # The remove event's own scope gates its details; a caller who cannot
            # see it learns only that the edge is removed.
            if principal is None or can_see(principal, scope, frozenset({actor_id} if actor_id else ())):
                out["removal"] = {"event_id": rem[1], "source": rem[2][2],
                                  "source_sequence": rem[0], "reason": reason}
        return out

    # ---------------------------------------------------------- visibility
    def _node_visible(self, principal, node: _Node | None) -> bool:
        scope = node.eff_scope() if node is not None else None
        return scope is not None and can_see(principal, scope, node.writers())

    def _edge_visible(self, principal, edge: _Edge) -> bool:
        """An edge is visible only if its own scope AND both endpoints are."""
        return (self._live(edge) and can_see(principal, edge.scope or "private", edge.writers())
                and self._node_visible(principal, self._nodes.get(edge.from_id))
                and self._node_visible(principal, self._nodes.get(edge.to_id)))

    # -------------------------------------------------------------- reads
    def _expand(self, principal, nid: str, direction: str, edge_types, include_removed: bool,
                from_id: str | None, budget: int) -> tuple[list, list, bool]:
        """Visible incident edges of ``nid``: (own, others, scan_exhausted).

        ``own`` are edges the caller is a writer of (never crowded out); ``others``
        are the rest, newest admission first. At most ``budget`` incident edges are
        examined, newest first, so a flood of old edges cannot hide recent ones and
        a read costs O(budget). Only visible edges are ever counted or returned.
        """
        aid = (principal or {}).get("id")
        outs = self._out.get(nid, {}) if direction in ("both", "out") else {}
        ins = self._in.get(nid, {}) if direction in ("both", "in") else {}

        def ok(edge: _Edge) -> bool:
            if edge_types is not None and edge.edge_type not in edge_types:
                return False
            if from_id is not None and edge.from_id != from_id:
                return False
            if not include_removed and self._is_removed(edge):
                return False
            return self._edge_visible(principal, edge)

        own, seen_own = [], set()
        mine = self._edges_by_owner.get(aid, set()) if aid else set()
        cand = mine if len(mine) <= len(outs) + len(ins) else itertools.chain(outs, ins)
        for key in cand:
            if key in mine and (key in outs or key in ins) and key not in seen_own:
                edge = self._edges[key]
                if self._live(edge) and ok(edge):
                    own.append(edge)
                    seen_own.add(key)
        others, scanned = [], 0
        exhausted = False
        for key in itertools.chain(reversed(outs), reversed(ins)):
            if scanned >= budget:
                exhausted = True
                break
            scanned += 1
            if key in seen_own:
                continue
            edge = self._edges[key]
            if self._live(edge) and ok(edge):
                others.append(edge)
        others.sort(key=lambda e: e.adds.best()[2], reverse=True)
        own.sort(key=lambda e: e.adds.best()[2], reverse=True)
        return own, others, exhausted

    def get_node(self, principal, node_id: str, *, edge_type: str | None = None,
                 from_id: str | None = None, limit: int = MAX_NODE_EDGES, offset: int = 0) -> dict:
        """Node detail plus a page of its visible incident edges. KeyError if absent OR hidden.

        Edges the caller writes come first and are exempt from the page cut; the
        rest are newest admission first. ``edges_omitted`` counts visible edges
        not returned (over visible items only); ``next_offset`` pages through them.
        """
        if edge_type is not None and edge_type not in EDGE_TYPES:
            raise ValueError("unknown edge_type filter")
        if not isinstance(limit, int) or not 1 <= limit <= MAX_NODE_EDGES:
            raise ValueError(f"limit must be between 1 and {MAX_NODE_EDGES}")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be >= 0")
        with self._lock:
            node = self._nodes.get(node_id)
            if not self._node_visible(principal, node):
                raise KeyError(node_id)
            own, others, exhausted = self._expand(
                principal, node_id, "both", {edge_type} if edge_type else None, False, from_id, SCAN_BUDGET)
            ctx: dict = {}
            view = self._node_view(node)
            budget = MAX_RESPONSE_BYTES - len(json.dumps(view))
            page_own = own[:MAX_NODE_EDGES] if offset == 0 else []
            page_oth = others[offset:offset + limit]
            edges, byte_cut = [], False
            for e in page_own + page_oth:
                ev = self._edge_view(e, principal, ctx)
                budget -= len(json.dumps(ev))
                if budget < 0:
                    byte_cut = True
                    break
                edges.append(ev)
            returned = len(edges)
            omitted = max(0, len(own) + len(others) - returned - (0 if offset == 0 else min(offset, len(others))))
            more = offset + limit < len(others)
            return {"node": view, "edges": edges,
                    "edges_truncated": bool(more or byte_cut or exhausted or len(own) > MAX_NODE_EDGES),
                    "edges_omitted": omitted, "scan_exhausted": exhausted,
                    "next_offset": offset + limit if more else None}

    def get_edge(self, principal, edge_type: str, from_id: str, to_id: str,
                 commit: str | None = None, plan_digest: str | None = None) -> dict:
        """Provenance of the visible edge(s) of a triple (optionally one binding)."""
        with self._lock:
            matches = [e for e in (self._edges[k] for k in self._by_triple.get((edge_type, from_id, to_id), ()))
                       if self._edge_visible(principal, e)
                       and (commit is None or e.binding == ("commit", commit))
                       and (plan_digest is None or e.binding == ("plan_digest", plan_digest))]
            if not matches:
                raise KeyError("edge")
            matches.sort(key=lambda e: e.adds.best()[2])
            ctx: dict = {}
            return {"edges": [self._edge_view(e, principal, ctx) for e in matches[:MAX_NODE_EDGES]]}

    def neighborhood(self, principal, root: str, *, depth: int = DEFAULT_DEPTH,
                     max_nodes: int = DEFAULT_NODES, max_edges: int = DEFAULT_EDGES,
                     direction: str = "both", edge_types: set | None = None,
                     include_removed: bool = False) -> dict:
        """Breadth-first subgraph around ``root`` with the visibility check at every hop.

        A hidden or unknown root is KeyError (indistinguishable). A hidden node
        is never expanded, listed, counted, or revealed as an edge endpoint;
        ``truncated`` reflects only visible items so it leaks nothing. At each node
        the caller's own edges are followed first, then the rest newest first, so a
        flood of edges cannot crowd out recent or own ones. The response is bounded
        by ``MAX_RESPONSE_BYTES`` and each node examines at most
        ``NEIGHBOR_SCAN_BUDGET`` incident edges.
        """
        if not isinstance(depth, int) or depth < 0 or depth > MAX_DEPTH:
            raise ValueError(f"depth must be between 0 and {MAX_DEPTH}")
        if not isinstance(max_nodes, int) or not 1 <= max_nodes <= MAX_NODES:
            raise ValueError(f"max_nodes must be between 1 and {MAX_NODES}")
        if not isinstance(max_edges, int) or not 0 <= max_edges <= MAX_EDGES:
            raise ValueError(f"max_edges must be between 0 and {MAX_EDGES}")
        if direction not in ("both", "out", "in"):
            raise ValueError("direction must be both, out or in")
        if edge_types is not None and not edge_types <= EDGE_TYPES:
            raise ValueError("unknown edge_type filter")
        with self._lock:
            start = self._nodes.get(root)
            if not self._node_visible(principal, start):
                raise KeyError(root)
            budget = MAX_RESPONSE_BYTES - len(json.dumps(self._node_view(start)))
            seen: dict[str, int] = {root: 0}
            expansions: dict[str, list] = {}
            queue = deque([root])
            truncated = False
            while queue:
                nid = queue.popleft()
                own, others, exhausted = self._expand(
                    principal, nid, direction, edge_types, include_removed, None, NEIGHBOR_SCAN_BUDGET)
                expansions[nid] = (own, others)
                truncated = truncated or exhausted
                if seen[nid] >= depth:
                    continue
                for edge in own + others:
                    other = edge.to_id if edge.from_id == nid else edge.from_id
                    if other in seen:
                        continue
                    cost = len(json.dumps(self._node_view(self._nodes[other])))
                    if len(seen) >= max_nodes or budget - cost < 0:
                        truncated = True
                        continue
                    budget -= cost
                    seen[other] = seen[nid] + 1
                    queue.append(other)
            ctx: dict = {}
            edges, done = [], set()
            # the caller's own edges first, then the rest newest first, so a flood cannot crowd either out
            ordered = [e for nid in sorted(expansions) for e in expansions[nid][0]] + sorted(
                (e for nid in sorted(expansions) for e in expansions[nid][1]),
                key=lambda e: e.adds.best()[2], reverse=True)
            if True:
                for edge in ordered:
                    if edge.key in done or edge.from_id not in seen or edge.to_id not in seen:
                        continue
                    done.add(edge.key)
                    view = self._edge_view(edge, principal, ctx)
                    cost = len(json.dumps(view))
                    if len(edges) >= max_edges or budget - cost < 0:
                        truncated = True
                        continue
                    budget -= cost
                    edges.append(view)
            nodes = [{**self._node_view(self._nodes[n]), "depth": seen[n]} for n in sorted(seen)]
            return {"root": root, "depth": depth, "nodes": nodes, "edges": edges,
                    "truncated": truncated,
                    "limits": {"max_depth": MAX_DEPTH, "max_nodes": max_nodes, "max_edges": max_edges,
                               "max_response_bytes": MAX_RESPONSE_BYTES}}

    # ------------------------------------------------------------ testing
    def snapshot(self) -> dict:
        """Unfiltered canonical state (tests and rebuild equality; never served)."""
        with self._lock:
            ctx: dict = {}
            nodes = {n.id: self._node_view(n, internal=True) for n in self._nodes.values()}
            edges = {}
            for k, e in self._edges.items():
                if self._live(e):
                    edges[repr(k)] = {**self._edge_view(e, None, ctx), "owner": e.owner(),
                                      "co_writers": list(e.co_writers())}
            wild = {repr(k): {"remove": [w.removes.best()[0], w.removes.best()[1], w.removes.best()[3]],
                              "scope": w.scope, "owner": w.owner()}
                    for k, w in self._wild.items() if w.removes.best()}
            return {"nodes": nodes, "edges": edges, "wildcard_removes": wild,
                    "owned": dict(sorted(self._owned.items()))}
