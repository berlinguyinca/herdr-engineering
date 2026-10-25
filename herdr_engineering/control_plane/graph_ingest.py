"""P2-P4 live-ingest wiring: translate lifecycle events into lineage envelopes.

This adapter routes existing mission/session/repo/trace lifecycle events into
the running lineage ``GraphProjection`` through the SAME, fully-tested ingest
path as any other lineage write: it builds a clean lineage envelope and calls
``repo.ingest`` (which runs ``repo.graph.admit`` under the ledger lock, persists
the row, and applies it via ``_apply_lineage``). The adapter is opt-in: the
server enables it by passing ``system_graph=True`` to ``IngestService``; when
disabled nothing changes and no lineage events are produced.

The adapter is read-only with respect to the source stores: it reads the
incoming envelope and emits lineage envelopes; it never mutates
missions/sessions/repos/traces.

Why route through ``repo.ingest`` instead of calling ``graph.admit/apply``
directly? The core path assigns the server-side ``event_id``/``source_sequence``
/``received_at``/``lineage_seq`` and persists the row for durable replay; a
direct ``apply`` with a fabricated stored-style dict would bypass the ledger and
never survive restart. Reusing ``repo.ingest`` keeps ordering, idempotency and
replay identical to any other lineage event.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .graph_mission import MissionGraphEmitter
from .graph_repo import RepositoryGraphEmitter
from .graph_trace import TraceGraphEmitter

log = logging.getLogger(__name__)

__all__ = ["GraphIngestAdapter"]


class GraphIngestAdapter:
    """Translate lifecycle envelopes into lineage envelopes fed to the graph.

    ``emit`` builds one lineage envelope per applicable entity and returns it
    (not yet ingested). ``ingest`` admits them all through ``repo.ingest`` and
    returns how many were ingested. A failure in graph admission (invalid for
    lineage, graph full) is logged and swallowed so it never breaks the source
    event's own ingestion.
    """

    def __init__(self, repo, *, actor: str = "key:system-graph",
                 scope: str = "org") -> None:
        self.repo = repo
        self.actor = actor
        self.scope = scope
        # Monotonic per-adapter sequence for the (actor, source) ledger domain.
        # The ledger keys events by (actor_id, source, source_sequence), so a
        # fresh 1..N per callback would collide on the second lifecycle event.
        self._seq = 0

    # -- envelope -> emitter record mapping --------------------------------
    def _records_for(self, entity_type: str, entity_id: str,
                     event_type: str, payload: dict) -> list[dict]:
        if entity_type == "mission":
            return [{"kind": "mission", "id": entity_id,
                     "state": self._mission_state(event_type, payload),
                     "stage": payload.get("stage"),
                     "progress": payload.get("progress"),
                     "blocked_by": payload.get("blocked_by")}]
        if entity_type == "session":
            return [{"kind": "session", "id": entity_id,
                     "mission_id": payload.get("mission_id"),
                     "state": self._session_state(event_type, payload)}]
        if entity_type == "repository":
            return [{"kind": "repository", "name": entity_id,
                     "depends_on": payload.get("depends_on") or (),
                     "supersedes": payload.get("supersedes")}]
        if entity_type == "request":
            return [{"kind": "request", "id": entity_id,
                     "label": payload.get("model"), "status": payload.get("status"),
                     "duration_ms": payload.get("duration_ms"),
                     "sampled": payload.get("sampled"),
                     "parent_id": payload.get("parent_request_id")}]
        if entity_type == "span":
            return [{"kind": "span", "id": entity_id,
                     "request_id": payload.get("request_id"),
                     "op": payload.get("op"),
                     "duration_ms": payload.get("duration_ms"),
                     "parent_span_id": payload.get("parent_span_id"),
                     "sampled": payload.get("sampled")}]
        return []

    @staticmethod
    def _mission_state(event_type: str, payload: dict) -> str | None:
        # Lifecycle event types are dotted (mission.created, mission.completed).
        # Map the terminal verb to a graph state when known; otherwise fall back
        # to an explicit payload state.
        verb = event_type.split(".")[-1].lower()
        mapping = {
            "created": None, "started": "running", "running": "running",
            "queued": "queued", "completed": "done", "done": "done",
            "failed": "failed", "cancelled": "cancelled", "canceled": "cancelled",
            "blocked": "blocked", "superseded": "superseded",
        }
        state = mapping.get(verb)
        if state is not None:
            return state
        return payload.get("state")

    @staticmethod
    def _session_state(event_type: str, payload: dict) -> str | None:
        if "stop" in event_type:
            return "stopped"
        if "start" in event_type:
            return "active"
        return payload.get("status")

    # -- emission ----------------------------------------------------------
    def _lineage_envelopes(self, entity_type: str, entity_id: str,
                           event_type: str, payload: dict) -> list[dict]:
        """Build clean lineage envelopes (one per lineage event) ready for
        ``repo.ingest``. Each has schema_version 2 and entity_type 'lineage'."""
        records = self._records_for(entity_type, entity_id, event_type, payload or {})
        if not records:
            return []
        # Collect the raw lineage ops (node.upsert / edge.add) with a fresh
        # per-emitter sequence so each envelope gets a unique source_sequence.
        raw: list[tuple[str, dict]] = []  # (event_type, payload)
        if entity_type in ("mission", "session"):
            em = MissionGraphEmitter(source="pi", actor=self.actor, scope=self.scope)
            for r in records:
                if r["kind"] == "mission":
                    for ev in em.mission_upsert(r["id"], state=r.get("state"),
                                                stage=r.get("stage"), progress=r.get("progress"),
                                                blocked_by=r.get("blocked_by")):
                        raw.append((ev["event_type"], ev["payload"]))
                else:
                    for ev in em.session(r["id"], mission_id=r.get("mission_id"),
                                         state=r.get("state")):
                        raw.append((ev["event_type"], ev["payload"]))
        elif entity_type == "repository":
            em = RepositoryGraphEmitter(source="git", actor=self.actor, scope=self.scope)
            for r in records:
                for ev in em.repo(r["name"], depends_on=r.get("depends_on", ()),
                                  supersedes=r.get("supersedes")):
                    raw.append((ev["event_type"], ev["payload"]))
        else:
            em = TraceGraphEmitter(source="inferweave", actor=self.actor, scope=self.scope)
            for r in records:
                if r["kind"] == "request":
                    for ev in em.request(r["id"], label=r.get("label"),
                                         status=r.get("status"), duration_ms=r.get("duration_ms"),
                                         sampled=r.get("sampled"), parent_id=r.get("parent_id")):
                        raw.append((ev["event_type"], ev["payload"]))
                else:
                    for ev in em.span(r["id"], request_id=r.get("request_id"),
                                      op=r.get("op"), duration_ms=r.get("duration_ms"),
                                      parent_span_id=r.get("parent_span_id"),
                                      sampled=r.get("sampled")):
                        raw.append((ev["event_type"], ev["payload"]))
        # Ensure edge endpoints exist: a non-operator writer can only create an
        # edge when both endpoint nodes are defined. Prepend a bare node.upsert
        # (an "ensure": no label/state/attrs, so it never claims ownership or
        # overrides real data) for each edge endpoint that is not already being
        # created by this batch.
        defined = {p.get("node_id") for e, p in raw if e == "node.upsert"}
        ensured: dict[str, tuple[str, str]] = {}  # node_id -> (node_type, event_type)
        for etype, payload in raw:
            if etype != "edge.add":
                continue
            for end in (payload.get("from_id"), payload.get("to_id")):
                if not end or end in defined or end in ensured:
                    continue
                ntype = end.split(":", 1)[0]
                ensured[end] = (ntype, "node.upsert")
        for node_id, (ntype, etype) in ensured.items():
            raw.insert(0, (etype, {"node_type": ntype, "node_id": node_id}))

        # Strip the stored-style `_envelope` metadata the emitters added; a live
        # envelope must carry only client fields (the server sets its own).
        envelopes = []
        for (etype, payload) in raw:
            clean_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
            self._seq += 1
            envelopes.append({
                "schema_version": 2,
                "event_id": f"system-graph-{int(time.time()*1000)}-{self._seq}",
                "source": "system-graph",
                "source_sequence": self._seq,
                "entity_type": "lineage",
                "entity_id": clean_payload.get("node_id")
                             or clean_payload.get("from_id"),
                "event_type": etype,
                "payload": clean_payload,
                "visibility_scope": self.scope,
                "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        return envelopes

    def emit(self, entity_type: str, entity_id: str,
             event_type: str, payload: dict) -> list[dict]:
        """Return lineage envelopes for an event (not ingested). Pure, testable."""
        return self._lineage_envelopes(entity_type, entity_id, event_type, payload or {})
