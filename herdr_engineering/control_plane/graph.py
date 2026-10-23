"""Durable lineage graph projection (T02, docs/contracts/lineage-events.md).

A pure in-memory projection derived from ``lineage`` envelope events stored in
``fabric_events``. It owns no table: on restart the repo replays the stored
events through :meth:`GraphProjection.apply` and gets the same graph.

Merge rules (every merge is commutative, associative and idempotent, so replay,
re-delivery and out-of-order arrival converge on the same state):

* **Version** of an event = ``(source_sequence, source, event_id)``. Within one
  source this is the emitter's own order; across sources the tuple only gives a
  deterministic tie-break (sequences of different sources are not comparable).
* Node ``label`` and ``state`` and every ``attrs`` key are last-writer-wins by
  version, each field independently.
* ``node_type`` is fixed by the first writer's id prefix and must match the id.
* ``visibility_scope`` of a node/edge is the NARROWEST scope any contributing
  event carried (private < mission < room < org). It never widens.
* An edge is identified by ``(edge_type, from_id, to_id)``; for ``reviewed``
  and ``validated`` the revision binding (``commit`` / ``plan_digest``) is part
  of the identity, so a re-review at a new commit is a new edge and the old one
  can be flagged stale instead of being overwritten.
* An edge is *removed* when its newest ``edge.remove`` version is greater than
  its newest ``edge.add`` version (tombstone; history kept). A later add
  resurrects it.
* ``stale`` is derived at read time: a ``reviewed``/``validated`` edge is stale
  when another live edge of the same type for the same subject (``to_id``) with
  a different binding has a newer version.

Nothing here infers edges; only explicit ``edge.add`` events create them.
"""
from __future__ import annotations

import json
import re
import threading
from collections import deque
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
MAX_ATTRS_BYTES = 16 * 1024
MAX_ATTR_KEYS = 64
MAX_REASON = 512
MAX_ACTORS = 8
DEFAULT_DEPTH, MAX_DEPTH = 2, 5
DEFAULT_NODES, MAX_NODES = 200, 1000
DEFAULT_EDGES, MAX_EDGES = 500, 2000
MAX_NODE_EDGES = 200

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


def _attrs(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ATTR_KEYS:
        raise ValueError("attrs must be a JSON object with at most 64 keys")
    if not all(isinstance(k, str) and 0 < len(k) <= 128 for k in value):
        raise ValueError("attrs keys must be short strings")
    try:
        blob = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("attrs must be JSON") from None
    if len(blob) > MAX_ATTRS_BYTES:
        raise ValueError("attrs too large")
    return value


def _opt_text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be a string of at most {limit} characters")
    return value


def _binding(attrs: dict) -> tuple[str | None, str | None]:
    return attrs.get("commit"), attrs.get("plan_digest")


def validate_lineage(event_type: str, payload: Any) -> dict:
    """Validate and normalise a lineage payload (ValueError => HTTP 400).

    Returns the normalised operation. Pure: stores nothing.
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
    if event_type == "edge.add":
        attrs = _attrs(payload.get("attrs"))
        if edge_type in BOUND_EDGE_TYPES:
            commit, digest = _binding(attrs)
            for name, val in (("commit", commit), ("plan_digest", digest)):
                if val is not None and (not isinstance(val, str) or not val or len(val) > 128):
                    raise ValueError(f"attrs.{name} must be a non-empty string")
            if commit is None and digest is None:
                raise ValueError(f"{edge_type} edges require attrs.commit or attrs.plan_digest")
        op["attrs"] = attrs
        return op
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("edge.remove requires a reason")
    op["reason"] = _opt_text(reason, "reason", MAX_REASON)
    attrs = _attrs(payload.get("attrs"))
    op["attrs"] = attrs
    if edge_type in BOUND_EDGE_TYPES and attrs and _binding(attrs) != (None, None):
        commit, digest = _binding(attrs)
        for name, val in (("commit", commit), ("plan_digest", digest)):
            if val is not None and (not isinstance(val, str) or not val or len(val) > 128):
                raise ValueError(f"attrs.{name} must be a non-empty string")
    return op


# ------------------------------------------------------------- visibility
def can_see(principal: dict | None, scope: str, actors: frozenset | None) -> bool:
    """Fail-closed read check for one node or edge.

    There is no per-principal grant model yet (see the contract doc), so:

    * ``org``: any authenticated caller.
    * ``room`` / ``mission``: only an ``operator`` principal (the sole role
      with fleet-wide trust today); there is no room/mission membership.
    * ``private``: only the principal that authored every contributing event
      (``actor.id`` equality); nobody else, operators included.
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
        return bool(pid) and actors is not None and actors == frozenset({pid})
    return False


# ------------------------------------------------------------- projection
def _version(ev: dict) -> tuple:
    return (int(ev["source_sequence"]), str(ev["source"]), str(ev["event_id"]))


def _narrow(a: str, b: str) -> str:
    return a if SCOPE_RANK.get(a, 0) <= SCOPE_RANK.get(b, 0) else b


class _Touch:
    """Fields shared by nodes and edges: scope, actors, first/last event."""

    def _touch(self, ev: dict, version: tuple) -> None:
        scope = ev.get("visibility_scope")
        scope = scope if scope in SCOPE_RANK else "private"
        self.scope = scope if self.scope is None else _narrow(self.scope, scope)
        actor = (ev.get("actor") or {}).get("id")
        if actor is None or self.actors is None:
            self.actors = None            # unknown author: private stays hidden
        elif actor not in self.actors:
            self.actors = (None if len(self.actors) >= MAX_ACTORS
                           else self.actors | {actor})
        if self.first is None or version < self.first[0]:
            self.first = (version, ev["event_id"])
        if self.last is None or version > self.last[0]:
            self.last = (version, ev["event_id"])


class _Node(_Touch):
    def __init__(self, node_id: str, node_type: str):
        self.id, self.node_type = node_id, node_type
        self.scope: str | None = None
        self.actors: frozenset | None = frozenset()
        self.first = self.last = None
        self.label: tuple | None = None       # (version, value, source)
        self.state: tuple | None = None       # (version, value, reason, source)
        self.attrs: dict[str, tuple] = {}     # key -> (version, value)


class _Edge(_Touch):
    def __init__(self, key: tuple):
        self.key = key
        self.edge_type, self.from_id, self.to_id = key[:3]
        self.binding = key[3] if len(key) > 3 else None
        self.scope: str | None = None
        self.actors: frozenset | None = frozenset()
        self.first = self.last = None
        self.add: tuple | None = None         # (version, event_id, source, sequence)
        self.remove: tuple | None = None      # (version, event_id, source, sequence, reason)
        self.attrs: dict[str, tuple] = {}


class GraphProjection:
    """Thread-safe lineage graph. Mutated only through :meth:`apply`."""

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: dict[str, _Node] = {}
        self._edges: dict[tuple, _Edge] = {}
        # (edge_type, from_id, to_id) -> remove tuple for bound types removed
        # without naming a binding: applies to every binding of the triple.
        self._wild: dict[tuple, tuple] = {}
        self._out: dict[str, set] = {}
        self._in: dict[str, set] = {}
        self.events_applied = 0
        self.events_rejected = 0

    # ----------------------------------------------------------- mutation
    def apply(self, ev: dict) -> None:
        """Apply one stored lineage event dict. Idempotent; ValueError if invalid."""
        op = validate_lineage(ev.get("event_type") or "", ev.get("payload") or {})
        version = _version(ev)
        with self._lock:
            self.events_applied += 1
            if op["op"] in ("node.upsert", "node.state"):
                self._apply_node(op, ev, version)
            else:
                self._apply_edge(op, ev, version)

    def _node(self, node_id: str, node_type: str) -> _Node:
        node = self._nodes.get(node_id)
        if node is None:
            node = self._nodes[node_id] = _Node(node_id, node_type)
        return node

    def _apply_node(self, op: dict, ev: dict, version: tuple) -> None:
        node = self._node(op["node_id"], op["node_type"])
        node._touch(ev, version)
        src = ev["source"]
        if op["op"] == "node.upsert":
            if op["label"] is not None and (node.label is None or version > node.label[0]):
                node.label = (version, op["label"], src)
            if op["state"] is not None and (node.state is None or version > node.state[0]):
                node.state = (version, op["state"], None, src)
            for k, v in op["attrs"].items():
                cur = node.attrs.get(k)
                if cur is None or version > cur[0]:
                    node.attrs[k] = (version, v)
        elif node.state is None or version > node.state[0]:
            node.state = (version, op["state"], op["reason"], src)

    def _apply_edge(self, op: dict, ev: dict, version: tuple) -> None:
        etype, frm, to = op["edge_type"], op["from_id"], op["to_id"]
        # Endpoints may arrive before their node.upsert. A placeholder is created
        # (type from the id prefix) but is NOT touched by the edge event: it has
        # no scope, so it stays invisible until a node event defines it, and an
        # edge event can never move a node's scope, authors or event ids.
        for nid, ntype in ((frm, op["from_type"]), (to, op["to_type"])):
            self._node(nid, ntype)
        prov = (version, ev["event_id"], ev["source"], int(ev["source_sequence"]))
        if op["op"] == "edge.add":
            key = (etype, frm, to) + ((_binding(op["attrs"]),) if etype in BOUND_EDGE_TYPES else ())
            edge = self._edge(key)
            edge._touch(ev, version)
            if edge.add is None or version > edge.add[0]:
                edge.add = prov
            for k, v in op["attrs"].items():
                cur = edge.attrs.get(k)
                if cur is None or version > cur[0]:
                    edge.attrs[k] = (version, v)
            return
        removal = prov + (op["reason"],)
        if etype in BOUND_EDGE_TYPES:
            b = _binding(op["attrs"])
            if b == (None, None):
                tri = (etype, frm, to)
                if tri not in self._wild or version > self._wild[tri][0]:
                    self._wild[tri] = removal
                for key in self._out.get(frm, ()):
                    if key[:3] == tri:
                        self._edges[key]._touch(ev, version)
                return
            key = (etype, frm, to, b)
        else:
            key = (etype, frm, to)
        edge = self._edge(key)
        edge._touch(ev, version)
        if edge.remove is None or version > edge.remove[0]:
            edge.remove = removal

    def _edge(self, key: tuple) -> _Edge:
        edge = self._edges.get(key)
        if edge is None:
            edge = self._edges[key] = _Edge(key)
            self._out.setdefault(key[1], set()).add(key)
            self._in.setdefault(key[2], set()).add(key)
        return edge

    # --------------------------------------------------------- derivation
    def _effective_remove(self, edge: _Edge) -> tuple | None:
        cands = [r for r in (edge.remove, self._wild.get(edge.key[:3])) if r is not None]
        return max(cands, key=lambda r: r[0]) if cands else None

    def _is_removed(self, edge: _Edge) -> bool:
        rem = self._effective_remove(edge)
        return rem is not None and (edge.add is None or rem[0] > edge.add[0])

    def _is_stale(self, edge: _Edge) -> bool:
        if edge.edge_type not in BOUND_EDGE_TYPES or edge.add is None:
            return False
        for key in self._in.get(edge.to_id, ()):
            other = self._edges[key]
            if (other is edge or other.edge_type != edge.edge_type or other.add is None
                    or other.binding == edge.binding or self._is_removed(other)):
                continue
            if other.add[0] > edge.add[0]:
                return True
        return False

    def _edge_live(self, edge: _Edge) -> bool:
        """Exists (has an add event); hidden-ness is a separate question."""
        return edge.add is not None

    # ------------------------------------------------------------ renders
    def _node_view(self, node: _Node) -> dict:
        return {
            "id": node.id, "node_type": node.node_type,
            "label": node.label[1] if node.label else None,
            "state": node.state[1] if node.state else None,
            "state_reason": node.state[2] if node.state else None,
            "attrs": {k: v[1] for k, v in sorted(node.attrs.items())},
            "visibility_scope": node.scope or "private",
            "first_event_id": node.first[1] if node.first else None,
            "last_event_id": node.last[1] if node.last else None,
            "source": node.last[0][1] if node.last else None,
        }

    def _edge_view(self, edge: _Edge) -> dict:
        rem = self._effective_remove(edge)
        removed = self._is_removed(edge)
        out = {
            "edge_type": edge.edge_type, "from_id": edge.from_id, "to_id": edge.to_id,
            "attrs": {k: v[1] for k, v in sorted(edge.attrs.items())},
            "stale": self._is_stale(edge), "removed": removed,
            "visibility_scope": edge.scope or "private",
            "provenance": {"event_id": edge.add[1], "source": edge.add[2],
                           "source_sequence": edge.add[3]},
        }
        if removed and rem:
            out["removal"] = {"event_id": rem[1], "source": rem[2],
                              "source_sequence": rem[3], "reason": rem[4]}
        return out

    # ---------------------------------------------------------- visibility
    def _node_visible(self, principal, node: _Node | None) -> bool:
        return (node is not None and node.scope is not None
                and can_see(principal, node.scope, node.actors))

    def _edge_visible(self, principal, edge: _Edge) -> bool:
        """An edge is visible only if its own scope AND both endpoints are."""
        return (self._edge_live(edge) and can_see(principal, edge.scope or "private", edge.actors)
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
            edges = [self._edge_view(self._edges[k]) for k in keys
                     if self._edge_visible(principal, self._edges[k])]
            truncated = len(edges) > MAX_NODE_EDGES
            return {"node": self._node_view(node), "edges": edges[:MAX_NODE_EDGES],
                    "edges_truncated": truncated}

    def get_edge(self, principal, edge_type: str, from_id: str, to_id: str,
                 commit: str | None = None, plan_digest: str | None = None) -> dict:
        """Provenance of one visible edge (all bindings when none is given)."""
        with self._lock:
            matches = [e for e in self._edges.values()
                       if e.key[:3] == (edge_type, from_id, to_id) and self._edge_visible(principal, e)
                       and (commit is None or e.binding and e.binding[0] == commit)
                       and (plan_digest is None or e.binding and e.binding[1] == plan_digest)]
            if not matches:
                raise KeyError("edge")
            matches.sort(key=lambda e: e.add[0])
            return {"edges": [self._edge_view(e) for e in matches]}

    def neighborhood(self, principal, root: str, *, depth: int = DEFAULT_DEPTH,
                     max_nodes: int = DEFAULT_NODES, max_edges: int = DEFAULT_EDGES,
                     direction: str = "both", edge_types: set | None = None,
                     include_removed: bool = False) -> dict:
        """Breadth-first subgraph around ``root`` with the visibility check at every hop.

        A hidden or unknown root is KeyError (indistinguishable). A hidden node
        is never expanded, listed, counted, or revealed as an edge endpoint;
        ``truncated`` reflects only visible items so it leaks nothing.
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
                    if len(seen) >= max_nodes:
                        truncated = True
                        continue
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
                    if len(edges) >= max_edges:
                        truncated = True
                        continue
                    edges.append(self._edge_view(edge))
            nodes = [{**self._node_view(self._nodes[n]), "depth": seen[n]} for n in sorted(seen)]
            return {"root": root, "depth": depth, "nodes": nodes, "edges": edges,
                    "truncated": truncated,
                    "limits": {"max_depth": MAX_DEPTH, "max_nodes": max_nodes, "max_edges": max_edges}}

    # ------------------------------------------------------------ testing
    def snapshot(self) -> dict:
        """Unfiltered canonical state (tests and rebuild equality; never served)."""
        with self._lock:
            nodes = {n.id: {**self._node_view(n), "actors": sorted(n.actors) if n.actors is not None else None}
                     for n in self._nodes.values()}
            edges = {repr(k): {**self._edge_view(e), "actors": sorted(e.actors) if e.actors is not None else None}
                     for k, e in self._edges.items() if self._edge_live(e)}
            return {"nodes": nodes, "edges": edges}
