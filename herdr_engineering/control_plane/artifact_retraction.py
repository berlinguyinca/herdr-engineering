"""Retract / re-scope an artifact in the lineage graph (the domain half of the HTTP routes in ``api.py``).

``retract``: hide the artifact (or one version) from EVERY graph reader, durably. The hide is the read gate's retraction
set (``artifact_scope.ArtifactGraphGate``); the registry persists it (``ArtifactStore.record_retraction``) and reloads it at
startup, so it holds across restarts and replays. A bounded state marker is also written to the graph as an audit trail
(best effort: the hide never depends on it). Nothing is deleted: the stored version, its blobs and the event log stay.

``backfill``: nodes created before artifact scoping were stamped ``private`` (their committer only). Re-emitting each stored
version under the current gated scope and a NEW event source (``artbf-<version_id>``) widens the label to what the gate
narrows back down to the ACL, so ACL readers see the history. Retracted versions are skipped. Idempotent: the same source
and sequence numbers dedupe a second run.
"""
from __future__ import annotations

from .artifact_lineage import _envelope, emit_version_lineage, source_name
from .artifact_scope import visibility_fields

MAX_REASON = 200


def _marker(ingest, principal: dict, node_id: str, source: str, reason: str, scope: dict) -> str | None:
    try:
        ingest.ingest(_envelope(source, 1, "node.state", node_id,
                                {"node_id": node_id, "state": "retracted", "reason": reason}, scope), principal)
        return None
    except Exception as exc:                                # noqa: BLE001 - the hide does not depend on the marker
        return str(exc) or type(exc).__name__


def retract(*, gate, ingest, principal: dict, artifact_id: str, version_id: str | None, reason: str,
            persist=None) -> dict:
    """Hide first (fail closed), then persist, then mark. Returns ``{"hidden", "persisted", "marker_error", "error"}``."""
    why = " ".join(str(reason or "").split())[:MAX_REASON] or "retracted"
    gate.retract(artifact_id, version_id)
    out = {"artifact_id": artifact_id, "version_id": version_id, "hidden": True, "persisted": False,
           "marker_error": None, "error": None}
    if persist is not None:
        try:
            persist(artifact_id, version_id, why)
            out["persisted"] = True
        except Exception as exc:                            # noqa: BLE001 - stays hidden in memory; say it is not durable
            out["error"] = f"hidden, but the retraction was not persisted: {str(exc) or type(exc).__name__}"
    scope = visibility_fields(gated=True)
    node = f"artifact:{artifact_id}@{version_id}" if version_id else f"artifact:{artifact_id}"
    out["marker_error"] = _marker(ingest, principal, node, source_name("artret", node.removeprefix("artifact:")),
                                  why, scope)
    return out


def backfill(*, gate, ingest, principal: dict, versions: list[dict], artifact_id: str) -> dict:
    """Re-emit ``versions`` (stored rows, any order) oldest first. Skips retracted versions and retracted artifacts."""
    out = {"artifact_id": artifact_id, "emitted": 0, "skipped_retracted": 0, "errors": []}
    if gate.artifact_retracted(artifact_id):
        out["skipped_retracted"] = len(versions)
        return out
    scope = visibility_fields(gated=True)
    for version in sorted(versions, key=lambda v: v.get("version_number") or 0):
        vid = str(version.get("version_id"))
        if gate.version_retracted(artifact_id, vid):
            out["skipped_retracted"] += 1
            continue
        res = emit_version_lineage(ingest, {**version, "artifact_id": artifact_id}, principal, scope=scope,
                                   source_prefix="artbf")
        if res["error"]:
            out["errors"].append({"version_id": vid, "error": res["error"]})
        else:
            out["emitted"] += 1
    return out
