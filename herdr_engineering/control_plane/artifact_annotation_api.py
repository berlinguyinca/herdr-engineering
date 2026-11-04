"""Owner API for artifact annotations (list and dismiss).

Routes (both need the ``share`` permission on the artifact; a denial is a non-disclosing 404):

* ``GET /api/v1/artifacts/{id}/annotations?status=open|addressed|dismissed|withdrawn|all&limit=1..500&before=<cursor>``
  returns ``{"artifact_id", "annotations", "next"}``. Rows are the owner view: they carry ``author_email``,
  ``author_role`` and ``share_id``. ``next`` is an opaque cursor for ``before``.
* ``POST /api/v1/artifacts/{id}/annotations/{aid}/dismiss`` with ``{}`` (or no body) dismisses an open annotation of THIS
  artifact and returns ``{"annotation": {...}}``. A resolved one is a 409 ``not_open``.

Every id and query value from the URL is untrusted and checked here before it can reach SQL.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from .annotation_anchor import ANNOTATION_ID_RE
from .artifact_annotations import MAX_LIST, STATUSES, AnnotationError
from .artifact_report_api import _access
from .artifact_share_api import _call, _jsonable

_ARTIFACT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_LIMIT = re.compile(r"[0-9]{1,4}")
_CURSOR = re.compile(r"(?P<at>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?[+-][0-9]{2}:[0-9]{2})"
                     r"\|(?P<id>ann_[A-Za-z0-9_-]{22})")
_QUERY_KEYS = frozenset({"status", "limit", "before"})
_YEARS = range(2000, 3000)
_DEFAULT_LIMIT = 200


class _BadRequest(ValueError):
    pass


def _query(raw: str) -> tuple[str, int, str | None]:
    """``(status, limit, before)`` from the raw query string; ``_BadRequest`` for anything not exactly valid."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=bool(raw), encoding="utf-8", errors="strict")
    except ValueError:
        raise _BadRequest("malformed query string") from None
    seen: dict[str, str] = {}
    for key, value in pairs:
        if key not in _QUERY_KEYS or key in seen:
            raise _BadRequest(f"unknown or repeated query parameter {key!r}")
        seen[key] = value
    status = seen.get("status", "open")
    if status not in (*STATUSES, "all"):
        raise _BadRequest("status must be one of: " + ", ".join((*STATUSES, "all")))
    limit = _DEFAULT_LIMIT
    if "limit" in seen:
        if not _LIMIT.fullmatch(seen["limit"]) or not 1 <= int(seen["limit"]) <= MAX_LIST:
            raise _BadRequest(f"limit must be an integer from 1 to {MAX_LIST}")
        limit = int(seen["limit"])
    before = seen.get("before")
    if before is not None:
        match = _CURSOR.fullmatch(before)
        try:
            stamp = datetime.fromisoformat(match["at"]) if match else None
        except ValueError:
            stamp = None
        if stamp is None or stamp.tzinfo is None or stamp.year not in _YEARS:
            raise _BadRequest("before is not a cursor returned by this API")
    return status, limit, before


def _store_missing(server) -> bool:
    return getattr(server, "annotation_store", None) is None or server.artifact_store is None


def route_annotations(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/artifacts/{id}/annotations[/{aid}/dismiss]``; false for any other path."""
    if resource != "artifacts" or len(parts) < 6 or parts[5] != "annotations":
        return False
    method = handler.command
    listing = len(parts) == 6 and method == "GET"
    dismiss = len(parts) == 8 and parts[7] == "dismiss" and method == "POST"
    # Early answers leave a POST body unread: ``_deny`` closes the connection so its bytes are never parsed as a request.
    if not listing and not dismiss:
        handler._deny(404, "not found")
        return True
    if _store_missing(server):
        handler._deny(503, "annotations are not enabled")
        return True
    origin = handler.headers.get("Origin")
    if method == "POST" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                         or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin annotation mutation refused")
        return True
    artifact_id = parts[4]
    if not _ARTIFACT_ID.fullmatch(artifact_id) or not _access(handler, artifact_id, "share"):
        handler._deny(404, "not found")
        return True
    if listing:
        _list(handler, server, artifact_id)
    else:
        _dismiss(handler, server, artifact_id, parts[6])
    return True


def _list(handler, server, artifact_id: str) -> None:
    try:
        status, limit, before = _query(urlsplit(handler.path).query)
    except _BadRequest as exc:
        handler._json({"error": str(exc)}, 400)
        return
    try:
        rows, cursor = _call(server.annotation_store, "list_for_artifact", artifact_id, status=status, limit=limit,
                             before=before)
    except AnnotationError as exc:
        handler._json({"error": exc.code}, 400)
        return
    handler._json({"artifact_id": artifact_id, "annotations": _jsonable(rows), "next": cursor})


def _dismiss(handler, server, artifact_id: str, annotation_id: str) -> None:
    if not ANNOTATION_ID_RE.fullmatch(annotation_id):
        handler._deny(404, "not found")
        return
    if int(handler.headers.get("Content-Length") or 0) and (
            handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"):
        handler._deny(415, "annotation requests require application/json")
        return
    try:
        body = handler._read_json()
    except ValueError as exc:
        handler._json({"error": str(exc)}, 400)
        return
    if body is not None and body != {}:
        handler._json({"error": "dismiss takes an empty JSON object"}, 400)
        return
    try:
        row = _call(server.annotation_store, "dismiss", artifact_id, annotation_id, actor=handler._resource_principal())
    except AnnotationError as exc:
        status = {"not_found": 404, "not_open": 409}.get(exc.code, 400)
        handler._json({"error": "not found" if status == 404 else exc.code}, status)
        return
    handler._json({"annotation": _jsonable(row)})
