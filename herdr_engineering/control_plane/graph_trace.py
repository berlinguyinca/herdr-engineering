"""P4 request/trace slice: emit request+span lineage events.

EMITTER over the existing lineage ``GraphProjection`` (no own store). Maps
InferWeave request/span rows onto the shared node/edge kinds (``request``,
``span`` nodes; ``calls``, ``observed_in`` edges) and produces lineage events
applied via ``GraphProjection.apply``.

Honesty rule (spec 02): sampling and unknown parents are surfaced as attrs,
never fabricated. A span without a recorded parent gets no ``calls`` edge; a
sampled-out request is labeled ``sampled`` rather than pretending to be whole.

Mapping:
  request -> node ``request``   (label = model/route; attrs: status, duration_ms)
  span    -> node ``span``      (attrs: op, duration_ms, sampled)
  parent  -> edge ``calls``     (span/request -> span)
  trace   -> edge ``observed_in`` (request -> request or span)
"""
from __future__ import annotations

from typing import Any, Iterable

from .graph import from_stored

__all__ = ["from_inferweave", "TraceGraphEmitter"]


class TraceGraphEmitter:
    """Emit request/span lineage events."""

    def __init__(self, *, source: str = "inferweave", actor: str = "key:system-graph",
                 scope: str = "org", seq: int = 0) -> None:
        self.source = source
        self.actor = actor
        self.scope = scope
        self.seq = seq

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def _stored(self, etype: str, payload: dict) -> dict:
        payload = dict(payload)
        payload["_envelope"] = {"lineage_mode": "normal"}
        return from_stored({
            "event_type": etype, "payload": payload, "visibility_scope": self.scope,
            "source": self.source, "source_sequence": self._next(),
            "actor": {"id": self.actor, "kind": "agent", "authenticated_by": "api_key"},
            "event_id": f"{self.source}-{self.seq}", "occurred_at": "2026-10-22T10:00:00Z",
        })

    def request(self, request_id: str, *, label: str | None = None,
                status: str | None = None, duration_ms: int | None = None,
                sampled: bool | None = None, parent_id: str | None = None) -> list[dict]:
        attrs: dict = {}
        if status:
            attrs["status"] = status
        if duration_ms is not None:
            attrs["duration_ms"] = duration_ms
        if sampled is not None:
            attrs["sampled"] = sampled
        payload: dict = {"node_type": "request", "node_id": f"request:{request_id}"}
        if label:
            payload["label"] = label
        if attrs:
            payload["attrs"] = attrs
        events = [self._stored("node.upsert", payload)]
        if parent_id:
            events.append(self._stored(
                "edge.add", {"edge_type": "calls",
                             "from_id": f"request:{parent_id}",
                             "to_id": f"request:{request_id}"}))
        return events

    def span(self, span_id: str, *, request_id: str | None = None,
             op: str | None = None, duration_ms: int | None = None,
             parent_span_id: str | None = None, sampled: bool | None = None) -> list[dict]:
        """Emit a span node; ``observed_in`` links it to its request trace ONLY
        when a request id is present (honesty: never fabricate ``request:None``).
        """
        attrs: dict = {}
        if op:
            attrs["op"] = op
        if duration_ms is not None:
            attrs["duration_ms"] = duration_ms
        if sampled is not None:
            attrs["sampled"] = sampled
        if request_id is None:
            attrs["orphan"] = True  # surfaced, never fabricated
        payload: dict = {"node_type": "span", "node_id": f"span:{span_id}"}
        if attrs:
            payload["attrs"] = attrs
        events = [self._stored("node.upsert", payload)]
        # observed_in: span belongs to its request trace (only when known)
        if request_id:
            events.append(self._stored(
                "edge.add", {"edge_type": "observed_in",
                             "from_id": f"span:{span_id}",
                             "to_id": f"request:{request_id}"}))
        # only a recorded parent yields a calls edge (honesty: no fabrication)
        if parent_span_id:
            events.append(self._stored(
                "edge.add", {"edge_type": "calls",
                             "from_id": f"span:{parent_span_id}",
                             "to_id": f"span:{span_id}"}))
        return events

    @staticmethod
    def apply_all(graph: Any, events: Iterable[dict]) -> None:
        for ev in events:
            graph.apply(ev)


def from_inferweave(row: dict) -> dict:
    """Map one InferWeave request/span row to a TraceGraphEmitter record.

    Expected row keys (subset of iw-protocol request/span fields):
      request_id, span_id, op, duration_ms, parent_span_id, sampled, status,
      model.
    Returns {"kind": "request"|"span", ...} ready for ``trace_lineage_events``.
    """
    if row.get("span_id"):
        return {"kind": "span", "id": row["span_id"], "request_id": row.get("request_id"),
                "op": row.get("op"), "duration_ms": row.get("duration_ms"),
                "parent_span_id": row.get("parent_span_id"), "sampled": row.get("sampled")}
    return {"kind": "request", "id": row.get("request_id") or row.get("request"),
            "label": row.get("model"), "status": row.get("status"),
            "duration_ms": row.get("duration_ms"), "sampled": row.get("sampled"),
            "parent_id": row.get("parent_request_id")}


def trace_lineage_events(emitter: TraceGraphEmitter, records: Iterable[dict]) -> list[dict]:
    """Expand trace records (as produced by ``from_inferweave``) into events."""
    events: list[dict] = []
    for rec in records:
        if rec["kind"] == "request":
            events += emitter.request(rec["id"], label=rec.get("label"),
                                      status=rec.get("status"),
                                      duration_ms=rec.get("duration_ms"),
                                      sampled=rec.get("sampled"),
                                      parent_id=rec.get("parent_id"))
        else:
            events += emitter.span(rec["id"], request_id=rec["request_id"],
                                   op=rec.get("op"), duration_ms=rec.get("duration_ms"),
                                   parent_span_id=rec.get("parent_span_id"),
                                   sampled=rec.get("sampled"))
    return events
