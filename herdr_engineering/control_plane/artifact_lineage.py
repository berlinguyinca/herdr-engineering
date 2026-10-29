"""Record a committed artifact version in the lineage graph.

The graph holds a stable reference and the immutable facts of the version (ids, number, content digests,
structured producer references), never blob locations, file names or content. Visibility is ``private``:
the artifact's own ACL governs who may open it, and the graph must not widen that. Emission is best effort
and never raises: the version is already durable when this runs.
"""
from __future__ import annotations

import re

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


def emit_version_lineage(ingest, version: dict, principal: dict) -> dict:
    """Ingest ``artifact:<id>`` (commons node) and, when the committing principal may write it, a ``produced``
    edge from the producer. Returns ``{"node", "edge", "error"}``; never raises."""
    result = {"node": None, "edge": None, "error": None}
    try:
        artifact_id, version_id = str(version["artifact_id"]), str(version["version_id"])
        node_id = f"artifact:{artifact_id}"
        attrs = {"version_id": version_id, "version_number": version.get("version_number"),
                 "parent_version_id": version.get("parent_version_id"),
                 "content_digests": _digests(version.get("output_manifest")), "source": "artifact-registry"}
        for key in ("mission_id", "session_id", "agent_id", "job_id", "host_id"):
            if version.get(key):
                attrs[key] = str(version[key])
        attrs = {k: v for k, v in attrs.items() if v is not None}
        source = f"art-{version_id}"[:64]

        def send(n: int, event_type: str, entity_id: str, payload: dict) -> dict:
            return ingest.ingest({
                "schema_version": 2, "event_id": f"art-{version_id}-{n}"[:128], "source": source,
                "source_sequence": n, "entity_type": "lineage", "entity_id": entity_id,
                "event_type": event_type, "payload": payload, "visibility_scope": "private"}, principal)

        send(1, "node.upsert", node_id, {"node_type": "artifact", "node_id": node_id,
                                         "label": f"{artifact_id} v{version.get('version_number')}",
                                         "state": "ready", "attrs": attrs})
        result["node"] = node_id
        for field, kind in _PRODUCERS:
            if version.get(field):
                producer = f"{kind}:{version[field]}"
                try:
                    send(2, "edge.add", producer, {"edge_type": "produced", "from_id": producer, "to_id": node_id})
                    result["edge"] = producer
                except Exception:                      # noqa: BLE001 - someone else owns the producer: node stands
                    pass
                break
    except Exception as exc:                           # noqa: BLE001 - the version is already committed
        result["error"] = str(exc) or type(exc).__name__
    return result
