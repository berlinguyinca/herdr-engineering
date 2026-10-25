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
  defined it carried (private < mission < room < org); it never widens. A
  ``room``/``mission`` scope that names its room/mission (envelope ``room_id`` /
  ``mission_id``, plus ``workspace_id``) is a CONTEXTUAL label resolved against HCN
  membership at read time (T08, see :func:`can_see`); see the label notes above
  the visibility section for how two labels meet.
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

import hmac
import itertools
import json
import re
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

NODE_TYPES = frozenset(
    "room workspace mission plan task agent attempt session tab pane handoff "
    "thread message decision approach worktree commit pr ci review finding "
    "artifact gate issue "
    # system-intelligence (spec docs/specs/herdr-system-graph-20261020/02):
    # repository/host/service/gpu/model/request/span/queue are OWNED kinds; they
    # default to per-writer ownership exactly like the existing non-commons kinds.
    "repository branch service endpoint deployment host gpu model worker request "
    "span queue dataset memory_reference external_dependency module symbol package trace".split())
EDGE_TYPES = frozenset(
    "spawn handoff delegation consultation dependency issue_link fork "
    "supersession reviewed produced validated cross_link "
    # system-intelligence edge kinds; overlapping spec concepts map onto the
    # existing kinds above (depends_on->dependency, handoff_to->handoff,
    # reviews->reviewed, produced->produced, spawned_by->spawn, supersedes->supersession).
    "calls observed_in belongs_to blocked_by implements references runs_on "
    "serves deploys_to assigned_to precedes imports routes_to queued_on".split())
# T14: edge kinds only the HCN service may write (as a `collab` principal, after it has checked the asking person
# and the room). `cross_link` joins two nodes of different workspaces of one room; a `consultation` edge from a
# service-written `thread:hcn-consult.*` node records a peer consultation. Everybody else is refused with the one
# indistinguishable lineage_forbidden, operators excepted (trusted, as for every other write).
CROSS_LINK = "cross_link"
CONSULT_PREFIX = "thread:hcn-consult."
BOUND_EDGE_TYPES = frozenset({"reviewed", "validated"})
# How far a fork decision reaches through `dependency` edges when marking evidence stale (bounded, per read).
FORK_DEPENDENCY_DEPTH = 6
FORK_DEPENDENCY_NODES = 200
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
EDGE_SCAN_TOTAL = 20000              # incident edges a whole listing may examine
FIRST_TS_MAX = 1_000_000            # event ids remembered for since_join stripping
MAX_CONTRIBUTIONS = 5                # foreign values listed per commons field
MAX_INSTANCES_PER_TRIPLE = 8         # live per-writer instances of a commons-from edge
_GENERIC_LIMIT = "lineage object limit reached"
MAX_TRIPLE_RECORDS = 64              # instances incl. tombstoned ones (bounds the admit scan)
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
# T09 (lineage views and replay)
REPLAY_MAX_EVENTS = 25_000           # lineage events one replay may apply (ONE room's context events up to the position)
LOG_MAX_PER_CONTEXT = 100_000        # events kept per room/mission context; the oldest are evicted (history floor)
CHUNK = 128                          # events whose visibility is evaluated per live-lock acquisition
LEDGER_SCAN_BUDGET = 20_000          # events of ONE room context examined per read (newest window)
LEDGER_DEFAULT, LEDGER_MAX = 200, 500
TIMELINE_MAX_LANES, TIMELINE_MAX_MARKERS = 300, 1000
TERMINAL_STATES = frozenset("done failed cancelled superseded merged closed passed completed success error "
                            "rejected approved aborted".split())

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
# A scope LABEL is a plain scope string, or for a ``room``/``mission`` scope that names its
# context the tuple ``(scope, context_id, room_id, workspaces)``: a room label has ``room_id`` None and
# ``context_id`` the room; a MISSION label is bound to the room that linked it (``room_id`` required,
# otherwise the label is the bare operator-only ``mission``), so another room's owner linking the same
# mission id gains nothing. ``workspaces`` is
# the tuple (the audience's room must also hold links to them) where ``workspaces`` is a sorted tuple
# of the workspace ids the audience's room must also be linked to (empty = no workspace gate).
# Labels form a meet-semilattice (narrower = fewer readers):
#   private < bare mission < bare room < contextual room (same id: MORE workspaces = narrower) < org
#   bare mission < contextual mission (same id likewise) < org
# A contextual room is NOT above a contextual mission or bare room-vs-mission mix beyond the
# chain above, so the meet of two different contexts falls to the bare (operator-only) label of
# their kind: a producer that re-scopes one node into another room/mission hides it from both
# audiences, never leaks it into either. The meet is commutative, associative and idempotent.
# ``org`` is never readable through a member principal (see ``can_see``).
MAX_SCOPE_LABELS = 4         # distinct labels remembered per commons writer
MAX_LABEL_WORKSPACES = 8     # more distinct workspaces than this fall to the bare label (fail closed)


def scope_label(ev: dict):
    """The visibility label of one stored/live event. Derived ONLY from the event's own
    server-validated envelope fields, so live and rebuilt projections agree."""
    scope = ev.get("visibility_scope")
    if scope not in SCOPE_RANK:
        return "private"
    if scope in ("room", "mission"):
        def ident(name):
            v = ev.get(name)
            return v if isinstance(v, str) and v and len(v) <= MAX_ID else None
        rid = ident("room_id")
        cid = rid if scope == "room" else ident("mission_id")
        if cid is not None and rid is not None:
            ws = ident("workspace_id")
            return (scope, cid, None if scope == "room" else rid, (ws,) if ws else ())
    return scope


def _base(label) -> str:
    """The plain scope name of a label (what API responses show)."""
    return label if isinstance(label, str) else label[0]


def _lrank(label) -> int:
    return SCOPE_RANK.get(_base(label), 0)


def _ctx_visible(principal: dict, label: tuple) -> bool:
    """Membership check of a contextual label. Operators are trusted for every room/mission (as
    before); any other principal needs a server-built ``access`` snapshot of HCN membership."""
    if principal.get("kind") == "operator":
        return True
    access = principal.get("access")
    if principal.get("kind") != "member" or principal.get("authenticated_by") != "collab" \
            or not isinstance(access, dict):
        return False
    scope, cid, rid, wss = label
    if scope == "room":
        return cid in access["rooms"] and all((cid, w) in access["room_ws"] for w in wss)
    # a mission is readable through THE room that linked it, and only by the people that link lists
    return (rid, cid) in access["room_missions"] and all((rid, w) in access["room_ws"] for w in wss)


def _widen_labels(cur: tuple, label) -> tuple:
    """Join one label into a writer's remembered set (canonical, bounded, order-independent)."""
    s = set(cur) | {label}
    if "org" in s:
        return ("org",)
    if len(s) > 1:
        s.discard("private")
    return tuple(sorted(s, key=repr)[:MAX_SCOPE_LABELS])


def _is_room_service(actor: dict | None) -> bool:
    """The HCN service acting for ONE room. The kind cannot be minted by an API key (api.py maps such a role to
    `unknown`), and the id is the room-bound principal the service builds itself."""
    return bool(actor) and actor.get("kind") == "collab" and actor.get("authenticated_by") == "collab" \
        and isinstance(actor.get("id"), str) and actor["id"].startswith("collab:room:")


def _is_operator(actor: dict | None) -> bool:
    return bool(actor) and actor.get("kind") == "operator" \
        and actor.get("authenticated_by") not in (None, "none")


def can_see(principal: dict | None, scope, writers: frozenset | None) -> bool:
    """Fail-closed read check for one node or edge.

    There is no per-principal grant model yet (see the contract doc), so:

    * ``org``: any authenticated fleet key (never a ``member`` principal, see below).
    * bare ``room`` / ``mission`` (no context id on the event): only an
      ``operator`` principal. Fail closed.
    * contextual ``(room|mission, id, workspace?)``: an operator, or a ``member``
      principal (built by the HCN service for one request) whose current
      membership covers that room/mission and, when the event names a workspace,
      whose room holds a viewing link to it (see ``_ctx_visible``).
    * ``private``: only the object's writers (owner + operator-set
      ``co_writers``); nobody else, operators included.
    * anything unrecognised or an unauthenticated principal: hidden.
    """
    if not principal or principal.get("authenticated_by") in (None, "none") \
            or principal.get("kind") in (None, "unknown"):
        return False
    if scope == "org":
        # `org` is fleet data (any fleet KEY reads it); a signed-in PERSON does not, whatever rooms they are in
        return principal.get("kind") != "member"
    if isinstance(scope, tuple):
        return _ctx_visible(principal, scope)
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
    # a cross-workspace link has one instance PER ROOM (its writer is the room's service principal), so two rooms
    # that link the same two nodes never share, merge or squat on each other's edge
    return (op["edge_type"], op["from_id"], op["to_id"], op["binding"] if bound else None,
            writer_id if ((op["from_type"] in COMMONS_KINDS and not bound) or op["edge_type"] == CROSS_LINK) else None)


def _writer_key(ev: dict) -> tuple:
    return (str(ev["source"]), (ev.get("actor") or {}).get("id"))


def _narrow(a, b):
    """Meet of two scope labels (see the label notes above the visibility section)."""
    if a == b:
        return a
    if a == "org":
        return b
    if b == "org":
        return a
    if isinstance(a, str) and isinstance(b, str):
        return a if SCOPE_RANK.get(a, 0) <= SCOPE_RANK.get(b, 0) else b
    if isinstance(a, tuple) and isinstance(b, tuple):
        if a[:3] == b[:3]:                              # same room/mission: every workspace gate applies
            union = tuple(sorted(set(a[3]) | set(b[3])))
            return (*a[:3], union) if len(union) <= MAX_LABEL_WORKSPACES else a[0]
        return a[0] if a[0] == b[0] else "mission"      # different contexts: bare, operator-only
    ctx, bare = (a, b) if isinstance(a, tuple) else (b, a)
    if ctx[0] == "room" or bare in ("private", "mission"):
        return bare                                     # a bare label is below a contextual room, and
    return "mission"                                    # bare room vs contextual mission meets at bare mission


# Registers whose recent write history is kept (see _Reg.recent): only the PR head, so the
# bookkeeping costs nothing for every other attribute.
_TRACKED_ATTRS = frozenset({"head_sha"})
_RECENT_VALUES = 8


class _Reg:
    """Last-writer-wins register per (source, actor), merged across writers by admission order.

    Inside one (source, actor) the emitter's own ``source_sequence`` decides, so
    a late-arriving older event loses. Between different writers only the
    server's admission order counts: a client-chosen sequence can never beat
    another principal, even on a shared source name.
    """
    __slots__ = ("by", "firsts", "lasts", "recent", "track")

    def __init__(self, track: bool = False):
        self.track = track
        # tracked registers only: writer key -> {value: [first_rank, last_rank, last_seq,
        # last_event_id]}, one entry per DISTINCT value (so re-emits of one head cannot evict
        # the evidence about another), at most _RECENT_VALUES values, oldest evicted first.
        # min/max aggregates, so replay order cannot change it.
        self.recent: dict[tuple, dict] = {}
        # writer key -> (seq, event_id, rank, value, key, is_op, scope)
        self.by: dict[tuple, tuple] = {}
        self.firsts: dict[tuple, tuple] = {}   # writer key -> rank of that writer's EARLIEST event here
        self.lasts: dict[tuple, tuple] = {}    # writer key -> rank of that writer's LATEST event here (kept or not)

    def set(self, ev: dict, value: Any = None) -> None:
        key, seq, eid = _writer_key(ev), int(ev["source_sequence"]), str(ev["event_id"])
        rank = _rank(ev)
        first = self.firsts.get(key)
        if first is None or rank < first:
            self.firsts[key] = rank            # min over ALL events, whichever order they arrive in
        last = self.lasts.get(key)
        if last is None or rank > last:
            self.lasts[key] = rank             # max over ALL events: order independent
        if self.track and isinstance(value, str) and value.strip():
            hist = self.recent.setdefault(key, {})
            ent = hist.get(value)
            if ent is None:
                hist[value] = [rank, rank, seq, eid]
                if len(hist) > _RECENT_VALUES:
                    del hist[min(hist, key=lambda v: hist[v][1])]
            else:
                ent[0] = min(ent[0], rank)
                if rank > ent[1]:
                    ent[1], ent[2], ent[3] = rank, seq, eid
        cur = self.by.get(key)
        if cur is None or (seq, eid) > (cur[0], cur[1]):
            self.by[key] = (seq, eid, _rank(ev), value, key, _is_operator(ev.get("actor")),
                            scope_label(ev))

    def best(self) -> tuple | None:
        return max(self.by.values(), key=lambda t: t[2]) if self.by else None

    def value(self) -> Any:
        b = self.best()
        return b[3] if b else None

    def primary(self, owner: str | None, vis=None) -> tuple | None:
        """The authoritative entry for a shared (commons) field, chosen by who wrote it,
        never by who wrote last: an operator's value, else the owner's, else the writer who
        wrote this field FIRST. Everyone else's values are only ``contributions``. ``vis`` limits the
        candidates to the entries the reader may see (a hidden value is never primary)."""
        ents = [e for e in self.by.values() if vis is None or vis(e)]
        if not ents:
            return None
        ops = [e for e in ents if e[5]]
        if ops:
            return max(ops, key=lambda t: t[2])
        own = [e for e in ents if e[4][1] == owner]
        if own:
            return max(own, key=lambda t: t[2])
        # earliest FIRST write wins, so a later writer cannot displace an earlier one when the
        # earlier one updates its own value (its latest rank moves, its first write does not)
        return min(ents, key=lambda t: self.firsts.get(t[4], t[2]))

    def contributions(self, primary: tuple | None, limit: int = MAX_CONTRIBUTIONS, *,
                      owner: str | None = None, caller: str | None = None, vis=None, keep=()) -> list:
        """Latest visible value of every writer other than the primary's: one per distinct writer,
        operators first, then the owner, then the caller's own, then newest admission first, so
        a flood of principals cannot push a legitimate contribution out of the cap."""
        latest: dict = {}
        for e in self.by.values():
            if primary is not None and e[4][1] == primary[4][1]:
                continue
            if vis is not None and not vis(e):
                continue
            cur = latest.get(e[4][1])
            if cur is None or e[2] > cur[2]:
                latest[e[4][1]] = e

        def prio(t):
            return (t[5], t[4][1] == owner, caller is not None and t[4][1] == caller, t[2])
        # T16 M7: the configured trusted writers are selected BEFORE the cap (they are few, operator-listed, and
        # do not count against it), so other writers can never push their values out of a read.
        kept = [t for t in latest.values() if t[4][1] in keep]
        rest = [t for t in latest.values() if t[4][1] not in keep]
        return sorted(kept, key=prio, reverse=True) + sorted(rest, key=prio, reverse=True)[:limit]


def _entry_visible(principal: Any, entry: tuple) -> bool:
    """May ``principal`` see this register entry? Each value carries the scope its writer gave it
    and is visible only through THAT writer (a private value is its writer's alone)."""
    if principal is None:
        return True
    writer = entry[4][1]
    return can_see(principal, entry[6], frozenset({writer}) if writer else frozenset())


def _prov(entry: tuple | None, extra: dict | None = None, member: bool = False) -> dict | None:
    """Provenance of one register entry: who wrote it, from which source, in which event, when. The admission
    number is a global counter (it would count other rooms' and hidden events), so a room member never gets it."""
    if entry is None:
        return None
    out = {"value": entry[3], "writer": entry[4][1], "source": entry[4][0], "event_id": entry[1],
           "admitted_at": datetime.fromtimestamp(entry[2][1], UTC).isoformat() if entry[2][1] else None}
    if not member:
        out["admission"] = entry[2][0]
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
        scope = scope_label(ev)
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
            self.attrs.setdefault(k, _Reg(k in _TRACKED_ATTRS)).set(ev, v)
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
        self.scope_by: dict[str, tuple] = {}      # commons only: actor -> (labels it used, is_op)
        self.fclaim: tuple | None = None          # commons only: (rank, actor) of the first FIELD-bearing write
        self.fwriters: dict = {}                  # commons only: actor -> rank of its first field-bearing write

    def note_scope(self, ev: dict) -> None:
        """Commons nodes: remember the widest scope each writer used (their existence claim)."""
        if not self.commons:
            return
        actor = ev.get("actor") or {}
        key = actor.get("id")
        cur = self.scope_by.get(key)
        self.scope_by[key] = (_widen_labels(cur[0] if cur else (), scope_label(ev)), _is_operator(actor))

    def note_field_claim(self, ev: dict, owns: bool = True) -> None:
        """Commons ownership belongs to the first writer of a FIELD (label/state/attr); an empty
        'ensure' never creates it."""
        actor = ev.get("actor") or {}
        rank = _rank(ev)
        cur = self.fwriters.get(actor.get("id"))
        if cur is None or rank < cur:
            self.fwriters[actor.get("id")] = rank
        claim = (rank, actor.get("id"))
        if owns and (self.fclaim is None or claim[0] < self.fclaim[0]):
            self.fclaim = claim
            self._w = None

    def view_owner(self, actors: set | None = None) -> str | None:
        """The writer a reader treats as the owner: the earliest field-writer AMONG THE WRITERS THE
        READER CAN SEE (``actors``; None = all). It equals the real owner whenever that one is
        visible, and never depends on a writer the reader cannot see."""
        cands = [(r, a) for a, r in self.fwriters.items() if actors is None or a in actors]
        return min(cands, key=lambda t: t[0])[1] if cands else None

    def owner(self) -> str | None:
        if self.commons:
            return self.fclaim[1] if self.fclaim else None
        return super().owner()

    def eff_scope(self, principal: Any = None, internal: bool = True):
        """Owned nodes: the narrowest scope any admitted event carried. COMMONS nodes: the widest
        scope among the writers the READER can see (``internal``: all writers). A node is as
        visible as its most visible writer - pre-creating a shared identity as private cannot hide
        it from its real emitters - but the scope says nothing about what the other writers'
        fields are: each value is gated by its own writer (``_entry_visible``)."""
        if not self.commons:
            return self.scope
        scopes = [s for actor, (labels, _) in self.scope_by.items() for s in labels
                  if internal or can_see(principal, s, frozenset({actor}) if actor else frozenset())]
        return max(scopes, key=lambda s: (_lrank(s), repr(s))) if scopes else None

    def pick(self, reg: _Reg, vis=None, owner: Any = "") -> tuple | None:
        if not self.commons:
            return reg.best()
        return reg.primary(self.view_owner() if owner == "" else owner, vis)

    def attr_values(self, vis=None, owner: Any = "") -> dict:
        out = {}
        for k, r in sorted(self.attrs.items()):
            e = self.pick(r, vis, owner)
            if e is not None or vis is None:
                out[k] = e[3] if e else None
        return out


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

    def __init__(self, *, keep_log: bool = True, gate: GraphProjection | None = None):
        self._lock = threading.RLock()
        # T09: every applied (non-noop) lineage event with its validated op, in apply order, plus an index by
        # the room/mission context its scope names. The projection keeps only winners; replay and the ledger
        # need the events. Derived, never persisted: rebuild refills it exactly as it refills the graph.
        self._keep_log = keep_log
        self._first_ts: dict[str, tuple] = {}      # event_id -> (earliest admission time, room) for since_join stripping
        self._log_ctx: dict[tuple, list] = {}        # context -> (event, op) in apply order: PER ROOM, so one busy
        self._log_evicted: dict[tuple, int] = {}     # room never costs another room anything
        # a replayed projection reads through the CURRENT projection: nothing is visible in the past
        # that the reader cannot see now (no widening by replay)
        self._gate = gate
        self._replay_lock = threading.Lock()
        self._replay_cache: dict[tuple, GraphProjection] = {}
        self._cursor_key = secrets.token_bytes(32)
        self._nodes: dict[str, _Node] = {}
        self._edges: dict[tuple, _Edge] = {}
        self._wild: dict[tuple, _Wild] = {}
        # Insertion-ordered key sets; incident edges are walked newest first.
        self._out: dict[str, dict] = {}
        self._in: dict[str, dict] = {}
        self._by_type: dict[str, dict] = {}         # node_type -> ids in first-seen order
        # (scope, id, room_id) context -> node_type -> ids: every node an event ever addressed to that
        # context (a SUPERSET: later narrowing is applied at read time). Maintained in `_apply_node`, which
        # live ingest and rebuild both run, so a listing costs the size of one context, not of the graph.
        self._by_ctx: dict[tuple, dict] = {}
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
                elif operator or aid in rec.writers() or (
                        rec.commons and rec.owner() is None
                        and not (op["op"] == "node.upsert" and op["ensure"])):
                    # (an ownerless commons node - only ever "ensured" - is claimed by its first
                    # field-bearing write; an ensure never creates ownership)
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
                svc = _is_room_service(actor)
                if op["edge_type"] == CROSS_LINK and not (operator or svc):
                    raise deny
                if svc and (op["edge_type"] == CROSS_LINK or (
                        op["edge_type"] == "consultation" and op["from_id"].startswith(CONSULT_PREFIX))):
                    # the service already checked the person, the room and both endpoints; the graph only insists
                    # that the endpoints exist (an edge to a node nobody defined stays invisible anyway)
                    if any(n is None or n.claim is None for n in (frm, to)):
                        raise deny
                elif not operator:
                    # the author of an endpoint can always use it, whatever its scope
                    if any(n is None or n.claim is None or not (
                            aid in n.writers() or self._node_visible(actor, n)) for n in (frm, to)):
                        raise deny
                    if not (aid in frm.writers() or (
                            frm.node_type in COMMONS_KINDS and (not bound or frm.owner() is None))):
                        raise deny
                wildcard = (op["op"] == "edge.remove" and bound and op["binding"] is None)
                if op["op"] == "edge.add":
                    key = _ekey(op, aid)
                    rec = self._edges.get(key)
                    if rec is None or rec.claim is None:
                        self._check_create(operator, at_quota, nodes=False)
                        if key[4] is not None and not operator:
                            inst = self._by_triple.get(key[:3], ())
                            live = 0
                            for k in inst:
                                e = self._edges[k]
                                live += self._live(e) and not self._is_removed(e)
                                if live >= MAX_INSTANCES_PER_TRIPLE:
                                    break
                            # tombstoned instances do not count, but the scan (and the set) stays bounded
                            if live >= MAX_INSTANCES_PER_TRIPLE or len(inst) >= MAX_TRIPLE_RECORDS:
                                raise ValueError(_GENERIC_LIMIT)
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
                            and any(aid in n.writers() for n in (frm, to) if n.node_type not in COMMONS_KINDS)):
                        # the owner of an OWNED-kind endpoint may tombstone another writer's instance.
                        # A commons endpoint confers no such authority (anyone can be its "owner").
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
                        raise ValueError(_GENERIC_LIMIT if isinstance(rec, _Node) and rec.commons
                                         else "too many distinct writers for this lineage object")
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
            if what == "shared object":
                # depends on every writer's data, hidden ones included: say nothing specific
                if len(table) > kcap or sum(len(k) + len(v) for k, v in table.items()) > bcap:
                    raise ValueError(_GENERIC_LIMIT)
                return
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
            check(whole, MAX_OBJECT_ATTR_KEYS, MAX_OBJECT_ATTRS_BYTES, "shared object")
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
            if self._keep_log:
                eid = ev.get("event_id")
                if isinstance(eid, str):          # T16 M2: when an event id was first admitted, and in which room
                    t = _ts(ev)
                    room = ev.get("room_id") if isinstance(ev.get("room_id"), str) else None
                    known = self._first_ts.get(eid)
                    if known is None:
                        self._first_ts[eid] = (t, room)
                        if len(self._first_ts) > FIRST_TS_MAX:        # bounded; an unknown id is stripped (fail closed)
                            self._first_ts.pop(next(iter(self._first_ts)))
                    else:                                              # an id reused by another event: keep the strictest
                        self._first_ts[eid] = (min(t, known[0]), room if room == known[1] else None)
                label = scope_label(ev)
                if isinstance(label, tuple):
                    lst = self._log_ctx.setdefault(label[:3], [])
                    lst.append((ev, op))
                    if len(lst) > LOG_MAX_PER_CONTEXT:           # bounded: evict the oldest tenth, remember that we did
                        cut = LOG_MAX_PER_CONTEXT // 10
                        del lst[:cut]
                        self._log_evicted[label[:3]] = self._log_evicted.get(label[:3], 0) + cut
            if op["op"] in ("node.upsert", "node.state"):
                self._apply_node(op, ev, mode)
            else:
                self._apply_edge(op, ev)

    def _node(self, node_id: str, node_type: str) -> _Node:
        node = self._nodes.get(node_id)
        if node is None:
            node = self._nodes[node_id] = _Node(node_id, node_type)
            self._by_type.setdefault(node_type, {})[node_id] = None
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
        if node.commons and (op["op"] == "node.state" or op["label"] is not None
                             or op["state"] is not None or op["attrs"]):
            node.note_field_claim(ev, owns=mode == "normal")
        node.note_scope(ev)
        label = scope_label(ev)
        if isinstance(label, tuple):
            self._by_ctx.setdefault(label[:3], {}).setdefault(op["node_type"], {})[op["node_id"]] = None
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
        rem = (op["reason"], scope_label(ev), (ev.get("actor") or {}).get("id"))
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
        return self._behind_promoted_fork(edge, my_add, principal, ctx)

    def _behind_promoted_fork(self, edge: _Edge, my_add: tuple, principal: Any, ctx: dict) -> bool:
        """Does ``edge``'s evidence rest on work a fork decision replaced AFTER the evidence was admitted?

        A fork decision is a ``supersession`` edge from an ``approach:`` node (winner) to the node it replaced
        (T13). Evidence (``reviewed`` / ``validated``) about a subject that is, or transitively depends on
        (``dependency``: dependent -> prerequisite), a replaced node described work that no longer stands, so it is
        stale. Only that dependency closure is affected: a gate about an unrelated subject is untouched, and
        evidence admitted after the decision is current. Plain retries (a supersession whose winner is not an
        approach) keep their old behaviour. Derived per caller over the edges the caller can see; nothing is written.
        """
        ck = ("fork-decision", edge.to_id)
        if ck not in ctx:
            best, seen, todo = None, {edge.to_id}, [(edge.to_id, 0)]
            while todo:
                nid, depth = todo.pop()
                for key in self._in.get(nid, ()):
                    e = self._edges[key]
                    if (key[0] == "supersession" and key[1].startswith("approach:") and self._live(e)
                            and not self._is_removed(e) and self._edge_visible(principal, e)):
                        add = e.adds.best()
                        if best is None or add[2] > best[2]:
                            best = add
                if depth >= FORK_DEPENDENCY_DEPTH:
                    continue
                for key in self._out.get(nid, ()):
                    e = self._edges[key]
                    if (key[0] == "dependency" and key[2] not in seen and len(seen) < FORK_DEPENDENCY_NODES
                            and self._live(e) and not self._is_removed(e) and self._edge_visible(principal, e)):
                        seen.add(key[2])
                        todo.append((key[2], depth + 1))
            ctx[ck] = best
        best = ctx[ck]
        return best is not None and _after(best, my_add)

    # ------------------------------------------------------------ renders
    def _node_view(self, node: _Node, principal: Any = None, internal: bool = False) -> dict:
        """What ``principal`` may see of a node (``None`` = everything, for the unserved snapshot).

        Plain values are the PRIMARY ones (see ``_Reg.primary`` for commons); ``fields`` gives
        each one's provenance and ``contributions`` the other writers' values, so one writer's
        value is never shown under another writer's name. On a COMMONS node every value is
        gated by the scope ITS writer gave it: a reader sees the node shell (id, type, widest
        visible scope) plus only the fields of writers it may see, and ids/sources/event ids
        come from those visible values alone."""
        mk = None
        if node.commons and principal is not None:
            memo: dict = {}
            live = self._gate._nodes.get(node.id) if self._gate is not None else None

            def base(e, _p=principal, _m=memo):
                k = (e[6], e[4][1])             # visibility depends only on (scope, writer)
                r = _m.get(k)
                if r is None:
                    r = _m[k] = _entry_visible(_p, e)
                return r

            def mk(live_reg, _b=base, _p=principal):
                """Visibility of one register's entries. In a REPLAYED projection a past value is shown only if
                the same writer's CURRENT value of that field is visible too: a writer who later made a field
                private takes its past back out of sight."""
                if self._gate is None:
                    return _b
                return lambda e: _b(e) and live_reg is not None and e[4] in live_reg.by \
                    and _entry_visible(_p, live_reg.by[e[4]])

            def lreg(name, key=None):
                if live is None:
                    return None
                return live.attrs.get(key) if name == "attrs" else getattr(live, name)
        aid = (principal or {}).get("id")
        keep = (principal or {}).get("keep_writers") or ()
        owner = None
        if node.commons:
            actors = None
            if mk is not None:              # writers with at least one value this reader may see
                regs = [(node.label, lreg("label")), (node.state, lreg("state")),
                        *((r, lreg("attrs", k)) for k, r in node.attrs.items())]
                actors = {e[4][1] for reg, lr in regs for e in reg.by.values() if mk(lr)(e)}
            owner = node.view_owner(actors)
        vstate, vlabel = (mk(lreg("state")), mk(lreg("label"))) if mk else (None, None)
        state, label = node.pick(node.state, vstate, owner), node.pick(node.label, vlabel, owner)
        mem = bool(principal) and principal.get("kind") == "member"
        fields: dict = {"label": _prov(label, None, mem), "state": None, "attrs": {}}
        if state:
            fields["state"] = _prov(state, {"value": state[3][0], "reason": state[3][1]}, mem)
        contrib: dict = {}
        seen_entries = [e for e in (label, state) if e]
        for name, reg, ent, rv in (("label", node.label, label, vlabel), ("state", node.state, state, vstate)):
            lst = reg.contributions(ent, owner=owner, caller=aid, vis=rv, keep=keep) if node.commons else []
            seen_entries += lst
            if lst:
                contrib[name] = [_prov(e, {"value": e[3][0], "reason": e[3][1]} if name == "state" else None, mem)
                                 for e in lst]
        cattrs: dict = {}
        attrs: dict = {}
        for k, reg in sorted(node.attrs.items()):
            av = mk(lreg("attrs", k)) if mk else None
            ent = node.pick(reg, av, owner)
            lst = reg.contributions(ent, owner=owner, caller=aid, vis=av, keep=keep) if node.commons else []
            if ent is None and not lst:
                continue                      # every writer of this key is hidden from the reader
            attrs[k] = ent[3] if ent else None
            fields["attrs"][k] = _prov(ent, None, mem)
            if ent:
                seen_entries.append(ent)
            seen_entries += lst
            if lst:
                cattrs[k] = [_prov(e, None, mem) for e in lst]
        if cattrs:
            contrib["attrs"] = cattrs
        if node.commons:
            first = min(seen_entries, key=lambda t: t[2]) if seen_entries else None
            last = max(seen_entries, key=lambda t: t[2]) if seen_entries else None
            ids = (first[1] if first else None, last[1] if last else None, first[2][2] if first else None)
            scope = node.eff_scope(principal, internal=principal is None)
        else:
            ids = (node.first[1] if node.first else None, node.last[1] if node.last else None,
                   node.claim[0][2] if node.claim else None)
            scope = node.eff_scope()
        out = {
            "id": node.id, "node_type": node.node_type,
            "label": label[3] if label else None,
            "state": state[3][0] if state else None,
            "state_reason": state[3][1] if state else None,
            "attrs": attrs,
            "fields": fields,
            "visibility_scope": _base(scope or "private"),
            "first_event_id": ids[0],
            "last_event_id": ids[1],
            "source": ids[2],
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
            "visibility_scope": _base(edge.scope or "private"),
            "provenance": {"event_id": add[1], "source": add[2][2], "source_sequence": add[0],
                           "writer": add[4][1]},
            "fields": {k: _prov(r.best(), None, bool(principal) and principal.get("kind") == "member")
                       for k, r in sorted(edge.attrs.items())},
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
        if node is None:
            return False
        if self._gate is not None and not self._gate._node_visible(principal, self._gate._nodes.get(node.id)):
            return False
        if node.commons:
            # as visible as its most visible writer (see _Node.eff_scope); fields gate separately
            return node.eff_scope(principal, internal=False) is not None
        scope = node.eff_scope()
        return scope is not None and can_see(principal, scope, node.writers())

    def _edge_visible(self, principal, edge: _Edge) -> bool:
        """An edge is visible only if its own scope AND both endpoints are."""
        if self._gate is not None and not self._gate._edge_gate(principal, edge.key):
            return False
        return (self._live(edge) and can_see(principal, edge.scope or "private", edge.writers())
                and self._node_visible(principal, self._nodes.get(edge.from_id))
                and self._node_visible(principal, self._nodes.get(edge.to_id)))

    def _edge_gate(self, principal, key: tuple) -> bool:
        """Is the edge instance visible NOW, ignoring whether it is currently removed? The check a replayed
        projection must also pass: a tombstone is history, a narrowing is not."""
        edge = self._edges.get(key)
        return (edge is not None and can_see(principal, edge.scope or "private", edge.writers())
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
            view = self._node_view(node, principal)
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

    # ------------------------------------------------------------ T12: comment anchors and steering targets
    def anchor_visible(self, principal, anchor: dict) -> bool:
        """Cheap yes/no: may ``principal`` see this node or edge (hidden and missing are both False)?
        Used to gate comments, which would otherwise leak the existence and text around a hidden item."""
        with self._lock:
            if anchor.get("kind") == "node":
                return self._node_visible(principal, self._nodes.get(anchor.get("node_id")))
            keys = self._by_triple.get((anchor.get("edge_type"), anchor.get("from_id"), anchor.get("to_id")), ())
            return any(self._edge_visible(principal, self._edges[k]) for k in keys)

    def node_context(self, principal, node_id: str) -> dict | None:
        """What a context reference may show ``principal`` about one node: type, label, state and the workspaces its
        scope names. None when it is hidden or missing (the two are never told apart)."""
        with self._lock:
            node = self._nodes.get(node_id)
            if not self._node_visible(principal, node):
                return None
            view = self._node_view(node, principal)
            scope = node.eff_scope(principal, internal=False) if node.commons else node.scope
            wss = tuple(scope[3]) if isinstance(scope, tuple) else ()
            return {"node_id": node_id, "node_type": node.node_type, "label": view["label"], "state": view["state"],
                    "workspaces": wss, "scope": scope}

    def steer_facts(self, principal, node_id: str) -> dict:
        """What a steering request is fenced on, as ``principal`` sees it. KeyError if absent OR hidden.

        ``owner`` is the graph principal that created the node (OWNED kinds only; a commons node has no single
        owner); steering is delivered only to that principal. ``superseded`` is true when a live, visible
        ``supersession`` edge points AT the node (new -> old), i.e. a newer attempt replaced it."""
        with self._lock:
            node = self._nodes.get(node_id)
            if not self._node_visible(principal, node):
                raise KeyError(node_id)
            view = self._node_view(node, principal, internal=True)
            own, others, exhausted = self._expand(principal, node_id, "in", {"supersession"}, False, None,
                                                  SCAN_BUDGET)
            superseded = any(not v["removed"] and v["to_id"] == node_id
                             for v in (self._edge_view(x, principal, {}) for x in own + others))
            return {"node_id": node_id, "node_type": node.node_type, "commons": bool(node.commons),
                    "state": view["state"], "label": view["label"], "last_event_id": view["last_event_id"],
                    "owner": None if node.commons else view.get("owner"), "superseded": superseded,
                    "scan_exhausted": bool(exhausted),
                    "attrs": {k: view["attrs"].get(k) for k in
                              ("plan_digest", "digest", "commit", "head_sha", "attempt_id", "fencing_token", "instance_id")
                              if isinstance(view["attrs"].get(k), (str, int))}}

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

    @staticmethod
    def _owner_attr(edge: _Edge, owner: str, name: str) -> Any:
        """Newest value of attr ``name`` written by ``owner`` itself.

        An operator or a ``co_writer`` may add to the same edge key, and the plain
        :meth:`_Claims.attr_values` would report THEIR value under the owner's name; a verdict
        is the reviewer's own statement, so only the reviewer's own entries are read.
        """
        reg = edge.attrs.get(name)
        ents = [e for e in (reg.by.values() if reg else ()) if e[4][1] == owner]
        return max(ents, key=lambda t: t[2])[3] if ents else None

    def _owner_live(self, edge: _Edge, owner: str) -> bool:
        """Does ``owner`` currently stand behind ``edge``? Only the owner's own add/remove count.

        A remove by anyone else (operator, co_writer, wildcard) must not erase a reviewer's
        ``blocked``, nor must another writer's add keep an edge the owner never made alive.
        """
        adds = [e for e in edge.adds.by.values() if e[4][1] == owner]
        if not adds:
            return False
        add = max(adds, key=lambda t: t[2])
        removes = [r for r in edge.removes.by.values() if r[4][1] == owner]
        wild = self._wild.get(edge.key[:3])
        if wild is not None:
            removes += [r for r in wild.removes.by.values() if r[4][1] == owner]
        return not removes or not _after(max(removes, key=lambda t: t[2]), add)

    def review_evidence(self, principal, pr_id: str, reviewer_ids, head_writers) -> dict:
        """Raw, UNFILTERED inputs for the head-bound review quorum of one PR (T06).

        The quorum decision must not depend on who asks (a private ``blocked`` edge a
        viewer cannot see still blocks), so every candidate edge is included and carries a
        ``visible`` flag the caller uses only to redact identities. KeyError if the PR node
        is absent or hidden from ``principal``.

        Bounded by construction: reviews are found through the by-owner index of the
        CONFIGURED reviewer ids, never by scanning the PR's incident edges, so junk edges
        from other principals cost nothing and cannot cause a denial of service.
        ``head`` and ``author`` come only from the register entries of ``head_writers``
        (the PR is a commons node: its primary value may be anyone's).
        """
        with self._lock:
            node = self._nodes.get(pr_id)
            if node is None or node.node_type != "pr" or not self._node_visible(principal, node):
                raise KeyError(pr_id)

            def trusted(name):
                """(value, event_id): newest non-empty string from a trusted writer."""
                reg = node.attrs.get(name)
                ents = [e for e in (reg.by.values() if reg else ())
                        if e[4][1] in head_writers and isinstance(e[3], str) and e[3].strip()]
                if not ents:
                    return None, None
                best = max(ents, key=lambda t: t[2])
                return best[3], best[1]

            def trusted_head():
                """(value, event_id, state, candidate): the PR head from the trusted writers.

                A trusted writer's applied head is its highest-``source_sequence`` write. A
                write admitted AFTER it (so lower in sequence) with a different value is only
                harmless if that value is an OLD head the writer had already reported before
                (a late redelivery). Any other lower-sequence different head, however near,
                could equally be a restart that reset the sequence, so the head is
                ``unverified`` (alert, never satisfied). It clears when the writer writes
                past the applied sequence, or when an operator acknowledges the candidate
                write with ``head_resync`` (``resynced``). Nothing is adopted automatically.
                """
                reg = node.attrs.get("head_sha")
                if reg is None:
                    return None, None, "ok", None, None
                acks = {e[3] for e in (node.attrs["head_resync"].by.values()
                                       if "head_resync" in node.attrs else ()) if e[5]}
                cands, state, candidate = [], "ok", None
                for e in reg.by.values():
                    if e[4][1] not in head_writers or not isinstance(e[3], str) or not e[3].strip():
                        continue
                    value, eid, rank = e[3], e[1], e[2]
                    unknown = [(h[1], v, h[3]) for v, h in reg.recent.get(e[4], {}).items()
                               if v != e[3] and h[1] > e[2] and h[0] > e[2]]
                    if unknown:
                        last_rank, last_value, last_eid = max(unknown)
                        if last_eid in acks:
                            value, eid, rank = last_value, last_eid, last_rank
                            if state == "ok":
                                state = "resynced"
                        else:
                            state, candidate = "unverified", last_eid
                    cands.append((rank, value, eid, e[4]))
                if not cands:
                    return None, None, "ok", None, None
                best = max(cands, key=lambda c: c[:3])
                return best[1], best[2], state, candidate, best[3]
            head, head_event, head_state, candidate, head_writer = trusted_head()
            # T16 M9: the DECISION uses the real head whoever asks, but the head value (and the event ids that
            # name it) is shown only to a reader who may see the field: it is a hidden value otherwise.
            hreg = node.attrs.get("head_sha")
            head_visible = head is None or (
                hreg is not None and head_writer in hreg.by and _entry_visible(principal, hreg.by[head_writer]))
            author, _ = trusted("author")
            reviews, authors = [], set()
            for rid in sorted(reviewer_ids):
                for key in list(self._edges_by_owner.get(rid, ())):
                    e = self._edges.get(key)
                    if e is None or e.to_id != pr_id or e.owner() != rid or not self._owner_live(e, rid):
                        continue
                    if e.edge_type == "produced":
                        authors.add(rid)
                    elif e.edge_type == "reviewed" and e.binding and e.binding[0] == "commit":
                        reviews.append({
                            "reviewer": rid, "commit": e.binding[1], "from_id": e.from_id,
                            "verdict": self._owner_attr(e, rid, "verdict"),
                            "receipt_digest": self._owner_attr(e, rid, "receipt_digest"),
                            "receipt_binding": self._owner_attr(e, rid, "receipt_binding"),
                            "event_id": e.adds.best()[1],
                            "visible": self._edge_visible(principal, e)})
            return {"pr": pr_id, "head": head, "head_event_id": head_event,
                    "head_state": head_state, "head_resync_candidate": candidate, "author": author,
                    "head_visible": head_visible,
                    "reviews": reviews, "authors": sorted(authors)}

    def list_nodes(self, principal, node_type: str, *, edge_type: str | None = None,
                   limit: int = DEFAULT_NODES, contexts: frozenset | None = None) -> dict:
        """The nodes of one type the caller may see (first-seen order, at most ``limit``), plus the visible
        live ``edge_type`` edges between them. This is how a workbench finds its agents when the emitter
        defines no workspace/mission root node. Visibility is checked per node and per edge; a hidden node
        is never counted, so ``truncated`` (limit hit, or the ``SCAN_BUDGET`` of examined ids spent) says
        nothing about hidden items. ``contexts`` (server-built, never from a client) narrows the list to nodes
        whose visible scope names one of those ``(room|mission, id)`` pairs: one room's view of the graph."""
        if node_type not in NODE_TYPES:
            raise ValueError("unknown node_type")
        if edge_type is not None and edge_type not in EDGE_TYPES:
            raise ValueError("unknown edge_type")
        if not isinstance(limit, int) or not 1 <= limit <= MAX_NODES:
            raise ValueError(f"limit must be between 1 and {MAX_NODES}")
        with self._lock:
            views, reason = {}, None
            if contexts is None:
                candidates = self._by_type.get(node_type, ())      # keyed API: first-seen order
            else:                                                  # one room: only what was addressed to it
                # insertion (admission) order: no sort of the whole set, identical live and after rebuild
                seen = {}
                for c in sorted(contexts, key=repr):
                    seen.update(self._by_ctx.get(c, {}).get(node_type, {}))
                candidates = seen
            for scanned, nid in enumerate(candidates):
                if scanned >= SCAN_BUDGET:
                    reason = "scan_budget"
                    break
                node = self._nodes[nid]
                if not self._node_visible(principal, node):
                    continue
                if contexts is not None:
                    label = node.eff_scope(principal, internal=False) if node.commons else node.eff_scope()
                    if not (isinstance(label, tuple) and label[:3] in contexts):
                        continue
                if len(views) >= limit:
                    reason = "limit"
                    break
                views[nid] = self._node_view(node, principal)
            edges, ctx, done = [], {}, set()
            if edge_type is not None:
                left = EDGE_SCAN_TOTAL                              # one budget for the whole read
                for nid in views:
                    if left <= 0:
                        reason = reason or "edge_budget"
                        break
                    per = min(NEIGHBOR_SCAN_BUDGET, left)
                    left -= min(len(self._out.get(nid, ())), per)
                    own, others, exhausted = self._expand(principal, nid, "out", {edge_type}, False, None, per)
                    if exhausted:
                        reason = reason or "edge_budget"
                    for edge in own + others:
                        if edge.to_id in views and edge.key not in done:
                            if len(edges) >= MAX_EDGES:
                                reason = reason or "limit"
                                continue
                            done.add(edge.key)
                            edges.append(self._edge_view(edge, principal, ctx))
            # `truncated_reason` names the budget that ended the read; it counts only visible items or the size
            # of the one context asked about, never hidden nodes elsewhere in the graph.
            return {"node_type": node_type, "nodes": list(views.values()), "edges": edges,
                    "truncated": reason is not None, "truncated_reason": reason}

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
            views: dict = {root: self._node_view(start, principal)}      # one render per node per read
            budget = MAX_RESPONSE_BYTES - len(json.dumps(views[root]))
            seen: dict[str, int] = {root: 0}
            expansions: dict[str, list] = {}
            queue = deque([root])
            truncated = full = False
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
                    if len(seen) >= max_nodes or budget <= 0 or full:
                        truncated = True       # exhausted: do not even render the neighbour
                        continue
                    view = views.get(other)
                    if view is None:
                        view = views[other] = self._node_view(self._nodes[other], principal)
                    cost = len(json.dumps(view))
                    if budget - cost < 0:
                        truncated = full = True    # the byte budget is spent: stop rendering neighbours
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
            nodes = [{**views[n], "depth": seen[n]} for n in sorted(seen)]
            return {"root": root, "depth": depth, "nodes": nodes, "edges": edges,
                    "truncated": truncated,
                    "limits": {"max_depth": MAX_DEPTH, "max_nodes": max_nodes, "max_edges": max_edges,
                               "max_response_bytes": MAX_RESPONSE_BYTES}}


    # ------------------------------------------------------ T09: lineage views
    # Everything below reads the stored lineage events kept in ``_log`` (the projection itself only keeps
    # winners). A reader gets an event only when (1) the event's own scope label is visible to them and (2) the
    # object it concerns is visible to them NOW. Replay therefore can never show what the current graph would
    # hide: a later narrowing hides the past too, a tombstone does not.
    def _event_visible(self, principal, ev: dict, op: dict) -> bool:
        actor = (ev.get("actor") or {}).get("id")
        if not can_see(principal, scope_label(ev), frozenset({actor}) if actor else frozenset()):
            return False
        if op["op"] in ("node.upsert", "node.state"):
            node = self._nodes.get(op["node_id"])
            if not self._node_visible(principal, node):
                return False
            return not node.commons or self._commons_event_visible(principal, ev, op, node)
        # the exact edge instance this event touched (not any sibling binding or writer instance of the triple)
        if op["op"] == "edge.add":
            keys = [_ekey(op, actor)]
        elif op["edge_type"] in BOUND_EDGE_TYPES and op["binding"] is None:       # a wildcard remove names no instance
            keys = list(self._by_triple.get((op["edge_type"], op["from_id"], op["to_id"]), ()))
        else:
            keys = [_ekey(op, op.get("writer") or actor)]
        return any(self._edge_gate(principal, k) for k in keys)

    @staticmethod
    def _commons_event_visible(principal, ev: dict, op: dict, node: _Node) -> bool:
        """A commons field is visible through its writer's CURRENT value of that field: an event that wrote a
        value its writer has since made private is not shown (the past must not undo that)."""
        key = _writer_key(ev)
        regs = [node.state] if op["op"] == "node.state" else [
            *([node.label] if op.get("label") is not None else []),
            *([node.state] if op.get("state") is not None else []),
            *[node.attrs[k] for k in (op.get("attrs") or {}) if k in node.attrs]]
        return all(key in reg.by and _entry_visible(principal, reg.by[key]) for reg in regs)

    def _room_entries(self, principal, contexts, since: float | None = None) -> tuple[list, bool]:
        """The newest ``LEDGER_SCAN_BUDGET`` events the reader may see that are addressed to ``contexts``, in
        admission order: ``(entries, trimmed)``. The window is counted in the READER'S visible events (the log is
        scanned newest first until enough visible ones are found or it is exhausted), so its size never
        correlates with how many hidden events there are. ``since`` (a join time) drops earlier events one by
        one, never by position. ``trimmed`` also covers a context whose oldest events were evicted. The live
        lock is held for ``CHUNK`` events at a time, so a viewer never stalls ingest for a whole scan."""
        with self._lock:
            parts, trimmed = [], False
            for c in sorted(contexts, key=repr):
                parts.extend(self._log_ctx.get(c, ()))
                trimmed = trimmed or bool(self._log_evicted.get(c))
        parts.sort(key=lambda t: _rank(t[0]))
        got: list = []                                   # newest first
        i = len(parts)
        while i > 0 and len(got) <= LEDGER_SCAN_BUDGET:
            lo = max(0, i - CHUNK)
            with self._lock:
                got.extend((ev, op) for ev, op in reversed(parts[lo:i])
                           if (since is None or _ts(ev) >= since) and self._event_visible(principal, ev, op))
            i = lo
            time.sleep(0.0002)       # a lock is not fair: really hand it to a waiting ingest before re-taking it
        if len(got) > LEDGER_SCAN_BUDGET or (i > 0 and len(got) >= LEDGER_SCAN_BUDGET):
            trimmed = True
        return got[:LEDGER_SCAN_BUDGET][::-1], trimmed

    @staticmethod
    def _iso(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value) if value else None

    def _event_view(self, step: int, ev: dict, op: dict) -> dict:
        out = {"step": step, "event_id": str(ev["event_id"]), "event_type": op["op"], "source": str(ev["source"]),
               "received_at": self._iso(ev.get("ingest_timestamp") or ev.get("received_at")),
               "occurred_at": self._iso(ev.get("source_timestamp"))}
        if op["op"] in ("node.upsert", "node.state"):
            out.update(node_id=op["node_id"], node_type=op["node_type"], state=op.get("state"),
                       label=op.get("label"), reason=op.get("reason"))
        else:
            out.update(edge_type=op["edge_type"], from_id=op["from_id"], to_id=op["to_id"],
                       binding=dict([op["binding"]]) if op.get("binding") else None, reason=op.get("reason"))
        return out

    @staticmethod
    def _parse_at(value: str) -> float:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()      # no zone means UTC, never server-local
        except (AttributeError, ValueError):
            raise ValueError("at must be an ISO-8601 timestamp") from None

    @staticmethod
    def _position(entries: list, step: int | None, at: str | None) -> int:
        """Resolve a replay position to ``k``, the number of the reader's visible events included (0..n; every
        value is replayable). ``at`` counts the visible events BEFORE the first one received after that instant
        (server receipt time, never the emitter's claim), so it is a prefix and never skips an earlier event."""
        n = len(entries)
        if step is not None and at is not None:
            raise ValueError("give step or at, not both")
        if step is not None:
            if not isinstance(step, int) or isinstance(step, bool) or step < 0:
                raise ValueError("step must be a non-negative integer")
            return min(step, n)
        if at is not None:
            t = GraphProjection._parse_at(at)
            k = 0
            for ev, _ in entries:
                if _ts(ev) > t:
                    break
                k += 1
            return k
        return n

    def _replay(self, principal, contexts, entries: list, k: int) -> GraphProjection:
        """The graph after the first ``k`` of the reader's visible events, built from those events alone, read
        through THIS projection's visibility (``apply`` is commutative and idempotent). The cap counts the
        reader's own visible events, so it says nothing about hidden volume. Cached (two projections)."""
        if k >= len(entries):
            return self
        if k > REPLAY_MAX_EVENTS:
            raise ValueError("replay is not available for this position")
        key = (principal.get("id"), frozenset(contexts), k, _lseq(entries[0][0]) if entries else 0,
               _lseq(entries[k - 1][0]) if k else 0, self.events_applied)
        with self._replay_lock:
            hit = self._replay_cache.get(key)
            if hit:
                return hit
            past = GraphProjection(keep_log=False, gate=self)
            for ev, _op in entries[:k]:
                try:
                    past.apply(ev)
                except ValueError:
                    past.events_rejected += 1
            self._replay_cache[key] = past
            while len(self._replay_cache) > 2:
                self._replay_cache.pop(next(iter(self._replay_cache)))
            return past

    def _as_of(self, entries: list, k: int, trimmed: bool) -> dict:
        last = entries[k - 1][0] if k else None
        return {"step": k, "visible_events": len(entries), "head": k >= len(entries),
                "event_id": str(last["event_id"]) if last else None,
                "received_at": self._iso(last.get("ingest_timestamp") or last.get("received_at")) if last else None,
                "window_trimmed": trimmed}

    def _cursor_pad(self, principal, contexts) -> bytes:
        return hmac.new(self._cursor_key, repr((principal.get("id"), sorted(contexts, key=repr))).encode(),
                        "sha256").digest()

    def _cursor_make(self, principal, contexts, lseq: int) -> str:
        """An opaque, reader- and room-bound page token: the admission number it stands for is masked, so a
        token (like a step number) reveals nothing about the events the reader cannot see."""
        pad = self._cursor_pad(principal, contexts)
        ct = (lseq ^ int.from_bytes(pad[:8], "big")).to_bytes(8, "big")
        tag = hmac.new(pad, ct, "sha256").digest()[:8]
        return (ct + tag).hex()

    def _cursor_read(self, principal, contexts, token) -> int:
        pad = self._cursor_pad(principal, contexts)
        try:
            raw = bytes.fromhex(str(token))
        except ValueError:
            raise ValueError("invalid cursor") from None
        if len(raw) != 16 or not hmac.compare_digest(hmac.new(pad, raw[:8], "sha256").digest()[:8], raw[8:]):
            raise ValueError("invalid cursor")
        return int.from_bytes(raw[:8], "big") ^ int.from_bytes(pad[:8], "big")

    def room_ledger(self, principal, contexts, *, cursor: str | None = None, limit: int = LEDGER_DEFAULT,
                    since: float | None = None) -> dict:
        """A page of the events this reader may see in one room, in admission order. ``step`` numbers the
        reader's OWN visible events (1-based) and the page token is opaque, so nothing reveals how many hidden
        events exist."""
        if not isinstance(limit, int) or not 1 <= limit <= LEDGER_MAX:
            raise ValueError(f"limit must be between 1 and {LEDGER_MAX}")
        after = self._cursor_read(principal, contexts, cursor) if cursor not in (None, "") else None

        def compute():
            return self._room_entries(principal, contexts, since)

        def finish(state):
            entries, trimmed = state
            start = 0 if after is None else sum(1 for ev, _ in entries if _lseq(ev) <= after)
            page = entries[start:start + limit]
            more = start + limit < len(entries)
            return {"events": [self._event_view(start + i + 1, ev, op) for i, (ev, op) in enumerate(page)],
                    "visible_events": len(entries),
                    "next_cursor": self._cursor_make(principal, contexts, _lseq(page[-1][0])) if more else None,
                    "truncated": trimmed, "truncated_reason": "scan_budget" if trimmed else None}
        return self._coherent(compute, finish)

    def room_graph(self, principal, contexts, *, node_types: set | None = None, edge_types: set | None = None,
                   limit: int = DEFAULT_NODES, offset: int = 0) -> dict:
        """Every node type of ONE room's view (its own scope plus the missions it linked that the reader
        belongs to) with the visible edges among them, paged by ``offset``. Call it on a replayed projection
        to get the past; the two share every check. Budgets: ``SCAN_BUDGET`` ids examined, ``EDGE_SCAN_TOTAL``
        incident edges, ``MAX_EDGES`` edges and ``MAX_RESPONSE_BYTES`` bytes; ``truncated_reason`` names the
        one that ended the read and never counts hidden items."""
        if node_types is not None and not node_types <= NODE_TYPES:
            raise ValueError("unknown node_type")
        if edge_types is not None and not edge_types <= EDGE_TYPES:
            raise ValueError("unknown edge_type")
        if not isinstance(limit, int) or not 1 <= limit <= MAX_NODES:
            raise ValueError(f"limit must be between 1 and {MAX_NODES}")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be >= 0")
        with self._lock:
            reason = None
            kinds = [t for t in sorted(NODE_TYPES) if node_types is None or t in node_types]
            iters = {}
            for t in kinds:
                seen: dict = {}
                for c in sorted(contexts, key=repr):
                    seen.update(self._by_ctx.get(c, {}).get(t, {}))
                iters[t] = iter(seen)
            found: dict[str, list] = {t: [] for t in kinds}
            scanned, active = 0, list(kinds)
            while active:                       # round-robin: one busy type cannot starve the others of the budget
                for t in list(active):
                    nid = next(iters[t], None)
                    if nid is None:
                        active.remove(t)
                        continue
                    if scanned >= SCAN_BUDGET:
                        reason = "scan_budget"
                        active = []
                        break
                    scanned += 1
                    node = self._nodes[nid]
                    if not self._node_visible(principal, node):
                        continue
                    label = node.eff_scope(principal, internal=False) if node.commons else node.eff_scope()
                    if isinstance(label, tuple) and label[:3] in contexts:
                        found[t].append(nid)
            ids = [nid for t in kinds for nid in found[t]]
            rank = {nid: i for i, nid in enumerate(ids)}
            budget = MAX_RESPONSE_BYTES
            nodes, served = [], []
            for nid in ids[offset:offset + limit]:
                view = self._node_view(self._nodes[nid], principal)
                cost = len(json.dumps(view))
                if budget - cost < 0:
                    reason = reason or "response_bytes"
                    break
                budget -= cost
                nodes.append(view)
                served.append(nid)
            end = offset + len(served)
            if end < len(ids) and reason is None:
                reason = "limit"
            edges, done, left, ctx = [], set(), EDGE_SCAN_TOTAL, {}
            for nid in served:
                if left <= 0:
                    reason = reason or "edge_budget"
                    break
                per = min(NEIGHBOR_SCAN_BUDGET, left)
                left -= min(len(self._out.get(nid, ())) + len(self._in.get(nid, ())), per)
                own, others, exhausted = self._expand(principal, nid, "both", edge_types, False, None, per)
                if exhausted:
                    reason = reason or "edge_budget"
                for edge in own + others:
                    if edge.key in done or rank.get(edge.from_id, end) >= end or rank.get(edge.to_id, end) >= end:
                        continue
                    done.add(edge.key)
                    view = self._edge_view(edge, principal, ctx)
                    cost = len(json.dumps(view))
                    if len(edges) >= MAX_EDGES or budget - cost < 0:
                        reason = reason or ("limit" if len(edges) >= MAX_EDGES else "response_bytes")
                        continue
                    budget -= cost
                    edges.append(view)
            edges.sort(key=lambda e: (e["edge_type"], e["from_id"], e["to_id"], repr(e.get("binding"))))
            return {"nodes": nodes, "edges": edges, "total_nodes": len(ids), "offset": offset,
                    "next_offset": end if end < len(ids) else None,
                    "truncated": reason is not None, "truncated_reason": reason}

    def _coherent(self, compute, finish):
        """Run ``compute`` (entries, position, replay: all OUTSIDE the live lock) and ``finish`` (the graph read:
        under it) against one state of the world. If an event arrived in between, retry; the last try does
        everything under the lock, so the ``as_of`` of a response always matches what it shows."""
        for attempt in range(3):
            if attempt == 2:
                with self._lock:
                    return finish(compute())
            seen = self.events_applied
            state = compute()
            with self._lock:
                if self.events_applied == seen:
                    return finish(state)

    @staticmethod
    def _strip_pre_join(out: dict, post: set) -> dict:
        """``replay_history: since_join``: current values are shown, but the identity and time of any event that
        is not among the reader's post-join events (an older value's history) is dropped."""
        def fix(d):
            if isinstance(d, dict) and d.get("event_id") is not None and d["event_id"] not in post:
                d["event_id"] = None
                if "admitted_at" in d:
                    d["admitted_at"] = None
        for n in out.get("nodes", ()):
            for k in ("first_event_id", "last_event_id"):
                if n.get(k) is not None and n[k] not in post:
                    n[k] = None
            f = n.get("fields") or {}
            for v in (f.get("label"), f.get("state"), *(f.get("attrs") or {}).values()):
                fix(v)
            for lst in (n.get("contributions") or {}).values():
                for v in (lst.values() if isinstance(lst, dict) else [lst]):
                    for e in (v if isinstance(v, list) else [v]):
                        fix(e)
        for e in out.get("edges", ()):
            fix(e.get("provenance"))
            for v in (e.get("fields") or {}).values():
                fix(v)
        return out

    def strip_before(self, out: Any, joins: dict | None) -> Any:
        """T16 M2: ``replay_history: since_join`` for EVERY read, not just ``/lineage``. ``joins`` maps each room the
        reader reads through to the reader's join time. The horizon of an event is the join time of the room that
        SCOPES the event (never a room the caller merely named); an event with no room, or written from a room the
        reader has no join time for (a mission shared with the reader's room), uses the reader's LATEST join; an
        unknown event id, or an id shared with an earlier event, is treated as pre-join. Pre-join events lose their id, admission time and removal reason, and an edge that
        was removed before the reader joined is dropped altogether (no endpoints, no writer). Mutates ``out``."""
        if joins is None:
            return out
        first = self._first_ts
        latest = max(joins.values()) if joins else None

        def pre(eid):
            got = first.get(eid) if isinstance(eid, str) else None
            if got is None or latest is None:
                return True
            ts, room = got
            # a mission event written from a room this reader is not in (shared mission) is still visible to the
            # reader through THEIR room: fall back to their latest join, never to "everything is pre-join"
            horizon = joins.get(room, latest) if room is not None else latest
            return horizon is None or ts < horizon

        def dead(edge):          # removed before the reader joined: its existence is pre-join history
            return (isinstance(edge, dict) and "edge_type" in edge and edge.get("removed") is True
                    and isinstance(edge.get("removal"), dict) and pre(edge["removal"].get("event_id")))

        def walk(d, depth=0):
            if depth > 12:
                return
            if isinstance(d, list):
                d[:] = [x for x in d if not dead(x)]
                for x in d:
                    walk(x, depth + 1)
                return
            if not isinstance(d, dict):
                return
            for k in ("event_id", "first_event_id", "last_event_id"):
                if d.get(k) is not None and pre(d[k]):
                    d[k] = None
                    if k == "event_id":
                        for extra in ("admitted_at", "reason", "source_sequence"):
                            if extra in d:
                                d[extra] = None
            for v in d.values():
                walk(v, depth + 1)
        had_edges = isinstance(out, dict) and bool(out.get("edges"))
        walk(out)
        if had_edges and isinstance(out, dict) and not out["edges"] and "node" not in out and "nodes" not in out:
            raise KeyError("edge")           # the only edge asked for was removed before the reader joined
        return out

    def room_lineage(self, principal, contexts, *, step: int | None = None, at: str | None = None,
                     since: float | None = None, **kw) -> dict:
        """The room graph at a position in the reader's event ledger (default: now)."""
        def compute():
            entries, trimmed = self._room_entries(principal, contexts, since)
            k = self._position(entries, step, at)
            return entries, k, trimmed, self._replay(principal, contexts, entries, k)

        def finish(state):
            entries, k, trimmed, past = state
            out = {"as_of": self._as_of(entries, k, trimmed), **past.room_graph(principal, contexts, **kw)}
            if since is None:
                return out
            # L2: the id set alone is fooled by a hostile writer reusing a victim's event id; the earliest-admission
            # check below is not
            joins = {r: since for r in {c[1] if c[0] == "room" else c[2] for c in contexts if c[0] in ("room", "mission")}}
            return self.strip_before(self._strip_pre_join(out, {ev["event_id"] for ev, _ in entries}), joins)
        return self._coherent(compute, finish)

    def room_timeline(self, principal, contexts, *, step: int | None = None, at: str | None = None,
                      since: float | None = None) -> dict:
        """Gantt data from the reader's visible events up to a replay position: one lane per node whose
        ``spans`` are its recorded state periods, plus markers for edge events (spawn, handoff, produced,
        issue_link, reviewed, validated ...). A ``head_change`` marker is a reviewed/validated edge bound to
        a different revision than an earlier visible one for the same subject; ``stale`` is the projection's
        own flag as the reader sees it at that position, never a client guess. Times are server receipt times
        (``at``) with the emitter's claim alongside (``claimed``); nothing is inferred or invented."""
        def compute():
            entries, trimmed = self._room_entries(principal, contexts, since)
            k = self._position(entries, step, at)
            return entries, k, trimmed, self._replay(principal, contexts, entries, k)

        def finish(state):
            entries, k, trimmed, past = state
            return self._timeline_of(principal, past, entries, k, trimmed)
        return self._coherent(compute, finish)

    def _timeline_of(self, principal, past, entries: list, k: int, trimmed: bool) -> dict:
        shown = entries[:k]
        lanes: dict[str, dict] = {}
        markers: list[dict] = []
        bindings: dict[tuple, set] = {}
        ctx: dict = {}
        reason = "scan_budget" if trimmed else None
        for i, (ev, op) in enumerate(shown):
            view = self._event_view(i + 1, ev, op)
            at_, claimed = view["received_at"], view["occurred_at"]
            if "node_id" in view:
                lane = lanes.setdefault(view["node_id"], {"node_id": view["node_id"], "node_type": view["node_type"],
                                                          "first_at": at_, "last_at": at_, "last_step": 0, "marks": []})
                lane["last_at"], lane["last_step"] = at_, i + 1
                if view["state"]:
                    lane["marks"].append({"state": view["state"], "at": at_, "claimed": claimed,
                                          "reason": view["reason"], "step": i + 1})
                continue
            kind = "edge_removed" if op["op"] == "edge.remove" else "edge"
            stale = None
            if op["edge_type"] in BOUND_EDGE_TYPES and op["op"] == "edge.add":
                key = (op["edge_type"], op["to_id"])
                prior = bindings.setdefault(key, set())
                bound = op["binding"]
                if prior and bound not in prior:
                    kind = "head_change"
                prior.add(bound)
                found = [e for e in (past._edges[x] for x in past._by_triple.get(
                    (op["edge_type"], op["from_id"], op["to_id"]), ())) if e.binding == bound]
                if found and past._edge_visible(principal, found[0]):
                    stale = bool(past._edge_view(found[0], principal, ctx)["stale"])
            if len(markers) >= TIMELINE_MAX_MARKERS:
                reason = reason or "marker_limit"
                continue
            markers.append({"kind": kind, "step": i + 1, "at": at_, "claimed": claimed, "edge_type": op["edge_type"],
                            "from_id": op["from_id"], "to_id": op["to_id"], "binding": view["binding"],
                            "stale": stale})
        out_lanes = []
        for lane in sorted(lanes.values(), key=lambda x: x["last_step"], reverse=True)[:TIMELINE_MAX_LANES]:
            node = past._nodes.get(lane["node_id"])
            if node is None or not past._node_visible(principal, node):
                continue
            nv = past._node_view(node, principal)
            marks = lane.pop("marks")
            lane.pop("last_step")
            spans = []
            for j, m in enumerate(marks):
                nxt = marks[j + 1] if j + 1 < len(marks) else None
                terminal = m["state"] in TERMINAL_STATES
                spans.append({"state": m["state"], "start": m["at"], "claimed_start": m["claimed"],
                              "end": (nxt["at"] if nxt else (m["at"] if terminal else None)),
                              "claimed_end": (nxt["claimed"] if nxt else (m["claimed"] if terminal else None)),
                              "open": nxt is None and not terminal, "terminal": terminal,
                              "reason": m["reason"], "step": m["step"]})
            out_lanes.append({**lane, "label": nv["label"], "state": nv["state"], "spans": spans})
        if len(lanes) > TIMELINE_MAX_LANES:
            reason = reason or "lane_limit"
        out_lanes.sort(key=lambda x: (x["first_at"] or "", x["node_id"]))
        return {"as_of": self._as_of(entries, k, trimmed), "lanes": out_lanes, "markers": markers,
                "range": {"start": self._iso(shown[0][0].get("ingest_timestamp")) if shown else None,
                          "end": self._iso(shown[-1][0].get("ingest_timestamp")) if shown else None},
                "truncated": reason is not None, "truncated_reason": reason}

    # ------------------------------------------------------------ testing
    def snapshot(self) -> dict:
        """Unfiltered canonical state (tests and rebuild equality; never served)."""
        with self._lock:
            ctx: dict = {}
            nodes = {n.id: self._node_view(n, None, internal=True) for n in self._nodes.values()}
            edges = {}
            for k, e in self._edges.items():
                if self._live(e):
                    edges[repr(k)] = {**self._edge_view(e, None, ctx), "owner": e.owner(),
                                      "co_writers": list(e.co_writers())}
            wild = {repr(k): {"remove": [w.removes.best()[0], w.removes.best()[1], w.removes.best()[3]],
                              "scope": repr(w.scope), "owner": w.owner()}
                    for k, w in self._wild.items() if w.removes.best()}
            return {"nodes": nodes, "edges": edges, "wildcard_removes": wild,
                    "owned": dict(sorted(self._owned.items()))}


def read_graph(graph: GraphProjection, principal: dict | None, parts: list[str], q, *,
               contexts: frozenset | None = None) -> dict:
    """The one read router for both front doors (the keyed fabric API and the HCN-authenticated
    collab API). ``parts`` is the path after ``graph``; ``q(name)`` returns a query value or None.
    KeyError (hidden or missing) => 404, ValueError => 400, exactly as for a keyed read."""
    def num(name, default):
        raw = q(name)
        if raw in (None, ""):
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
    if parts == ["nodes"]:
        return graph.list_nodes(principal, q("node_type") or "", edge_type=q("edge_type") or None,
                                limit=num("limit", DEFAULT_NODES), contexts=contexts)
    if len(parts) == 2 and parts[0] == "nodes":
        return graph.get_node(principal, parts[1], edge_type=q("edge_type") or None,
                              from_id=q("from_id") or None, limit=num("limit", MAX_NODE_EDGES),
                              offset=num("offset", 0))
    if len(parts) == 3 and parts[0] == "nodes" and parts[2] in ("neighborhood", "subgraph"):
        kinds = q("edge_types")
        return graph.neighborhood(
            principal, parts[1], depth=num("depth", DEFAULT_DEPTH), max_nodes=num("max_nodes", DEFAULT_NODES),
            max_edges=num("max_edges", DEFAULT_EDGES), direction=q("direction") or "both",
            edge_types=set(kinds.split(",")) if kinds else None,
            include_removed=q("include_removed") == "true")
    if parts == ["edge"]:
        edge_type, frm, to = (q(n) for n in ("edge_type", "from_id", "to_id"))
        if not (edge_type and frm and to):
            raise ValueError("edge_type, from_id and to_id are required")
        return graph.get_edge(principal, edge_type, frm, to, commit=q("commit"), plan_digest=q("plan_digest"))
    raise KeyError("not found")
