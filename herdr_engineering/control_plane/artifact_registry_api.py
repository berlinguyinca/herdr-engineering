"""Registry-extension routes: context lookup, links, resumable uploads, lifecycle, reconcile.

Dispatched from ``_route_api`` right after the report routes. Every artifact-scoped route authorizes against the durable
registry (``report_permission``: owner or unexpired grant) and answers an unauthorized caller exactly like a missing
artifact (404). Identity is always derived from authentication, never from the body.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .artifact_report_api import LimitedInput, _access
from .artifact_uploads import MAX_PART_BYTES

_LINK_FIELDS = {"task_id", "project_id", "issue_ref", "room_id", "link_kind", "version_id", "source_event_id"}
_UPLOAD_FIELDS = {"filename", "media_type", "size_bytes", "sha256", "part_size"}
_LIFECYCLE_FIELDS = {"reason", "request_key", "legal_hold"}
_NUMBER = re.compile(r"^[0-9]{1,6}$")


def _json_body(handler, allowed):
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("this route requires application/json")
    value = handler._read_json() or {}
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("invalid request fields; identity is derived from authentication")
    return value


def _query(handler):
    return {key: values[0] for key, values in parse_qs(urlsplit(handler.path).query).items()}


def route_registry(handler, server, parts, resource):
    """Return true after handling a registry-extension route, else leave the request to the legacy routes."""
    if resource != "artifacts" or len(parts) < 5:
        return False
    method = handler.command
    lookup = len(parts) == 7 and parts[4] == "for" and method == "GET"
    reconcile = len(parts) == 5 and parts[4] == "reconcile" and method == "POST"
    scoped = len(parts) >= 6 and parts[4] not in {"for", "reconcile"}
    action = parts[5] if scoped else None
    links = scoped and len(parts) == 6 and action == "links" and method in {"GET", "POST"}
    lifecycle = (scoped and len(parts) == 6 and method == "POST"
                 and action in {"archive", "restore", "tombstone"})
    history = scoped and len(parts) == 6 and action == "lifecycle" and method == "GET"
    uploads = scoped and action == "uploads" and (
        (len(parts) == 6 and method == "POST")
        or (len(parts) == 7 and method == "GET")
        or (len(parts) == 8 and method == "POST" and parts[7] in {"complete", "abort"})
        or (len(parts) == 9 and method == "POST" and parts[7] == "parts" and _NUMBER.match(parts[8])))
    if not any((lookup, reconcile, links, lifecycle, history, uploads)):
        return False
    if server.artifact_store is None:
        handler._json({"error": "versioned artifact registry is unavailable"}, 503)
        return True
    origin = handler.headers.get("Origin")
    if method == "POST" and origin:
        from urllib.parse import urlsplit as _split
        if _split(origin).netloc != handler.headers.get("Host") or _split(origin).scheme not in {"http", "https"}:
            handler._deny(403, "cross-origin artifact mutation refused")
            return True
    principal = handler._resource_principal()

    if lookup:
        query = _query(handler)
        try:
            limit = int(query.get("limit", "50"))
        except ValueError:
            raise ValueError("limit must be an integer") from None
        page = handler._artifact_call(
            "list_by_context", principal_id=principal, kind=parts[5], ref=parts[6], query=query.get("q"),
            media_type=query.get("media_type"), limit=limit, cursor=query.get("cursor"))
        handler._json(page)
        return True

    if reconcile:
        handler._read_json()
        from .artifact_lineage import emit_version_lineage
        ingest = server.ingest
        agent = handler._principal()
        report = handler._artifact_call(
            "reconcile", emit=(lambda version: emit_version_lineage(ingest, version, agent)))
        handler._json(report)
        return True

    artifact_id = parts[4]
    needed = ("read" if (links and method == "GET") else
              "admin" if (lifecycle or history) else "edit")
    if not _access(handler, artifact_id, needed):
        handler._deny(404, "not found")
        return True

    if links:
        if method == "GET":
            handler._json({"artifact_id": artifact_id, "links": handler._artifact_call("list_links", artifact_id)})
        else:
            body = _json_body(handler, _LINK_FIELDS)
            result = handler._artifact_call("link_context", artifact_id, created_by=principal, **body)
            handler._json({"artifact_id": artifact_id, **result}, 201 if result["linked"] else 200)
        return True

    if history:
        handler._json({"artifact_id": artifact_id,
                       "events": handler._artifact_call("list_lifecycle_events", artifact_id)})
        return True

    if lifecycle:
        body = _json_body(handler, _LIFECYCLE_FIELDS)
        kwargs = {"actor": principal, "reason": body.get("reason") or "", "request_key": body.get("request_key")}
        if action == "tombstone":
            kwargs["legal_hold"] = bool(body.get("legal_hold", False))
        handler._json(handler._artifact_call(f"{action}_artifact", artifact_id, **kwargs))
        return True

    # resumable uploads
    if len(parts) == 6:
        body = _json_body(handler, _UPLOAD_FIELDS)
        created = handler._artifact_call("create_upload", artifact_id, principal_id=principal,
                                         idempotency_key=handler.headers.get("Idempotency-Key"), **body)
        handler._json(created, 201)
    elif len(parts) == 7:
        status = handler._artifact_call("get_upload", parts[6], artifact_id)
        if status is None:
            handler._json({"error": "not found"}, 404)
        else:
            handler._json(status)
    elif parts[7] == "complete":
        handler._read_json()
        handler._json(handler._artifact_call("complete_upload", parts[6], artifact_id))
    elif parts[7] == "abort":
        handler._read_json()
        handler._json(handler._artifact_call("abort_upload", parts[6], artifact_id))
    else:
        lengths = handler.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]) or handler.headers.get("Transfer-Encoding"):
            handler._deny(400, "unambiguous Content-Length is required")
            return True
        length = int(lengths[0])
        if not 0 < length <= MAX_PART_BYTES:
            handler.close_connection = True
            handler._deny(413, "part size is out of range")
            return True
        reader, chunks = LimitedInput(handler.rfile, length), []
        while True:
            chunk = reader.read()
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != length:
            handler.close_connection = True
            handler._deny(400, "truncated part")
            return True
        stored = handler._artifact_call("put_upload_part", parts[6], artifact_id, int(parts[8]), data,
                                        sha256=handler.headers.get("X-Content-SHA256"))
        handler._json(stored, 200 if stored["replayed"] else 201)
    return True
