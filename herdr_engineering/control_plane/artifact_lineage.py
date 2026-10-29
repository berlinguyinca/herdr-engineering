"""Record a committed artifact version in the lineage graph.

The graph holds a stable reference and the immutable facts of the version (ids, number, content digests,
structured producer references), never blob locations, file names or content. Visibility: ``private`` unless the
caller passes ``scope`` (``artifact_scope.visibility_fields``); a wider label is only ever safe because the ACL read
gate (``artifact_scope.ArtifactGraphGate``) narrows it again at read time, so a revoke takes effect immediately.
Emission is best effort and never raises: the version is already durable when this runs.

Shape per commit: the logical node ``artifact:<id>`` (head facts), one immutable version node
``artifact:<id>@<version_id>`` and, when the version has a parent, a ``supersession`` edge new -> old. A correction is
therefore a new version; a retraction (``emit_retraction``) marks the retracted version node and leaves its facts readable
by the same audience.
"""
from __future__ import annotations

import re

from .artifact_scope import PRIVATE

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
# producer node kinds, most specific first; the first one the version names is the candidate `produced` source
_PRODUCERS = (("session_id", "session"), ("agent_id", "agent"), ("mission_id", "mission"))


def _digests(manifest) -> list[str]:
    found: set[str] = set()
    for entry in manifest if isinstance(manifest, list) else []:
        if not isinstance(entry, dict):
            continue
        for key in ("sha256", "blob_sha256"):
            value = entry.get(key)
            if isinstance(value, str) and _HEX64.match(value):
                found.add(value)
    return sorted(found)


def _envelope(source: str, n: int, event_type: str, entity_id: str, payload: dict, scope: dict) -> dict:
    return {"schema_version": 2, "event_id": f"{source}-{n}"[:128], "source": source, "source_sequence": n,
            "entity_type": "lineage", "entity_id": entity_id, "event_type": event_type, "payload": payload, **scope}


def emit_version_lineage(ingest, version: dict, principal: dict, *, scope: dict | None = None) -> dict:
    """Ingest ``artifact:<id>`` (commons node), the immutable ``artifact:<id>@<version_id>`` node, a ``supersession``
    edge to the parent version and, when the committing principal may write it, a ``produced`` edge from the producer.
    ``scope`` is the envelope visibility (``artifact_scope.visibility_fields``); if the ingest refuses it (for example
    the key may not address that room) the whole sequence is retried ``private``, never wider.
    Returns ``{"node", "version_node", "edge", "scope", "error"}``; never raises."""
    result = {"node": None, "version_node": None, "edge": None, "scope": None, "error": None}
    try:
        artifact_id, version_id = str(version["artifact_id"]), str(version["version_id"])
        node_id, version_node = f"artifact:{artifact_id}", f"artifact:{artifact_id}@{version_id}"
        facts = {"version_id": version_id, "version_number": version.get("version_number"),
                 "parent_version_id": version.get("parent_version_id"),
                 "content_digests": _digests(version.get("output_manifest")), "source": "artifact-registry"}
        for key in ("mission_id", "session_id", "agent_id", "job_id", "host_id"):
            if version.get(key):
                facts[key] = str(version[key])
        attrs = {k: v for k, v in facts.items() if v is not None}
        source = f"art-{version_id}"[:64]
        label = f"{artifact_id} v{version.get('version_number')}"
        candidates = [scope or PRIVATE] + ([] if (scope or PRIVATE) == PRIVATE else [PRIVATE])
        for index, chosen in enumerate(candidates):
            def send(n, event_type, entity_id, payload):
                return ingest.ingest(_envelope(source, n, event_type, entity_id, payload, chosen), principal)
            try:
                send(1, "node.upsert", node_id, {"node_type": "artifact", "node_id": node_id, "label": label,
                                                 "state": "ready", "attrs": attrs})
            except Exception:                          # noqa: BLE001 - refused scope: fall back to private
                if index + 1 == len(candidates):
                    raise
                continue
            result["node"], result["scope"] = node_id, dict(chosen)
            send(3, "node.upsert", version_node, {"node_type": "artifact", "node_id": version_node, "label": label,
                                                  "state": "ready", "attrs": attrs})
            result["version_node"] = version_node
            parent = version.get("parent_version_id")
            if parent:
                old = f"artifact:{artifact_id}@{parent}"
                try:
                    send(4, "edge.add", version_node, {"edge_type": "supersession", "from_id": version_node,
                                                       "to_id": old})
                except Exception:                      # noqa: BLE001 - the parent was never recorded: no edge
                    pass
            for field, kind in _PRODUCERS:
                if version.get(field):
                    producer = f"{kind}:{version[field]}"
                    try:
                        send(2, "edge.add", producer, {"edge_type": "produced", "from_id": producer,
                                                       "to_id": node_id})
                        result["edge"] = producer
                    except Exception:                  # noqa: BLE001 - someone else owns the producer: node stands
                        pass
                    break
            break
    except Exception as exc:                           # noqa: BLE001 - the version is already committed
        result["error"] = str(exc) or type(exc).__name__
    return result


def emit_retraction(ingest, artifact_id: str, version_id: str, principal: dict, reason: str, *,
                    scope: dict | None = None, head: bool = False) -> dict:
    """Record that a version was retracted. The version node keeps its facts and stays readable by its audience (state
    ``retracted`` plus a bounded reason); ``head=True`` also marks the logical node. Nothing is deleted, and the scope is
    the one the version was committed with (callers pass the same ``scope``); never raises."""
    result = {"node": None, "error": None}
    try:
        node_id = f"artifact:{artifact_id}@{version_id}"
        source = f"artret-{version_id}"[:64]
        chosen, why = scope or PRIVATE, " ".join(str(reason or "").split())[:200] or "retracted"
        ingest.ingest(_envelope(source, 1, "node.state", node_id, {"node_id": node_id, "state": "retracted",
                                                                   "reason": why}, chosen), principal)
        result["node"] = node_id
        if head:
            logical = f"artifact:{artifact_id}"
            ingest.ingest(_envelope(source, 2, "node.state", logical, {"node_id": logical, "state": "retracted",
                                                                       "reason": why}, chosen), principal)
    except Exception as exc:                           # noqa: BLE001
        result["error"] = str(exc) or type(exc).__name__
    return result


def lineage_emitter(ingest, principal: dict, *, scope: dict | None = None):
    """``fn(version) -> result`` for ``ArtifactRenderRunner(..., lineage=...)``: the render path then records the same
    lineage as the HTTP commit route (same function, same shape)."""
    return lambda version: emit_version_lineage(ingest, version, principal, scope=scope)
