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
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, NamedTuple

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
_HANDOFF = {"queued": "planned", "claimed": "planned", "accepted": "accepted", "in_progress": "accepted"}
_REJECTED_TASK = frozenset({"rejected", "declined", "denied", "cancelled"})
_CONTRIBUTION = frozenset({"artifact.created", "artifact.verified", "tests.started", "tests.completed"})


class Viewer(NamedTuple):
    """Who is asking. ``rooms`` are the room ids this caller can read right now; an API key has none."""
    rooms: frozenset = frozenset()
    operator: bool = False
    external: bool = False


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


def _uint(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def cursor_of(event: dict) -> str:
    """Total, append-stable order of the aggregated feed: server receipt time, then event id."""
    return f"{event['ingested_at']}~{event['event_id']}"


def _event(**fields: Any) -> dict:
    base = {"event_id": None, "source": "unknown", "sequence": 0, "event_type": None, "occurred_at": "",
            "ingested_at": "", "room_id": None, "visibility_scope": "private", "provenance": "unknown",
            "agent_id": None, "session_id": None, "host_id": None, "model_id": None, "parent_session_id": None,
            "handoff_id": None, "task_id": None, "status": None, "sanitized_label": None, "payload": {}}
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


def envelope_events(repo: Any) -> list[dict]:
    """Stored envelope events that carry the Agent Operations block, as normalized events."""
    out: list[dict] = []
    with repo._event_lock:
        streams = [list(stream[-_ENVELOPE_TAIL:]) for stream in repo._events.values()]
    for stream in streams:
        for ev in stream:
            payload = ev.get("payload") or {}
            meta = payload.get("_envelope") if isinstance(payload, dict) else None
            ops = meta.get("agent_ops") if isinstance(meta, dict) else None
            if not isinstance(ops, dict) or ev.get("event_type") not in EVENT_TYPES:
                continue
            sequence = ev.get("source_sequence")
            out.append(_event(
                event_id=ev["event_id"], source=_str(ev.get("source"), 32) or "unknown",
                sequence=sequence if sequence is not None else ev.get("sequence", 0),
                event_type=ev["event_type"], occurred_at=_ts(ev.get("source_timestamp")),
                ingested_at=_ts(ev.get("ingest_timestamp")) or _ts(ev.get("source_timestamp")),
                room_id=_str(ev.get("room_id")), visibility_scope=ev.get("visibility_scope") or "private",
                provenance=ops.get("provenance") if ops.get("provenance") in PROVENANCE else "unknown",
                agent_id=_str(ev.get("agent_id")), session_id=_str(ops.get("session_id")),
                host_id=_str(ops.get("host_id")), model_id=_str(ops.get("model_id")),
                parent_session_id=_str(ops.get("parent_session_id")), handoff_id=_str(ops.get("handoff_id")),
                task_id=_str(ev.get("task_id")), status=_str(ops.get("status"), 64),
                sanitized_label=_str(ops.get("sanitized_label"), 80), payload=_payload(payload)))
    return out


def room_events(collab: Any, principal: Any, room_id: str) -> list[dict]:
    """Room events as ``principal`` may read them (the room service authorizes and filters), as normalized events."""
    from .collab import CollabError
    raw: deque = deque(maxlen=ROOM_EVENTS_PER_ROOM)
    cursor = 0
    try:
        for _ in range(100):
            page = collab.read_events(principal, room_id, cursor, 100)
            raw.extend(page["events"])
            if not page["has_more"] or page["next_cursor"] == cursor:
                break
            cursor = page["next_cursor"]
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
                agent_id=_str(src.get("agent_id")), session_id=_str(src.get("session_id")), handoff_id=hid,
                status=step.get("state") if isinstance(step.get("state"), str) else None,
                payload={**payload, "provider": _str(src.get("harness"), 64) or ""} if src.get("harness") else payload))
    return out


def visible(event: dict, viewer: Viewer) -> bool:
    """Fail closed: a room event needs room access; a room-less event needs org scope or the operator."""
    scope = event["visibility_scope"]
    if scope == "private":
        return viewer.operator
    if event["room_id"]:
        if viewer.external and not event["source"].startswith("room:"):
            return False
        return event["room_id"] in viewer.rooms
    return scope == "org" or viewer.operator


def authorized_events(events: list[dict], viewer: Viewer) -> list[dict]:
    """Visible events, newest ``MAX_EVENTS`` by cursor, ascending. Filtering happens before any aggregation."""
    kept = sorted((e for e in events if visible(e, viewer)), key=cursor_of)
    return kept[-MAX_EVENTS:]


# --------------------------------------------------------------------------------------------------- fold
def _ordered(events: list[dict]) -> list[dict]:
    """Dedupe by event_id, then order by (effective time, source, sequence): a source's own sequence is authoritative
    (its effective time never runs backwards), sources interleave by time. Deterministic for any input order."""
    unique: dict[str, dict] = {}
    for ev in events:
        known = unique.get(ev["event_id"])
        if known is None or _canon(ev) < _canon(known):
            unique[ev["event_id"]] = ev
    by_source: dict[str, list[dict]] = {}
    for ev in unique.values():
        by_source.setdefault(ev["source"], []).append(ev)
    keyed = []
    for source in sorted(by_source):
        floor = ""
        for ev in sorted(by_source[source], key=lambda e: (e["sequence"], e["event_id"])):
            floor = max(floor, ev["occurred_at"] or ev["ingested_at"])
            keyed.append(((floor, source, ev["sequence"], ev["event_id"]), ev))
    return [ev for _, ev in sorted(keyed, key=lambda item: item[0])]


def _canon(ev: dict) -> str:
    import json
    return json.dumps(ev, sort_keys=True, default=str)


class _Fold:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.redirect: dict[str, str] = {}
        self.rooms: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.tasks: dict[str, dict] = {}

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
                "label": None, "provenance": "unknown", "last_event_at": None}
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
            for dst in self.target(payload):
                self.edge("delegation", me, dst, _DELEGATION[kind], ev, ev["task_id"] or "")
                if kind == "delegation.accepted" and task is not None:
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

    def _agent_event(self, node: dict, ev: dict, kind: str, payload: dict, when: str) -> None:
        for field in ("host_id", "parent_session_id"):
            node[field] = ev[field] or node[field]
        node["provider"] = payload.get("provider") or node["provider"]
        if ev["sanitized_label"]:
            node["label"] = ev["sanitized_label"]
        if ev["provenance"] != "unknown" or node["provenance"] == "unknown":
            node["provenance"] = ev["provenance"]
        if kind in _STATES:
            node["state"] = _STATES[kind]
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
        elif kind == "room.joined" and ev["room_id"]:
            self._add(node["room_ids"], ev["room_id"])
        elif kind == "room.left" and ev["room_id"] and ev["room_id"] in node["room_ids"]:
            node["room_ids"].remove(ev["room_id"])
        if ev["room_id"] and kind not in ("room.left",):
            self._add(node["room_ids"], ev["room_id"])

    def result(self, cursor: str | None, event_count: int) -> dict:
        def fix(ref: str | None) -> str | None:
            seen = 0
            while ref in self.redirect and seen < 16:
                ref, seen = self.redirect[ref], seen + 1
            return ref

        agents = sorted(self.nodes.values(), key=lambda n: n["node_id"])[:MAX_AGENTS]
        rooms = []
        for room in sorted(self.rooms.values(), key=lambda r: r["room_id"])[:MAX_ROOMS]:
            rooms.append({"room_id": room["room_id"], "agent_ids": sorted({fix(a) for a in room["agent_ids"]})})
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
        return {"agents": copy.deepcopy(agents), "rooms": rooms, "edges": edges[:MAX_EDGES], "tasks": tasks,
                "cursor": cursor,
                "counts": {"agents": len(agents), "rooms": len(rooms), "edges": len(edges[:MAX_EDGES]),
                           "tasks": len(tasks), "events": event_count},
                "truncated": len(self.nodes) > MAX_AGENTS or len(self.edges) > MAX_EDGES
                or len(self.rooms) > MAX_ROOMS}


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

    def __init__(self, repo: Any, collab: Any = None, handoffs: Any = None, now: Any = None):
        self.repo, self.collab, self.handoffs = repo, collab, handoffs
        self._now = now or time.time

    def viewer(self, principal: Any = None, operator: bool = False) -> Viewer:
        rooms = frozenset(self.collab.readable_room_ids(principal)) if self.collab and principal is not None else frozenset()
        return Viewer(rooms=rooms, operator=operator, external=bool(getattr(principal, "external", False)))

    def _gather(self, viewer: Viewer, principal: Any) -> list[dict]:
        events = envelope_events(self.repo)
        if self.handoffs is not None:
            events += handoff_events(self.handoffs)
        if self.collab is not None and principal is not None:
            for room_id in sorted(viewer.rooms)[:MAX_ROOMS]:
                events += room_events(self.collab, principal, room_id)
        return authorized_events(events, viewer)

    def snapshot(self, viewer: Viewer, principal: Any = None) -> dict:
        return fold(self._gather(viewer, principal))

    def events(self, viewer: Viewer, principal: Any = None, cursor: str | None = None, limit: int = 100) -> dict:
        limit = max(1, min(limit, PAGE_MAX))
        ordered = [e for e in self._gather(viewer, principal) if cursor is None or cursor_of(e) > cursor]
        page = ordered[:limit]
        return {"events": page, "next_cursor": cursor_of(page[-1]) if page else cursor,
                "has_more": len(ordered) > limit}

    def replay(self, viewer: Viewer, principal: Any = None, until: str | None = None) -> dict:
        return fold(self._gather(viewer, principal), until)
