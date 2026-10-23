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

import json
import re
import threading
from collections import deque
from datetime import datetime
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
MAX_SOURCES_PER_OBJECT = 16          # distinct (source, actor) writers per object
MAX_WILDCARD_REMOVES = 10_000
MAX_OBJECTS_PER_PRINCIPAL = 50_000   # fair-share quota of the global pool (operators exempt)
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


def validate_lineage(event_type: str, payload: Any) -> dict:
    """Validate and normalise a lineage payload (ValueError => HTTP 400).

    Pure: stores nothing.
    """
    if event_type not in LINEAGE_EVENT_TYPES:
        raise ValueError(f"unknown lineage event_type {event_type!r}")
    if not isinstance(payload, dict):
        raise ValueError("lineage payload must be a JSON object")
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
        return {"op": "node.upsert", "node_type": node_type, "node_id": nid,
                "label": _opt_text(payload.get("label"), "label", MAX_LABEL),
                "state": state, "attrs": _attrs(payload.get("attrs"))}
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
        return op
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
    """Server-assigned monotonic admission number (persisted in the envelope row)."""
    val = ev.get("lineage_seq")
    if val is None:
        meta = (ev.get("payload") or {}).get("_envelope")
        val = meta.get("lineage_seq") if isinstance(meta, dict) else None
    return val if isinstance(val, int) and not isinstance(val, bool) else 0


def _rank(ev: dict) -> tuple:
    """Total order over events: admission number, then receipt time, source, sequence, id.

    The admission number is assigned by the server in a monotonic counter, so it
    cannot be influenced by a client's sequence, source name or the wall clock.
    """
    return (_lseq(ev), _ts(ev), str(ev["source"]), int(ev["source_sequence"]), str(ev["event_id"]))


def _writer_key(ev: dict) -> tuple:
    return (str(ev["source"]), (ev.get("actor") or {}).get("id"))


def _narrow(a: str, b: str) -> str:
    return a if SCOPE_RANK.get(a, 0) <= SCOPE_RANK.get(b, 0) else b


class _Reg:
    """Last-writer-wins register per (source, actor), merged across writers by admission order.

    Inside one (source, actor) the emitter's own ``source_sequence`` decides, so
    a late-arriving older event loses. Between different writers only the
    server's admission order counts: a client-chosen sequence can never beat
    another principal.
    """
    __slots__ = ("by",)

    def __init__(self):
        self.by: dict[tuple, tuple] = {}     # writer key -> (seq, event_id, rank, value, key)

    def set(self, ev: dict, value: Any = None) -> None:
        key, seq, eid = _writer_key(ev), int(ev["source_sequence"]), str(ev["event_id"])
        cur = self.by.get(key)
        if cur is None or (seq, eid) > (cur[0], cur[1]):
            self.by[key] = (seq, eid, _rank(ev), value, key)

    def best(self) -> tuple | None:
        return max(self.by.values(), key=lambda t: t[2]) if self.by else None

    def value(self) -> Any:
        b = self.best()
        return b[3] if b else None


def _after(x: tuple, y: tuple) -> bool:
    """Is register entry ``x`` newer than ``y``? (same writer: sequence; else admission order)"""
    if x[4] == y[4]:
        return (x[0], x[1]) > (y[0], y[1])
    return x[2] > y[2]


class _Claims:
    """Scope, ownership and first/last event bookkeeping shared by nodes and edges."""

    def _init_claims(self) -> None:
        self.scope: str | None = None
        self.claim: tuple | None = None    # (rank, actor_id) of the first admitted event
        self.first = self.last = None      # (rank, event_id)
        self.sources: set[tuple] = set()
        self.attrs: dict[str, _Reg] = {}

    def _touch(self, ev: dict) -> None:
        rank = _rank(ev)
        scope = ev.get("visibility_scope")
        scope = scope if scope in SCOPE_RANK else "private"
        self.scope = scope if self.scope is None else _narrow(self.scope, scope)
        claim = (rank, (ev.get("actor") or {}).get("id"))
        if self.claim is None or claim[0] < self.claim[0]:
            self.claim = claim
        if self.first is None or rank < self.first[0]:
            self.first = (rank, ev["event_id"])
        if self.last is None or rank > self.last[0]:
            self.last = (rank, ev["event_id"])
        self.sources.add(_writer_key(ev))

    def owner(self) -> str | None:
        """Actor of the first admitted event (pinned: admission numbers only grow)."""
        return self.claim[1] if self.claim else None

    def co_writers(self) -> tuple:
        reg = self.attrs.get("co_writers")
        val = reg.value() if reg else None
        if isinstance(val, str):
            val = [val]
        return tuple(sorted(v for v in (val or []) if isinstance(v, str)))

    def writers(self) -> frozenset:
        out = set(self.co_writers())
        if self.owner():
            out.add(self.owner())
        return frozenset(out)

    def _put_attrs(self, attrs: dict, ev: dict) -> None:
        for k, v in attrs.items():
            self.attrs.setdefault(k, _Reg()).set(ev, v)

    def attr_values(self) -> dict:
        return {k: r.value() for k, r in sorted(self.attrs.items())}


class _Node(_Claims):
    def __init__(self, node_id: str, node_type: str):
        self.id, self.node_type = node_id, node_type
        self._init_claims()
        self.label, self.state = _Reg(), _Reg()   # state value = (state, reason)


class _Edge(_Claims):
    def __init__(self, key: tuple):
        self.key = key
        self.edge_type, self.from_id, self.to_id = key[:3]
        self.binding = key[3] if len(key) > 3 else None
        self._init_claims()
        self.adds, self.removes = _Reg(), _Reg()  # remove value = (reason, scope, actor_id)


class _Wild(_Claims):
    """Wildcard removal for a bound triple: resolved at read time only."""

    def __init__(self):
        self._init_claims()
        self.removes = _Reg()


class GraphProjection:
    """Thread-safe lineage graph. Mutated only through :meth:`apply`."""

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: dict[str, _Node] = {}
        self._edges: dict[tuple, _Edge] = {}
        self._wild: dict[tuple, _Wild] = {}
        self._out: dict[str, set] = {}
        self._in: dict[str, set] = {}
        self._owned: dict[str, int] = {}
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

        A non-operator is refused with ONE indistinguishable PermissionError when
        it is not a writer of the target, cannot see an endpoint, is not a writer
        of the edge's ``from`` node, or tries to remove something that does not
        exist: so refusal never reveals whether the object exists.
        """
        op = validate_lineage(event_type, payload)
        operator = _is_operator(actor)
        aid = (actor or {}).get("id")
        deny = PermissionError("not permitted to modify this lineage object")
        attrs = op.get("attrs") or {}
        with self._lock:
            if not operator and "co_writers" in attrs:
                raise deny
            if not operator and (not aid or self._owned.get(aid, 0) >= MAX_OBJECTS_PER_PRINCIPAL) \
                    and self._would_create(op):
                if not aid:
                    raise deny
                raise ValueError("per-principal object quota reached")
            if op["op"] in ("node.upsert", "node.state"):
                rec = self._nodes.get(op["node_id"])
                if rec is None:
                    if len(self._nodes) >= MAX_TOTAL_NODES:
                        raise ValueError("graph node capacity reached")
                elif not operator and rec.owner() is not None and aid not in rec.writers():
                    raise deny
                recs = [rec]
            else:
                frm, to = self._nodes.get(op["from_id"]), self._nodes.get(op["to_id"])
                if not operator:
                    # the author of an endpoint can always use it, whatever its scope
                    if any(n is None or n.scope is None or not (
                            aid in n.writers() or self._node_visible(actor, n)) for n in (frm, to)):
                        raise deny
                    if aid not in frm.writers():
                        raise deny
                key = (op["edge_type"], op["from_id"], op["to_id"]) + \
                    ((op["binding"],) if op["edge_type"] in BOUND_EDGE_TYPES and op["binding"] else ())
                wildcard = (op["op"] == "edge.remove" and op["edge_type"] in BOUND_EDGE_TYPES
                            and op["binding"] is None)
                if op["op"] == "edge.add":
                    rec = self._edges.get(key)
                    if rec is None and len(self._edges) >= MAX_TOTAL_EDGES:
                        raise ValueError("graph edge capacity reached")
                    recs = [rec]
                elif wildcard:
                    tri = key[:3]
                    existing = [self._edges[k] for k in self._out.get(tri[1], ()) if k[:3] == tri
                                and self._live(self._edges[k])]
                    if not existing:
                        raise (ValueError("edge not found") if operator else deny)
                    wild = self._wild.get(tri)
                    if wild is None and len(self._wild) >= MAX_WILDCARD_REMOVES:
                        raise ValueError("too many wildcard removes")
                    recs = [wild] + existing
                else:
                    rec = self._edges.get(key)
                    if rec is None or not self._live(rec):
                        raise (ValueError("edge not found") if operator else deny)
                    recs = [rec]
            pair = (source, aid)
            for rec in recs:
                if rec is None:
                    continue
                if (not operator and op["op"].startswith("edge") and rec.owner() is not None
                        and aid not in rec.writers()):
                    raise deny
                if source and pair not in rec.sources and len(rec.sources) >= MAX_SOURCES_PER_OBJECT:
                    raise ValueError("too many distinct writers for this lineage object")
            if len(recs) == 1 and recs[0] is not None and attrs:
                self._check_attr_caps(recs[0], attrs)
            elif attrs:
                self._check_attr_caps(None, attrs)
        return op

    def _would_create(self, op: dict) -> bool:
        if op["op"] in ("node.upsert", "node.state"):
            return op["node_id"] not in self._nodes
        if op["op"] == "edge.add":
            return True
        return False

    def _touch(self, rec: _Claims, ev: dict) -> None:
        """Record an event on ``rec``; its first event charges the creator's quota."""
        first = rec.claim is None
        rec._touch(ev)
        aid = (ev.get("actor") or {}).get("id")
        if first and aid and not _is_operator(ev.get("actor")):
            self._owned[aid] = self._owned.get(aid, 0) + 1

    @staticmethod
    def _check_attr_caps(rec: _Claims | None, attrs: dict) -> None:
        merged = {k: json.dumps(v, sort_keys=True, separators=(",", ":"))
                  for k, v in (rec.attr_values() if rec else {}).items()}
        merged.update({k: json.dumps(v, sort_keys=True, separators=(",", ":")) for k, v in attrs.items()})
        if len(merged) > MAX_TOTAL_ATTR_KEYS:
            raise ValueError(f"too many attrs keys (max {MAX_TOTAL_ATTR_KEYS})")
        if sum(len(k) + len(v) for k, v in merged.items()) > MAX_TOTAL_ATTRS_BYTES:
            raise ValueError("attrs for this object exceed the total size cap")

    # ----------------------------------------------------------- mutation
    def apply(self, ev: dict) -> None:
        """Apply one stored lineage event dict. Commutative and idempotent.

        Does not authorize or enforce capacity: only admitted events are stored.
        ``ValueError`` if the payload no longer validates.
        """
        op = validate_lineage(ev.get("event_type") or "", ev.get("payload") or {})
        with self._lock:
            self.events_applied += 1
            if op["op"] in ("node.upsert", "node.state"):
                self._apply_node(op, ev)
            else:
                self._apply_edge(op, ev)

    def _node(self, node_id: str, node_type: str) -> _Node:
        node = self._nodes.get(node_id)
        if node is None:
            node = self._nodes[node_id] = _Node(node_id, node_type)
        return node

    def _apply_node(self, op: dict, ev: dict) -> None:
        node = self._node(op["node_id"], op["node_type"])
        self._touch(node, ev)
        if op["op"] == "node.upsert":
            if op["label"] is not None:
                node.label.set(ev, op["label"])
            if op["state"] is not None:
                node.state.set(ev, (op["state"], None))
            node._put_attrs(op["attrs"], ev)
        else:
            node.state.set(ev, (op["state"], op["reason"]))

    def _apply_edge(self, op: dict, ev: dict) -> None:
        etype, frm, to = op["edge_type"], op["from_id"], op["to_id"]
        # Endpoints may arrive before their node.upsert. A placeholder is created
        # (type from the id prefix) but is NOT touched by the edge event: it has
        # no scope, so it stays invisible until a node event defines it.
        for nid, ntype in ((frm, op["from_type"]), (to, op["to_type"])):
            self._node(nid, ntype)
        bound = op["binding"]
        if op["op"] == "edge.add":
            edge = self._edge((etype, frm, to) + ((bound,) if etype in BOUND_EDGE_TYPES else ()))
            self._touch(edge, ev)
            edge.adds.set(ev)
            edge._put_attrs(op["attrs"], ev)
            return
        rem = (op["reason"], ev.get("visibility_scope") if ev.get("visibility_scope") in SCOPE_RANK
               else "private", (ev.get("actor") or {}).get("id"))
        if etype in BOUND_EDGE_TYPES and bound is None:
            tri = (etype, frm, to)
            wild = self._wild.setdefault(tri, _Wild())
            self._touch(wild, ev)
            wild.removes.set(ev, rem)
            return
        edge = self._edge((etype, frm, to) + ((bound,) if etype in BOUND_EDGE_TYPES else ()))
        self._touch(edge, ev)
        edge.removes.set(ev, rem)

    def _edge(self, key: tuple) -> _Edge:
        edge = self._edges.get(key)
        if edge is None:
            edge = self._edges[key] = _Edge(key)
            self._out.setdefault(key[1], set()).add(key)
            self._in.setdefault(key[2], set()).add(key)
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

    def _related(self, edge: _Edge, other: _Edge) -> bool:
        """May ``other`` make ``edge`` stale? Only the same reviewer (same ``from``
        node, which only its writers can add edges from) or an author who is a
        writer of ``edge`` itself. Unrelated reviewers are never ranked against
        each other; they only appear side by side in ``bindings``."""
        return other.from_id == edge.from_id or (other.owner() is not None
                                                  and other.owner() in edge.writers())

    def _is_stale(self, edge: _Edge, principal: Any = None) -> bool:
        """Is ``edge`` (reviewed/validated) outdated, as seen by ``principal``?

        Only edges the principal can see take part (``principal=None`` means the
        server-internal view over everything), so a hidden edge can never be
        inferred from another edge's flag. Staleness is by binding identity:
        for each RELATED (see ``_related``) other live edge O of the same type
        and subject with a different binding, ``edge`` is stale when
        * O declares ``attrs.supersedes`` containing edge's binding value; or
        * neither side declares a supersession of the other and O's add was
          admitted later than edge's (implicit order: server admission).
        An edge that itself declares it supersedes O is never made stale by O.
        The server cannot know commit order; emitters that can deliver late
        should state it with ``attrs.supersedes``.
        """
        if edge.edge_type not in BOUND_EDGE_TYPES or not self._live(edge) or edge.binding is None:
            return False
        mine = edge.binding[1]
        my_sup = self._supersedes(edge)
        my_add = edge.adds.best()
        for other in self._peers(edge, principal):
            if other.binding == edge.binding or not self._related(edge, other):
                continue
            theirs = other.binding[1]
            if mine in self._supersedes(other):
                return True
            if theirs in my_sup:
                continue
            if _after(other.adds.best(), my_add):
                return True
        return False

    def _peers(self, edge: _Edge, principal: Any) -> list:
        out = []
        for key in self._in.get(edge.to_id, ()):
            other = self._edges[key]
            if other is edge or other.edge_type != edge.edge_type or other.binding is None:
                continue
            if not self._live(other) or self._is_removed(other):
                continue
            if principal is not None and not self._edge_visible(principal, other):
                continue
            out.append(other)
        return out

    # ------------------------------------------------------------ renders
    def _node_view(self, node: _Node, internal: bool = False) -> dict:
        state = node.state.value()
        out = {
            "id": node.id, "node_type": node.node_type,
            "label": node.label.value(),
            "state": state[0] if state else None,
            "state_reason": state[1] if state else None,
            "attrs": node.attr_values(),
            "visibility_scope": node.scope or "private",
            "first_event_id": node.first[1] if node.first else None,
            "last_event_id": node.last[1] if node.last else None,
            "source": node.last[0][2] if node.last else None,
        }
        if internal:
            out["owner"], out["co_writers"] = node.owner(), list(node.co_writers())
        return out

    def _edge_view(self, edge: _Edge, principal: Any = None) -> dict:
        add = edge.adds.best()
        rem = self._effective_remove(edge)
        removed = self._is_removed(edge)
        out = {
            "edge_type": edge.edge_type, "from_id": edge.from_id, "to_id": edge.to_id,
            "attrs": edge.attr_values(),
            "stale": self._is_stale(edge, principal), "removed": removed,
            "visibility_scope": edge.scope or "private",
            "provenance": {"event_id": add[1], "source": add[2][2], "source_sequence": add[0]},
        }
        if edge.binding is not None:
            out["binding"] = {edge.binding[0]: edge.binding[1]}
            out["bindings"] = sorted({o.binding[1] for o in self._peers(edge, principal)} | {edge.binding[1]})
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
        return (node is not None and node.scope is not None
                and can_see(principal, node.scope, node.writers()))

    def _edge_visible(self, principal, edge: _Edge) -> bool:
        """An edge is visible only if its own scope AND both endpoints are."""
        return (self._live(edge) and can_see(principal, edge.scope or "private", edge.writers())
                and self._node_visible(principal, self._nodes.get(edge.from_id))
                and self._node_visible(principal, self._nodes.get(edge.to_id)))

    # -------------------------------------------------------------- reads
    def get_node(self, principal, node_id: str) -> dict:
        """Node detail plus its visible incident edges. KeyError if absent OR hidden."""
        with self._lock:
            node = self._nodes.get(node_id)
            if not self._node_visible(principal, node):
                raise KeyError(node_id)
            keys = sorted(self._out.get(node_id, set()) | self._in.get(node_id, set()), key=repr)
            budget = MAX_RESPONSE_BYTES
            view = self._node_view(node)
            budget -= len(json.dumps(view))
            edges, truncated = [], False
            for k in keys:
                if not self._edge_visible(principal, self._edges[k]):
                    continue
                ev = self._edge_view(self._edges[k], principal)
                budget -= len(json.dumps(ev))
                if len(edges) >= MAX_NODE_EDGES or budget < 0:
                    truncated = True
                    break
                edges.append(ev)
            return {"node": view, "edges": edges, "edges_truncated": truncated}

    def get_edge(self, principal, edge_type: str, from_id: str, to_id: str,
                 commit: str | None = None, plan_digest: str | None = None) -> dict:
        """Provenance of the visible edge(s) of a triple (optionally one binding)."""
        with self._lock:
            matches = [e for e in self._edges.values()
                       if e.key[:3] == (edge_type, from_id, to_id) and self._edge_visible(principal, e)
                       and (commit is None or e.binding == ("commit", commit))
                       and (plan_digest is None or e.binding == ("plan_digest", plan_digest))]
            if not matches:
                raise KeyError("edge")
            matches.sort(key=lambda e: e.adds.best()[2])
            return {"edges": [self._edge_view(e, principal) for e in matches[:MAX_NODE_EDGES]]}

    def neighborhood(self, principal, root: str, *, depth: int = DEFAULT_DEPTH,
                     max_nodes: int = DEFAULT_NODES, max_edges: int = DEFAULT_EDGES,
                     direction: str = "both", edge_types: set | None = None,
                     include_removed: bool = False) -> dict:
        """Breadth-first subgraph around ``root`` with the visibility check at every hop.

        A hidden or unknown root is KeyError (indistinguishable). A hidden node
        is never expanded, listed, counted, or revealed as an edge endpoint;
        ``truncated`` reflects only visible items so it leaks nothing. The
        response is also bounded by ``MAX_RESPONSE_BYTES``.
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
            queue = deque([root])
            truncated = False
            while queue:
                nid = queue.popleft()
                if seen[nid] >= depth:
                    continue
                keys: set = set()
                if direction in ("both", "out"):
                    keys |= self._out.get(nid, set())
                if direction in ("both", "in"):
                    keys |= self._in.get(nid, set())
                for key in sorted(keys, key=repr):
                    edge = self._edges[key]
                    if edge_types is not None and edge.edge_type not in edge_types:
                        continue
                    if not include_removed and self._is_removed(edge):
                        continue
                    if not self._edge_visible(principal, edge):
                        continue
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
            edges = []
            for nid in sorted(seen):
                for key in sorted(self._out.get(nid, set()), key=repr):
                    edge = self._edges[key]
                    if edge.to_id not in seen or not self._edge_visible(principal, edge):
                        continue
                    if edge_types is not None and edge.edge_type not in edge_types:
                        continue
                    if not include_removed and self._is_removed(edge):
                        continue
                    view = self._edge_view(edge, principal)
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
            nodes = {n.id: self._node_view(n, internal=True) for n in self._nodes.values()}
            edges = {}
            for k, e in self._edges.items():
                if self._live(e):
                    edges[repr(k)] = {**self._edge_view(e), "owner": e.owner(),
                                      "co_writers": list(e.co_writers())}
            wild = {repr(k): {"remove": [w.removes.best()[0], w.removes.best()[1], w.removes.best()[3]],
                              "scope": w.scope, "owner": w.owner()}
                    for k, w in self._wild.items()}
            return {"nodes": nodes, "edges": edges, "wildcard_removes": wild}
