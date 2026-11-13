"""Agent Operations projection (docs/specs/2026-11-12-agent-operations-map, ADR-001).

A read-only, metadata-only projection over sources that already exist: envelope v2 events that carry the optional
Agent Operations fields (``_envelope.agent_ops``), room events of the collaboration service, and handoffs. Nothing is
stored here. ``fold`` is a pure function: the same events in any order, with duplicates, give the same snapshot.

Privacy: only identifiers, enums, counters and the already-sanitized label are ever copied from a source; prompts,
message text, task text/results, file paths and error strings are never read. Authorization happens BEFORE the fold,
so an event the caller cannot read contributes to no node, edge or count.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

from .envelope import _OPS_ID_RE

EVENT_TYPES = frozenset({
    "session.started", "session.ended", "model.selected", "model.switched", "tool.started", "tool.ended",
    "tool.failed", "context.metric", "context.compaction", "message.sent", "message.delivered",
    "message.acknowledged", "delegation.proposed", "delegation.accepted", "delegation.rejected",
    "owner.transferred", "review.requested", "review.completed", "artifact.created", "artifact.verified",
    "tests.started", "tests.completed", "room.joined", "room.left", "agent.disconnected", "agent.reconnected",
    "drift.warning", "handoff.state"})
PROVENANCE = ("observed", "derived", "inferred", "unknown")

MAX_EVENTS = 5000                # newest events kept per request, across all sources
MAX_AGENTS = 500
MAX_EDGES = 2000
MAX_ROOMS = 200
MAX_MODEL_CHANGES = 50
MAX_TARGETS = 16
ROOM_EVENTS_PER_ROOM = 1000
PAGE_MAX = 500
STREAM_PAGE = 100

_ENVELOPE_TAIL = 500             # per entity stream
_USER = "user:"
_STATES = {"session.started": "idle", "session.ended": "ended", "tool.started": "working", "tool.ended": "idle",
           "tool.failed": "attention", "agent.disconnected": "disconnected", "agent.reconnected": "idle"}
_DELEGATION = {"delegation.proposed": "planned", "delegation.accepted": "accepted",
               "delegation.rejected": "rejected"}
_MESSAGE = {"message.sent": "sent", "message.delivered": "delivered", "message.acknowledged": "acknowledged"}
_REVIEW = {"review.requested": "planned", "review.completed": "completed"}
_HANDOFF = {"queued": "planned", "claimed": "planned", "accepted": "accepted", "in_progress": "accepted",
            "failed": "failed", "expired": "expired", "cancelled": "cancelled", "rejected": "rejected",
            "completed": "completed"}
_TASK_END = {"completed": "completed", "done": "completed", "succeeded": "completed", "failed": "failed",
             "error": "failed", "errored": "failed", "timed_out": "failed", "expired": "expired"}
_REJECTED_TASK = frozenset({"rejected", "declined", "denied", "cancelled"})
_CONTRIBUTION = frozenset({"artifact.created", "artifact.verified", "tests.started", "tests.completed"})
_IN_FLIGHT = frozenset({"planned", "accepted"})   # handoff edge states that an agent crash can interrupt
CONTEXT_ELEVATED = 0.70                           # amber
CONTEXT_HIGH = 0.90                               # red
CONTEXT_CRITICAL = 1.0
FAILURE_STREAK = 3                                # consecutive tool.failed (no tool.ended between) = drift signal
MAX_EVIDENCE = 10
HEURISTIC_NOTE = "Heuristic signal derived from event metadata; not a proven policy violation."


def _level(fraction: float | None) -> str:
    if fraction is None:
        return "unknown"
    return ("critical" if fraction >= CONTEXT_CRITICAL else "high" if fraction >= CONTEXT_HIGH
            else "elevated" if fraction >= CONTEXT_ELEVATED else "ok")


def context_health(context: dict | None) -> dict:
    """Derived health of one measured context. Unknown (no used, or no positive limit) stays unknown, never 0."""
    used = context.get("used") if context else None
    limit = context.get("limit") if context else None
    fraction = round(used / limit, 4) if used is not None and limit else None
    return {"fraction": fraction, "level": _level(fraction), "source": (context or {}).get("source"),
            "measured": fraction is not None}


def context_aggregate(agents: list[dict]) -> dict:
    """Average over MEASURED agents only; ``measured_count/total`` says how much of the group that covers."""
    fractions = [a["context_health"]["fraction"] for a in agents if a["context_health"]["measured"]]
    average = round(sum(fractions) / len(fractions), 4) if fractions else None
    return {"measured_count": len(fractions), "total": len(agents), "average_fraction": average,
            "level": _level(average)}


class Viewer(NamedTuple):
    """Who is asking. ``rooms`` are the room ids this caller can read right now; an API key has none."""
    rooms: frozenset = frozenset()
    operator: bool = False
    external: bool = False
    guest_rooms: frozenset = frozenset()     # rooms where the caller is a guest: room events only (no helper/agent data)
    guest_only: bool = False                 # every readable room is a guest room: only ``room:`` sources, whatever the key


# --------------------------------------------------------------------------------------------- normalisation
def _ts(value: Any) -> str:
    """One fixed-width UTC format so timestamps compare as strings; '' when unknown."""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, OverflowError, OSError):
        return ""


def _str(value: Any, limit: int = 160) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _ops_id(value: Any) -> str | None:
    """An id as the envelope would accept it; the registry validates the handoff id but not its source ids."""
    return value if isinstance(value, str) and _OPS_ID_RE.fullmatch(value) else None


def _uint(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def cursor_of(event: dict) -> str:
    """Total, append-stable order of the aggregated feed: receipt time, then the event's identity (source, agent, id), so
    two emitters (or two authenticated keys) that reuse an event id never share a cursor. Clients treat it as opaque and
    take it from the ``cursor`` field of a row; ``~`` inside a part is escaped so the parts stay unambiguous."""
    parts = (event["ingested_at"], event["source"], event["agent_id"] or "", event.get("actor_ref") or "")
    return "~".join([p.replace("~", "%7E") for p in parts] + [event["event_id"]])


CURSOR_MAX = 2048                       # ts 27 + source <=168 + agent <=160 + actor 12 + event id <=175 + separators, with headroom
_CURSOR_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
_CURSOR_ACTOR_RE = re.compile(r"[0-9a-f]{12}")


def valid_cursor(cursor: Any) -> bool:
    """Whether ``cursor`` has the shape :func:`cursor_of` issues: ``ts~source~agent~actor_ref~event_id`` (ts a real UTC
    timestamp or empty when the event had none, actor_ref empty or 12 hex, no CR/LF/NUL, bounded length)."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > CURSOR_MAX:
        return False
    if any(c in cursor for c in "\r\n\0"):          # the only characters that can break an SSE frame or a header
        return False
    parts = cursor.split("~", 4)
    if len(parts) != 5:
        return False
    stamp, source, _agent, actor, event_id = parts
    if stamp:
        if not _CURSOR_TS_RE.fullmatch(stamp):
            return False
        try:
            datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            return False
    return bool(source and event_id and (not actor or _CURSOR_ACTOR_RE.fullmatch(actor)))


def _identity(event: dict) -> tuple:
    return (event["source"], event["agent_id"] or "", event.get("actor_ref") or "", event["room_id"] or "",
            event["event_id"])


def _actor_ref(ev: dict) -> str:
    """A short stable digest of the authenticated actor (never the id itself): part of an event's identity."""
    actor = ev.get("actor")
    if isinstance(actor, str):
        try:
            actor = json.loads(actor)
        except ValueError:
            actor = None
    actor_id = actor.get("id") if isinstance(actor, dict) else None
    return hashlib.sha256(actor_id.encode()).hexdigest()[:12] if isinstance(actor_id, str) and actor_id else ""


def _event(**fields: Any) -> dict:
    base = {"event_id": None, "source": "unknown", "sequence": 0, "event_type": None, "occurred_at": "",
            "ingested_at": "", "room_id": None, "visibility_scope": "private", "provenance": "unknown",
            "agent_id": None, "session_id": None, "host_id": None, "model_id": None, "parent_session_id": None,
            "handoff_id": None, "task_id": None, "status": None, "sanitized_label": None, "payload": {},
            "actor_ref": ""}
    base.update(fields)
    return base


def _payload(raw: Any) -> dict:
    """The only payload keys the projection reads, type-checked; everything else is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in ("context_used", "context_limit"):
        if _uint(raw.get(key)) is not None:
            out[key] = raw[key]
    for key in ("context_source", "provider", "target_agent_id", "target_session_id", "new_owner_agent_id"):
        if _str(raw.get(key), 64 if key in ("context_source", "provider") else 160):
            out[key] = _str(raw[key], 64 if key in ("context_source", "provider") else 160)
    targets = raw.get("target_agent_ids")
    if isinstance(targets, list):
        out["target_agent_ids"] = [t[:160] for t in targets if isinstance(t, str) and t][:MAX_TARGETS]
    return out


def _norm_envelope(ev: dict) -> dict | None:
    if ev.get("schema_version") != 2:
        return None                          # only a v2 ingest has its _envelope set by the server
    payload = ev.get("payload") or {}
    meta = payload.get("_envelope") if isinstance(payload, dict) else None
    ops = meta.get("agent_ops") if isinstance(meta, dict) else None
    if not isinstance(ops, dict) or ev.get("event_type") not in EVENT_TYPES:
        return None
    sequence = ev.get("source_sequence")
    scope = ev.get("visibility_scope") or "private"
    return _event(
        event_id=ev["event_id"], source=_str(ev.get("source"), 32) or "unknown",
        sequence=sequence if sequence is not None else ev.get("sequence", 0),
        event_type=ev["event_type"], occurred_at=_ts(ev.get("source_timestamp")),
        ingested_at=_ts(ev.get("ingest_timestamp")) or _ts(ev.get("source_timestamp")),
        room_id=_str(ev.get("room_id")) if scope == "room" else None,   # a room id counts only on a room-scoped event
        visibility_scope=scope, actor_ref=_actor_ref(ev),
        provenance=ops.get("provenance") if ops.get("provenance") in PROVENANCE else "unknown",
        agent_id=_str(ev.get("agent_id")), session_id=_str(ops.get("session_id")),
        host_id=_str(ops.get("host_id")), model_id=_str(ops.get("model_id")),
        parent_session_id=_str(ops.get("parent_session_id")), handoff_id=_str(ops.get("handoff_id")),
        task_id=_str(ev.get("task_id")), status=_str(ops.get("status"), 64),
        sanitized_label=_str(ops.get("sanitized_label"), 80), payload=_payload(payload))


_CARRY_SCAN = 5000
_CARRY_MAX = MAX_AGENTS * 6


def _category(ev: dict) -> str | None:
    """The kinds whose LAST value is still the current truth after older history slides out of a window."""
    kind = ev["event_type"]
    return ("model" if kind in ("model.selected", "model.switched") else "context" if kind == "context.metric"
            else "state" if kind in _STATES else "room" if kind in ("room.joined", "room.left") else None)


def carry_forward(dropped: list[dict]) -> list[dict]:
    """From events a sliding window dropped (ascending), the newest of each (kind, entity): last-known model, context,
    state and room membership survive truncation. They are real, original events, never synthesized."""
    latest: dict[tuple, dict] = {}
    for ev in dropped:
        category = _category(ev)
        if category:
            latest[(category, ev["source"], ev["actor_ref"], ev["agent_id"], ev["session_id"], ev["host_id"],
                    ev["room_id"])] = ev
    return sorted(latest.values(), key=cursor_of)[-_CARRY_MAX:]


def envelope_events(repo: Any) -> list[dict]:
    """Stored envelope events that carry the Agent Operations block, as normalized events. Each stream keeps its newest
    ``_ENVELOPE_TAIL`` plus the last-known model/context/state of what slid out."""
    out: list[dict] = []
    with repo._event_lock:
        streams = [(list(stream[-_ENVELOPE_TAIL:]), list(stream[:-_ENVELOPE_TAIL][-_CARRY_SCAN:]))
                   for stream in repo._events.values()]
    for tail, dropped in streams:
        norm_dropped = [n for n in (_norm_envelope(ev) for ev in dropped) if n is not None]
        out += carry_forward(norm_dropped)
        out += [n for n in (_norm_envelope(ev) for ev in tail) if n is not None]
    return out


def room_events(collab: Any, principal: Any, room_id: str) -> list[dict]:
    """The newest ``ROOM_EVENTS_PER_ROOM`` room events as ``principal`` may read them (the room service authorizes and
    filters, from the tail), as normalized events."""
    from .collab import CollabError
    try:
        raw = collab.read_tail(principal, room_id, ROOM_EVENTS_PER_ROOM)["events"]
    except CollabError:
        return []
    out = []
    for ev in raw:
        norm = _room_event(ev, room_id)
        if norm is not None:
            out.append(norm)
    return out


def _room_event(ev: dict, room_id: str) -> dict | None:
    kind, stamp = ev.get("event_type"), _ts(ev.get("created_at"))
    common = {"event_id": ev["event_id"], "source": f"room:{room_id}", "sequence": ev["sequence"],
              "occurred_at": stamp, "ingested_at": stamp, "room_id": room_id, "visibility_scope": "room",
              "provenance": "observed"}
    if kind == "message":
        sender = ev.get("sender") or {}
        sid = _str(sender.get("id"))
        if sid is None:
            return None
        person = sender.get("type") == "user"
        targets = [m["id"] for m in ev.get("mentions") or []
                   if isinstance(m, dict) and m.get("kind") == "agent" and _str(m.get("id"))][:MAX_TARGETS]
        return _event(**common, event_type="message.sent", agent_id=_USER + sid if person else sid,
                      payload={"target_agent_ids": targets} if targets else {})
    if kind == "agent_granted":
        return _event(**common, event_type="room.joined", agent_id=_str(ev.get("agent_id")))
    if kind in ("agent_grant_revoked", "agent_grant_expired", "agent_revoked"):
        return _event(**common, event_type="room.left", agent_id=_str(ev.get("agent_id")))
    if kind in ("task_created", "task_updated"):
        task = ev.get("task") or {}
        status = _str(task.get("status"), 32) or "pending"
        by = task.get("requested_by") or {}
        origin = _str(ev.get("actor_agent_id")) or (_str(by.get("id")) if by.get("type") == "agent" else None) \
            or (_USER + str(ev.get("actor_user_id")) if ev.get("actor_user_id") else None)
        etype = ("delegation.proposed" if status == "pending" else
                 "delegation.rejected" if status in _REJECTED_TASK else "delegation.accepted")
        target = _str(task.get("target_agent_id"))
        return _event(**common, event_type=etype, agent_id=origin, task_id=_str(task.get("task_id")), status=status,
                      payload={"target_agent_id": target} if target else {})
    return None


def handoff_events(registry: Any) -> list[dict]:
    """One ``handoff.state`` event per recorded handoff transition (ids and states only, never the goal text)."""
    out = []
    for row in registry.list(limit=500):
        hid, src, dst = row["handoff_id"], row.get("source") or {}, row.get("target") or {}
        for index, step in enumerate(row.get("history") or []):
            stamp = _ts(step.get("at"))
            payload = {k: v for k, v in (("target_agent_id", _str(dst.get("agent_id"))),
                                         ("target_session_id", _str(dst.get("session_id")))) if v}
            out.append(_event(
                event_id=f"handoff:{hid}:{index}", source=f"handoff:{hid}", sequence=index,
                event_type="handoff.state", occurred_at=stamp, ingested_at=stamp, room_id=_str(row.get("room_id")),
                visibility_scope="room" if row.get("room_id") else "private", provenance="observed",
                agent_id=_ops_id(src.get("agent_id")), session_id=_ops_id(src.get("session_id")), handoff_id=hid,
                status=step.get("state") if isinstance(step.get("state"), str) else None,
                payload={**payload, "provider": _str(src.get("harness"), 64) or ""} if src.get("harness") else payload))
    return out


def visible(event: dict, viewer: Viewer) -> bool:
    """Fail closed: a room event needs room access; a room-less event needs org scope or the operator."""
    scope = event["visibility_scope"]
    if viewer.guest_only:
        return bool(event["room_id"] in viewer.guest_rooms and event["source"].startswith("room:"))
    if scope == "private":
        return viewer.operator
    if event["room_id"]:
        if (viewer.external or event["room_id"] in viewer.guest_rooms) and not event["source"].startswith("room:"):
            return False                     # chat-only callers never see envelope/handoff data of a room
        return event["room_id"] in viewer.rooms
    return scope == "org" or viewer.operator


def authorized_events(events: list[dict], viewer: Viewer) -> list[dict]:
    """Visible events, newest ``MAX_EVENTS`` by cursor, ascending. Filtering happens before any aggregation."""
    kept = sorted((e for e in events if visible(e, viewer)), key=cursor_of)
    if len(kept) <= MAX_EVENTS:
        return kept
    window = kept[-MAX_EVENTS:] if MAX_EVENTS else []
    return sorted(carry_forward(kept[:len(kept) - len(window)]) + window, key=cursor_of)


# --------------------------------------------------------------------------------------------------- fold
def _ordered(events: list[dict]) -> list[dict]:
    """Dedupe by identity (source, agent, room, event id), then order by (effective time, source, sequence): a source's
    own sequence is authoritative (its effective time never runs backwards), sources interleave by time. Two emitters
    that reuse an event id stay distinct. Deterministic for any input order."""
    unique: dict[tuple, dict] = {}
    for ev in events:
        known = unique.get(_identity(ev))
        if known is None or _canon(ev) < _canon(known):
            unique[_identity(ev)] = ev
    by_source: dict[str, list[dict]] = {}
    for ev in unique.values():
        by_source.setdefault(ev["source"], []).append(ev)
    keyed = []
    for source in sorted(by_source):
        floor = ""
        for ev in sorted(by_source[source], key=lambda e: (e["sequence"], e["agent_id"] or "", e["actor_ref"], e["event_id"])):
            floor = max(floor, ev["occurred_at"] or ev["ingested_at"])
            keyed.append(((floor, source, ev["sequence"], ev["agent_id"] or "", ev["actor_ref"], ev["event_id"]), ev))
    return [ev for _, ev in sorted(keyed, key=lambda item: item[0])]


def _canon(ev: dict) -> str:
    return json.dumps(ev, sort_keys=True, default=str)


class _Fold:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.redirect: dict[str, str] = {}
        self.rooms: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.hosts: dict[str, dict] = {}

    # -- nodes
    def node(self, agent_id: str | None, session_id: str | None) -> str | None:
        if agent_id and agent_id.startswith(_USER):
            return agent_id                      # a person is an endpoint, never an agent node
        if session_id:
            key = session_id
            if agent_id and agent_id in self.nodes and self.nodes[agent_id]["session_id"] is None \
                    and key not in self.nodes:   # a sessionless node learns its session
                moved = self.nodes.pop(agent_id)
                moved["node_id"], moved["session_id"] = key, session_id
                self.nodes[key] = moved
                self.redirect[agent_id] = key
        elif agent_id:
            key = self._latest(agent_id) or agent_id
        else:
            return None
        if key not in self.nodes:
            self.nodes[key] = {
                "node_id": key, "agent_id": agent_id or key, "session_id": session_id, "host_id": None,
                "provider": None, "model": None, "model_changes": [], "state": "unknown", "context": None,
                "room_ids": [], "parent_session_id": None, "compactions": 0, "drift_warnings": 0,
                "label": None, "provenance": "unknown", "last_event_at": None,
                "_prior": None, "_by_host": None, "_fails": [], "_reported": []}
        elif agent_id and self.nodes[key]["agent_id"] == key and agent_id != key:
            self.nodes[key]["agent_id"] = agent_id
        return key

    def _latest(self, agent_id: str) -> str | None:
        found = [n for n in self.nodes.values() if n["agent_id"] == agent_id]
        return max(found, key=lambda n: (n["last_event_at"] or "", n["node_id"]))["node_id"] if found else None

    def target(self, payload: dict) -> list[str]:
        out = []
        if payload.get("target_session_id") or payload.get("target_agent_id"):
            out.append(self.node(payload.get("target_agent_id"), payload.get("target_session_id")))
        for agent_id in payload.get("target_agent_ids") or []:
            out.append(self.node(agent_id, None))
        return [t for t in out if t]

    # -- edges and tasks
    def edge(self, kind: str, src: str | None, dst: str | None, state: str, ev: dict, ident: str = "") -> None:
        if src is None or dst is None:
            return                               # never invent an endpoint
        key = (kind, src, dst, ev["room_id"] or "", ident)
        self.edges[key] = {"kind": kind, "from": src, "to": dst, "state": state, "room_id": ev["room_id"],
                           "event_id": ev["event_id"], "provenance": ev["provenance"], "task_id": ev["task_id"],
                           "handoff_id": ev["handoff_id"], "updated_at": ev["occurred_at"] or ev["ingested_at"]}

    def task(self, ev: dict) -> dict | None:
        if not ev["task_id"]:
            return None
        return self.tasks.setdefault(ev["task_id"], {
            "task_id": ev["task_id"], "room_id": ev["room_id"], "accountable_owner": None, "contributors": [],
            "reviewer": None, "updated_at": None})

    @staticmethod
    def _add(items: list, value: str | None) -> None:
        if value and value not in items:
            items.append(value)

    # -- one event
    def apply(self, ev: dict) -> None:
        kind, payload = ev["event_type"], ev["payload"]
        when = ev["occurred_at"] or ev["ingested_at"]
        me = self.node(ev["agent_id"], ev["session_id"])
        if ev["host_id"]:
            host = self.hosts.setdefault(ev["host_id"], {"connected": None, "reconnects": 0, "last_seen": ""})
            host["last_seen"] = max(host["last_seen"], when)
            if me is None and kind in ("agent.disconnected", "agent.reconnected"):
                self._host_link(ev["host_id"], host, kind, ev)
        if ev["room_id"]:
            room = self.rooms.setdefault(ev["room_id"], {"room_id": ev["room_id"], "agent_ids": []})
            if me and not me.startswith(_USER) and kind != "room.left":
                self._add(room["agent_ids"], me)
        task = self.task(ev)
        if task is not None:
            task["updated_at"] = when
        if me and me in self.nodes:
            node = self.nodes[me]
            node["last_event_at"] = max(node["last_event_at"] or "", when) or None
            self._agent_event(node, ev, kind, payload, when)
        if kind in _MESSAGE:
            for dst in self.target(payload):
                self.edge("communication", me, dst, _MESSAGE[kind], ev, ev["task_id"] or "")
        elif kind in _DELEGATION:
            state = (_TASK_END.get(ev["status"] or "") or _DELEGATION[kind]) if kind == "delegation.accepted" \
                else _DELEGATION[kind]               # a finished task is never drawn as an accepted delegation
            for dst in self.target(payload):
                self.edge("delegation", me, dst, state, ev, ev["task_id"] or "")
                if state == "accepted" and task is not None:
                    self._add(task["contributors"], dst)   # a contributor, never the accountable owner
        elif kind in _REVIEW:
            for dst in self.target(payload):
                self.edge("review", me, dst, _REVIEW[kind], ev, ev["task_id"] or "")
                if task is not None and kind == "review.requested":
                    task["reviewer"] = dst
        elif kind == "owner.transferred":
            new_owner = self.node(payload.get("new_owner_agent_id"), None) if payload.get("new_owner_agent_id") else None
            if new_owner:
                self.edge("ownership", me, new_owner, "transferred", ev, ev["task_id"] or "")
                if task is not None:
                    task["accountable_owner"] = new_owner
        elif kind == "handoff.state":
            for dst in self.target(payload):
                self.edge("handoff", me, dst, _HANDOFF.get(ev["status"] or "", ev["status"] or "unknown"), ev,
                          ev["handoff_id"] or "")
        elif kind in _CONTRIBUTION and task is not None and me and not me.startswith(_USER):
            self._add(task["contributors"], me)

    def _host_link(self, host_id: str, host: dict, kind: str, ev: dict) -> None:
        """A host-level (agent-less) disconnect/reconnect moves every agent known on that host."""
        members = [n for n in self.nodes.values() if n["host_id"] == host_id]
        if kind == "agent.disconnected" and host["connected"] is not False:
            host["connected"] = False
            for node in members:
                if node["state"] not in ("disconnected", "ended"):
                    node["_prior"], node["_by_host"], node["state"] = node["state"], host_id, "disconnected"
                    self._interrupt(node["node_id"], "stalled", ev)
        elif kind == "agent.reconnected":
            if host["connected"] is False:
                host["reconnects"] += 1
            host["connected"] = True
            for node in members:
                if node["_by_host"] == host_id:
                    node["state"], node["_by_host"], node["_prior"] = node["_prior"] or "idle", None, None

    def _interrupt(self, node_id: str, state: str, ev: dict) -> None:
        """A handoff in flight to/from an agent that disconnected or ended is stalled/failed, never accepted."""
        for edge in self.edges.values():
            if edge["kind"] == "handoff" and edge["state"] in _IN_FLIGHT and node_id in (edge["from"], edge["to"]):
                edge["state"], edge["interrupted_by"] = state, ev["event_id"]

    def _agent_event(self, node: dict, ev: dict, kind: str, payload: dict, when: str) -> None:
        for field in ("host_id", "parent_session_id"):
            node[field] = ev[field] or node[field]
        node["provider"] = payload.get("provider") or node["provider"]
        if ev["sanitized_label"]:
            node["label"] = ev["sanitized_label"]
        if ev["provenance"] != "unknown" or node["provenance"] == "unknown":
            node["provenance"] = ev["provenance"]
        if kind in _STATES:
            if node["_by_host"] and kind != "agent.reconnected":
                node["_prior"] = _STATES[kind]   # host is down: remember what to restore, stay disconnected
            else:
                node["state"], node["_by_host"] = _STATES[kind], None
            if kind == "agent.disconnected":
                self._interrupt(node["node_id"], "stalled", ev)
            elif kind == "session.ended":
                self._interrupt(node["node_id"], "failed", ev)
        if kind == "tool.failed":
            node["_fails"].append((ev["event_id"], ev["task_id"]))
            del node["_fails"][:-MAX_EVIDENCE]
        elif kind == "tool.ended":
            node["_fails"] = []
        if kind in ("model.selected", "model.switched") and ev["model_id"] and ev["model_id"] != node["model"]:
            node["model_changes"].append({"from": node["model"], "to": ev["model_id"], "at": when,
                                          "event_id": ev["event_id"]})
            del node["model_changes"][:-MAX_MODEL_CHANGES]
            node["model"] = ev["model_id"]
        elif ev["model_id"] and node["model"] is None:
            node["model"] = ev["model_id"]
        if kind == "context.metric":
            used = payload.get("context_used")
            node["context"] = None if used is None else {
                "used": used, "limit": payload.get("context_limit"), "source": payload.get("context_source")}
        elif kind == "context.compaction":
            node["compactions"] += 1
        elif kind == "drift.warning":
            node["drift_warnings"] += 1          # derived/inferred; never a proven policy violation
            node["_reported"].append((ev["event_id"], ev["provenance"], ev["task_id"]))
            del node["_reported"][:-MAX_EVIDENCE]
        elif kind == "room.joined" and ev["room_id"]:
            self._add(node["room_ids"], ev["room_id"])
        elif kind == "room.left" and ev["room_id"] and ev["room_id"] in node["room_ids"]:
            node["room_ids"].remove(ev["room_id"])
        if ev["room_id"] and kind not in ("room.left",):
            self._add(node["room_ids"], ev["room_id"])

    def _hosts(self, agents: list[dict]) -> list[dict]:
        ids = sorted(set(self.hosts) | {a["host_id"] for a in agents if a["host_id"]})[:MAX_ROOMS]
        out = []
        for host_id in ids:
            meta = self.hosts.get(host_id) or {"connected": None, "reconnects": 0, "last_seen": ""}
            members = [a for a in agents if a["host_id"] == host_id]
            state = "unknown" if meta["connected"] is None else "connected" if meta["connected"] else "disconnected"
            out.append({"host_id": host_id, "agents": [a["node_id"] for a in members], "state": state,
                        "last_seen": meta["last_seen"] or None, "connected": meta["connected"],
                        "reconnects": meta["reconnects"], "context_health": context_aggregate(members)})
        return out

    def result(self, cursor: str | None, event_count: int) -> dict:
        def fix(ref: str | None) -> str | None:
            seen = 0
            while ref in self.redirect and seen < 16:
                ref, seen = self.redirect[ref], seen + 1
            return ref

        agents = sorted(self.nodes.values(), key=lambda n: n["node_id"])[:MAX_AGENTS]
        by_id = {n["node_id"]: n for n in agents}
        for node in agents:
            node["context_health"] = context_health(node["context"])
        rooms = []
        for room in sorted(self.rooms.values(), key=lambda r: r["room_id"])[:MAX_ROOMS]:
            ids = sorted({fix(a) for a in room["agent_ids"]})
            rooms.append({"room_id": room["room_id"], "agent_ids": ids,
                          "context_health": context_aggregate([by_id[i] for i in ids if i in by_id])})
        edges = []
        for edge in sorted(self.edges.values(), key=lambda e: (e["kind"], e["from"], e["to"], e["event_id"])):
            edges.append({**edge, "from": fix(edge["from"]), "to": fix(edge["to"])})
        tasks = []
        for task in sorted(self.tasks.values(), key=lambda t: t["task_id"]):
            tasks.append({**task, "accountable_owner": fix(task["accountable_owner"]),
                          "reviewer": fix(task["reviewer"]),
                          "contributors": sorted({fix(c) for c in task["contributors"]})})
        for node in agents:
            node["room_ids"] = sorted(node["room_ids"])
        _drift(agents, by_id, edges, tasks)
        hosts = self._hosts(agents)
        out_agents = [{k: v for k, v in copy.deepcopy(n).items() if not k.startswith("_")} for n in agents]
        return {"agents": out_agents, "rooms": rooms, "edges": edges[:MAX_EDGES], "tasks": tasks,
                "cursor": cursor, "hosts": hosts, "health": {"context": context_aggregate(agents)},
                "counts": {"agents": len(agents), "rooms": len(rooms), "edges": len(edges[:MAX_EDGES]),
                           "tasks": len(tasks), "events": event_count, "hosts": len(hosts)},
                "truncated": len(self.nodes) > MAX_AGENTS or len(self.edges) > MAX_EDGES
                or len(self.rooms) > MAX_ROOMS}


def _drift(agents: list[dict], by_id: dict, edges: list[dict], tasks: list[dict]) -> None:
    """Attach ``drift`` warnings (heuristics from real event fields only) to agents and tasks. Never raw content."""
    def entry(kind: str, provenance: str, evidence: list[str], task_id: str | None) -> dict:
        return {"kind": kind, "provenance": provenance, "evidence_event_ids": evidence[-MAX_EVIDENCE:],
                "task_id": task_id, "heuristic": True, "note": HEURISTIC_NOTE}

    for node in agents:
        node["drift"] = []
        if node["_reported"]:
            marks = {p for _, p, _ in node["_reported"]}
            node["drift"].append(entry("reported_drift_warning", "inferred" if "inferred" in marks else "derived",
                                       [e for e, _, _ in node["_reported"]], node["_reported"][-1][2]))
        if len(node["_fails"]) >= FAILURE_STREAK:
            node["drift"].append(entry("repeated_tool_failures", "derived", [e for e, _ in node["_fails"]],
                                       node["_fails"][-1][1]))
    for edge in edges:
        if edge["kind"] == "handoff" and edge["state"] in ("stalled", "failed"):
            evidence = [e for e in (edge.get("interrupted_by"), edge["event_id"]) if e]
            for end in {edge["from"], edge["to"]}:
                if end in by_id:
                    by_id[end]["drift"].append(entry(f"handoff_{edge['state']}", "derived", evidence, edge["task_id"]))
    for node in agents:
        node["drift"].sort(key=lambda d: (d["kind"], d["evidence_event_ids"]))
    for task in tasks:
        task["drift"] = [d for n in agents for d in n["drift"] if d["task_id"] == task["task_id"]]


def fold(events: list[dict], up_to: str | None = None) -> dict:
    """Deterministic replay: the snapshot after every (already authorized) event whose cursor is <= ``up_to``."""
    chosen = [e for e in events if up_to is None or cursor_of(e) <= up_to]
    ordered = _ordered(chosen)
    state = _Fold()
    for ev in ordered:
        state.apply(ev)
    last = max((cursor_of(e) for e in ordered), default=None)
    return state.result(last, len(ordered))


# --------------------------------------------------------------------------------------------- service
class AgentOpsService:
    """Gathers the sources for one authorized caller and answers snapshot / events / replay."""

    CACHE_TTL = 1.0                  # seconds a streaming reader may reuse one viewer's gathered events
    CACHE_MAX = 64

    def __init__(self, repo: Any, collab: Any = None, handoffs: Any = None, now: Any = None):
        self.repo, self.collab, self.handoffs = repo, collab, handoffs
        self._now = now or time.time
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._cache_lock = threading.Lock()

    def viewer(self, principal: Any = None, operator: bool = False) -> Viewer:
        rooms = frozenset(self.collab.readable_room_ids(principal)) if self.collab and principal is not None else frozenset()
        guests = self._guest_rooms(principal, rooms)
        # Fail closed: someone who is (or ever was) a guest and reads no other room has nothing to see. A lapsed or
        # revoked guest keeps a valid sign-in, so without this they would fall back to org-scoped visibility.
        external = bool(getattr(principal, "external", False))
        # An external agent is chat-only like a guest: its bound room(s) are the only sources it may read, and with no
        # room left (binding expired or revoked) it reads nothing, never the org scope.
        guest_only = external or (bool(guests) and guests == rooms) or (not rooms and self._was_guest(principal))
        return Viewer(rooms=rooms, operator=operator, external=external,
                      guest_rooms=rooms if external else guests, guest_only=guest_only)

    def _was_guest(self, principal: Any) -> bool:
        """Latest membership was a guest one. For an agent, only when its owner reads no room other than guest rooms."""
        collab = self.collab
        if collab is None or principal is None:
            return False
        with collab._lock:
            is_user = principal.kind == "user"
            uid = principal.id if is_user else (collab._agents.get(principal.id) or {}).get("owner")
            if not uid or uid not in collab._ever_guests:
                return False
        if is_user:
            return True
        owner_rooms = frozenset(collab.readable_room_ids(type(principal)("user", uid, uid)))
        return not owner_rooms or owner_rooms == self._guest_rooms(type(principal)("user", uid, uid), owner_rooms)

    def _guest_rooms(self, principal: Any, rooms: frozenset) -> frozenset:
        """Rooms where the caller (or the person an agent acts for) is a guest."""
        collab = self.collab
        if collab is None or principal is None or not rooms:
            return frozenset()
        with collab._lock:
            uid = principal.id if principal.kind == "user" else (collab._agents.get(principal.id) or {}).get("owner")
            found = set()
            for rid in rooms:
                room = collab._rooms.get(rid)
                member = collab._member(room, uid) if room and uid else None
                if member and member.get("role") == "guest":
                    found.add(rid)
            return frozenset(found)

    def _gather(self, viewer: Viewer, principal: Any, cached: bool = False) -> list[dict]:
        key = (viewer, getattr(principal, "id", None))
        if cached:
            with self._cache_lock:
                hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < self.CACHE_TTL:
                return hit[1]
        events = envelope_events(self.repo)
        if self.handoffs is not None:
            events += handoff_events(self.handoffs)
        if self.collab is not None and principal is not None:
            for room_id in sorted(viewer.rooms)[:MAX_ROOMS]:
                events += room_events(self.collab, principal, room_id)
        out = authorized_events(events, viewer)
        if cached:
            with self._cache_lock:
                if len(self._cache) >= self.CACHE_MAX:
                    self._cache.clear()
                self._cache[key] = (time.monotonic(), out)
        return out

    def _named(self, snap: dict, viewer: Viewer) -> dict:
        """Additive: ``rooms[].name`` from the collab room, only for rooms this viewer can read; otherwise null."""
        for room in snap["rooms"]:
            name = None
            if self.collab is not None and room["room_id"] in viewer.rooms:
                with self.collab._lock:
                    name = (self.collab._rooms.get(room["room_id"]) or {}).get("name")
            room["name"] = name if isinstance(name, str) and name else None
        return snap

    def snapshot(self, viewer: Viewer, principal: Any = None) -> dict:
        return self._named(fold(self._gather(viewer, principal)), viewer)

    def events(self, viewer: Viewer, principal: Any = None, cursor: str | None = None, limit: int = 100,
               cached: bool = False) -> dict:
        limit = max(1, min(limit, PAGE_MAX))
        ordered = [e for e in self._gather(viewer, principal, cached) if cursor is None or cursor_of(e) > cursor]
        page = [{**e, "cursor": cursor_of(e)} for e in ordered[:limit]]
        return {"events": page, "next_cursor": page[-1]["cursor"] if page else cursor,
                "has_more": len(ordered) > limit}

    def replay(self, viewer: Viewer, principal: Any = None, until: str | None = None) -> dict:
        return self._named(fold(self._gather(viewer, principal), until), viewer)
