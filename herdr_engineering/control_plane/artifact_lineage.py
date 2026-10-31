"""Record a committed artifact version in the lineage graph.

The graph holds a stable reference and the immutable facts of the version (ids, number, content digests,
structured producer references), never blob locations, file names or content. Visibility: ``private`` unless the
caller passes ``scope`` (``artifact_scope.visibility_fields``); a wider label is only ever safe because the ACL read
gate (``artifact_scope.ArtifactGraphGate``) narrows it again at read time, so a revoke takes effect immediately.
Emission is best effort and never raises: the version is already durable when this runs.

Shape per commit: the logical node ``artifact:<id>`` (head facts), one immutable version node
``artifact:<id>@<version_id>`` and a ``supersession`` edge new -> old for every parent (a merge has two). A correction is
therefore a new version; a retraction (``emit_retraction``) marks the retracted version node and leaves its facts readable
by the same audience.
"""
from __future__ import annotations

import hashlib
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


def source_name(prefix: str, key: str) -> str:
    """A valid ingest source (``[a-z][a-z0-9_-]{0,31}``) that is a pure function of ``key``. Real registry ids are longer
    than a source may be, and a source that changes per version would hit the graph's cap on distinct sources one writer
    may use per node (4), so a node that every version touches gets ONE stable source and distinct sequences instead."""
    return f"{prefix}-{hashlib.sha256(str(key).encode()).hexdigest()[:20]}"


def sequence_of(version: dict) -> int:
    """Strictly per-version, deterministic, positive: the version number when it is one, else a digest of the id."""
    n = version.get("version_number")
    if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 10**9:
        return n
    return int(hashlib.sha256(str(version.get("version_id")).encode()).hexdigest()[:9], 16) + 10**9


def _envelope(source: str, n: int, event_type: str, entity_id: str, payload: dict, scope: dict) -> dict:
    return {"schema_version": 2, "event_id": f"{source}-{n}"[:128], "source": source, "source_sequence": n,
            "entity_type": "lineage", "entity_id": entity_id, "event_type": event_type, "payload": payload, **scope}


def _parents(version: dict) -> list[str]:
    """Ordered, distinct parent ids: ``parent_version_ids`` when present, else the single ``parent_version_id``."""
    listed = version.get("parent_version_ids")
    if not isinstance(listed, (list, tuple)) or not listed:
        listed = [version.get("parent_version_id")]
    out: list[str] = []
    for parent in listed:
        if parent and str(parent) not in out:
            out.append(str(parent))
    return out


def emit_version_lineage(ingest, version: dict, principal: dict, *, scope: dict | None = None,
                         source_prefix: str = "art") -> dict:
    """Ingest ``artifact:<id>`` (commons node), the immutable ``artifact:<id>@<version_id>`` node, a ``supersession``
    edge to each parent version and, when the committing principal may write it, a ``produced`` edge from the producer.
    ``scope`` is the envelope visibility (``artifact_scope.visibility_fields``); if the ingest refuses it (for example
    the key may not address that room) the whole sequence is retried ``private``, never wider.
    ``source_prefix`` names the event sources (see ``source_name``): the default is the commit's own, a different one
    (``artbf`` for the backfill) is a new source, so the ingest does not deduplicate it away as a replay.
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
        source = source_name(source_prefix, artifact_id)            # the logical node: one source, a sequence per version
        vsource = source_name(source_prefix + "v", version_id)       # objects that belong to this version alone
        seq = sequence_of(version) * 10
        label = f"{artifact_id} v{version.get('version_number')}"
        candidates = [scope or PRIVATE] + ([] if (scope or PRIVATE) == PRIVATE else [PRIVATE])
        for index, chosen in enumerate(candidates):
            def send(n, event_type, entity_id, payload, src=vsource):
                return ingest.ingest(_envelope(src, n, event_type, entity_id, payload, chosen), principal)
            try:
                send(seq + 1, "node.upsert", node_id, {"node_type": "artifact", "node_id": node_id, "label": label,
                                                 "state": "ready", "attrs": attrs}, source)
            except Exception:                          # noqa: BLE001 - refused scope: fall back to private
                if index + 1 == len(candidates):
                    raise
                continue
            result["node"], result["scope"] = node_id, dict(chosen)
            send(1, "node.upsert", version_node, {"node_type": "artifact", "node_id": version_node, "label": label,
                                                  "state": "ready", "attrs": attrs})
            result["version_node"] = version_node
            # one edge per parent (a merge has two). The first parent keeps sequence 2 so replays of versions
            # recorded before parent lists existed stay duplicates; later parents take 4, 5, ... (3 is `produced`).
            for index, parent in enumerate(_parents(version)):
                old = f"artifact:{artifact_id}@{parent}"
                try:
                    send(2 if index == 0 else 3 + index, "edge.add", version_node,
                         {"edge_type": "supersession", "from_id": version_node, "to_id": old})
                except Exception:                      # noqa: BLE001 - the parent was never recorded: no edge
                    pass
            for field, kind in _PRODUCERS:
                if version.get(field):
                    producer = f"{kind}:{version[field]}"
                    try:
                        send(3, "edge.add", producer, {"edge_type": "produced", "from_id": producer,
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
                    scope: dict, head: bool = False, graph=None) -> dict:
    """Record that a version was retracted. The version node keeps its facts and stays readable by its audience (state
    ``retracted`` plus a bounded reason); ``head=True`` also marks the logical node. Nothing is deleted, and ``scope`` is required: pass the one the
    version was committed with (a default could silently hide the retraction). ``graph`` (optional) verifies the state took;
    never raises."""
    result = {"node": None, "error": None}
    try:
        node_id = f"artifact:{artifact_id}@{version_id}"
        source = source_name("artret", f"{artifact_id}@{version_id}")
        chosen, why = scope, " ".join(str(reason or "").split())[:200] or "retracted"
        ingest.ingest(_envelope(source, 1, "node.state", node_id, {"node_id": node_id, "state": "retracted",
                                                                   "reason": why}, chosen), principal)
        result["node"] = node_id
        if graph is not None:
            # a node's primary state is its first state-writer's: another writer's retraction is only a contribution
            seen = graph.get_node(principal, node_id)["node"]["state"]
            if seen != "retracted":
                result["error"] = f"retraction recorded but the node still reads {seen!r}: only the committing writer can retract"
        if head:
            logical = f"artifact:{artifact_id}"
            ingest.ingest(_envelope(source_name("artret", artifact_id), sequence_of({"version_id": version_id}), "node.state",
                                    logical, {"node_id": logical, "state": "retracted", "reason": why}, chosen), principal)
    except Exception as exc:                           # noqa: BLE001
        result["error"] = str(exc) or type(exc).__name__
    return result


def lineage_emitter(ingest, principal: dict, *, scope: dict | None = None):
    """``fn(version) -> result`` for ``ArtifactRenderRunner(..., lineage=...)``: the render path then records the same
    lineage as the HTTP commit route (same function, same shape)."""
    return lambda version: emit_version_lineage(ingest, version, principal, scope=scope)


_POSTER_KEYS = frozenset({"api_url", "key_file", "timeout_seconds"})


def _poster_settings(section, *, where: str) -> tuple[str, str, float] | None:
    """Validate a YAML poster section (``api_url``, ``key_file``, optional ``timeout_seconds``) and return
    ``(url, key, timeout)``, or None when the section is absent. ``where`` names the section in error messages. The key
    is read from the FILE (never from the environment, never inlined in YAML) and never appears in an error."""
    if not section:
        return None
    from pathlib import Path
    from urllib.parse import urlsplit
    if not isinstance(section, dict) or set(section) - _POSTER_KEYS:
        raise ValueError(f"{where}: allowed keys are {sorted(_POSTER_KEYS)}")
    url, key_file = section.get("api_url"), section.get("key_file")
    if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"} or not isinstance(key_file, str):
        raise ValueError(f"{where} needs an http(s) api_url and a key_file path")
    timeout = section.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        raise ValueError(f"{where}.timeout_seconds must be a number in (0, 120]")
    key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"{where}.key_file is empty")
    return url, key, timeout


def _post_json(url: str, key: str, timeout: float, body: dict):
    import json
    import urllib.request
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:     # noqa: S310 - scheme checked by the caller
        return json.loads(response.read())


_POSTER_WHERE = "artifact_worker.lineage"


def lineage_poster_from_section(section):
    """Build the render worker's lineage hook from YAML ``control_plane.artifact_worker.lineage`` (``api_url``, ``key_file``,
    optional ``timeout_seconds``), or None when the section is absent. The key is read from the FILE at start-up (never from
    the environment, never inlined in YAML). The hook POSTs ``{"version_id"}`` to the control plane's authenticated
    ``/api/v1/artifact-jobs/<job>/lineage`` route: the server re-reads the version from the registry and applies the ACL
    write gate, so the worker has no graph channel of its own and cannot assert facts."""
    settings = _poster_settings(section, where=_POSTER_WHERE)
    if settings is None:
        return None
    from urllib.parse import quote
    url, key, timeout = settings

    def post(version: dict):
        job_id, version_id = version.get("job_id"), version.get("version_id")
        if not job_id or not version_id:
            return {"skipped": "version has no job_id/version_id"}
        return _post_json(f"{url.rstrip('/')}/api/v1/artifact-jobs/{quote(str(job_id), safe='')}/lineage", key, timeout,
                          {"version_id": version_id})
    return post


def collab_lineage_poster_from_section(section):
    """Build the collaborator applier's ``lineage`` hook from the same YAML section as the render worker's
    (``control_plane.artifact_worker.lineage``), or None when it is absent; the same strict validation. The hook takes
    the version dict ``merge_commit`` returns and POSTs ``{}`` to the authenticated
    ``/api/v1/artifacts/<id>/versions/<vid>/collab-lineage`` route, which re-reads the version from the registry."""
    settings = _poster_settings(section, where=_POSTER_WHERE)
    if settings is None:
        return None
    from urllib.parse import quote
    url, key, timeout = settings

    def post(version: dict):
        artifact_id, version_id = version.get("artifact_id"), version.get("version_id")
        if not artifact_id or not version_id:
            return {"skipped": "version has no artifact_id/version_id"}
        return _post_json(f"{url.rstrip('/')}/api/v1/artifacts/{quote(str(artifact_id), safe='')}/versions/"
                          f"{quote(str(version_id), safe='')}/collab-lineage", key, timeout, {})
    return post
