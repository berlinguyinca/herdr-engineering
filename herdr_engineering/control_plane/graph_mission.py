"""P2 mission slice: translate mission/session lifecycle into lineage events.

This is an EMITTER over the existing lineage ``GraphProjection`` (graph.py) —
it does not own a store. It maps mission/session state onto the shared
node/edge kinds and produces lineage event dicts that are applied through
``GraphProjection.apply`` (or admitted via ``admit`` on live ingest). The
projection remains the single source of truth; nothing here mutates missions.

Mapping (spec 02 sample chain):
  mission   -> node kind ``mission`` (state, stage, progress as attrs)
  session   -> node kind ``session``
  handoff   -> edge kind ``handoff``   (mission -> mission)
  spawn     -> edge kind ``spawn``     (mission -> session)
  blocked   -> edge kind ``blocked_by`` (mission -> mission)
"""
from __future__ import annotations

from typing import Any, Iterable

from .graph import from_stored

__all__ = ["mission_lineage_events", "MissionGraphEmitter"]


def _stored(etype: str, payload: dict, *, scope: str, source: str, seq: int,
            actor: str, mission_id: str | None = None, room_id: str | None = None,
            eid: str | None = None) -> dict:
    """Build a stored-style lineage event dict compatible with apply()."""
    payload = dict(payload)
    payload["_envelope"] = {"lineage_mode": "normal"}
    ev = {
        "event_type": etype, "payload": payload, "visibility_scope": scope,
        "source": source, "source_sequence": seq,
        "actor": {"id": actor, "kind": "agent", "authenticated_by": "api_key"},
        "event_id": eid or f"{source}-{seq}",
        "occurred_at": "2026-10-22T10:00:00Z",
    }
    if mission_id is not None:
        ev["mission_id"] = mission_id
    if room_id is not None:
        ev["room_id"] = room_id
    return from_stored(ev)


class MissionGraphEmitter:
    """Emit lineage events for mission/session lifecycle changes.

    ``emit_*`` methods return event dicts ready for ``GraphProjection.apply``.
    ``apply_all`` applies a list of events to a projection (used for replay /
    offline reconnect). This is read-only w.r.t. missions: the projection is
    derived from the same event source, never written back.
    """

    def __init__(self, *, source: str = "pi", actor: str = "key:system-graph",
                 scope: str = "org", seq: int = 0) -> None:
        self.source = source
        self.actor = actor
        self.scope = scope
        self.seq = seq

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def mission_upsert(self, mission_id: str, *, state: str | None = None,
                       stage: str | None = None, progress: Any = None,
                       blocked_by: str | None = None) -> list[dict]:
        """Emit a mission node upsert (+ optional blocked_by edge)."""
        node_id = f"mission:{mission_id}"
        payload: dict = {"node_type": "mission", "node_id": node_id}
        if state:
            payload["state"] = state
        attrs: dict = {}
        if stage:
            attrs["stage"] = stage
        if progress is not None:
            attrs["progress"] = progress
        if attrs:
            payload["attrs"] = attrs
        events = [_stored("node.upsert", payload, scope=self.scope, source=self.source,
                          seq=self._next(), actor=self.actor, mission_id=mission_id)]
        if blocked_by:
            events.append(_stored(
                "edge.add", {"edge_type": "blocked_by", "from_id": node_id,
                             "to_id": f"mission:{blocked_by}"},
                scope=self.scope, source=self.source, seq=self._next(), actor=self.actor,
                mission_id=mission_id))
        return events

    def handoff(self, from_mission: str, to_mission: str) -> dict:
        return _stored("edge.add", {"edge_type": "handoff",
                                    "from_id": f"mission:{from_mission}",
                                    "to_id": f"mission:{to_mission}"},
                       scope=self.scope, source=self.source, seq=self._next(),
                       actor=self.actor, mission_id=from_mission)

    def session(self, session_id: str, *, mission_id: str | None = None,
                state: str | None = None) -> list[dict]:
        node_id = f"session:{session_id}"
        payload: dict = {"node_type": "session", "node_id": node_id}
        if state:
            payload["state"] = state
        events = [_stored("node.upsert", payload, scope=self.scope, source=self.source,
                          seq=self._next(), actor=self.actor, mission_id=mission_id)]
        if mission_id:
            events.append(_stored(
                "edge.add", {"edge_type": "spawn", "from_id": f"mission:{mission_id}",
                             "to_id": node_id},
                scope=self.scope, source=self.source, seq=self._next(), actor=self.actor,
                mission_id=mission_id))
        return events

    @staticmethod
    def apply_all(graph: Any, events: Iterable[dict]) -> None:
        """Apply a batch of events to a projection (idempotent replay)."""
        for ev in events:
            graph.apply(ev)


def mission_lineage_events(emitter: MissionGraphEmitter, records: Iterable[dict]) -> list[dict]:
    """Convenience: expand mission/session record dicts into lineage events.

    Each record: {"kind": "mission"|"session", "id": str, "state": str,
                  "stage": str|None, "progress": any, "blocked_by": str|None,
                  "mission_id": str|None}.
    """
    events: list[dict] = []
    for rec in records:
        if rec["kind"] == "mission":
            events += emitter.mission_upsert(
                rec["id"], state=rec.get("state"), stage=rec.get("stage"),
                progress=rec.get("progress"), blocked_by=rec.get("blocked_by"))
        else:
            events += emitter.session(rec["id"], mission_id=rec.get("mission_id"),
                                      state=rec.get("state"))
    return events
