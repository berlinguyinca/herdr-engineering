"""Dashboard routes of the artifact library: ``/api/v1/library...`` (Tailscale dashboard only, never the public host).

The feed, folders, filing and pending accept/discard call the same ``LibraryStore`` as the member intents, so the two
surfaces cannot disagree.  The viewer is the dashboard's API-key principal (``api-key:<digest>:<role>``): it is not a
Cognito member, so that principal is the library owner everywhere (feed, folders, filing, pending) -- unless the
authenticated key is listed in ``control_plane.artifact_library.viewers``, which makes it that member's library (the
member principal, with email and groups from the member directory; fail closed when that is stale).  Another viewer's
folder, or an artifact the viewer may not read, is the same ``404 not_found``.  There is no claim route: claims come
only from the public share flow.  ``GET`` needs any authenticated role; every write needs ``operator``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from .artifact_library import (
    DEFAULT_SOURCES,
    MAX_PAGE,
    LibraryError,
    LibraryStore,
)

log = logging.getLogger("herdr.control_plane.artifact_library_api")

SOURCES: Final = frozenset({*DEFAULT_SOURCES, "pending"})
_ITEM_FIELDS: Final = frozenset({"folder_id", "artifact_id"})
_NAME_FIELDS: Final = frozenset({"name"})


@dataclass(frozen=True)
class Viewer:
    """Whose library a request reads: its owner principal, and for a mapped member the identity the feed needs."""
    principal: str
    member_id: str = ""
    email: str = ""
    groups: frozenset[str] = frozenset()
    member: bool = False


def parse_library_viewers(raw: Any) -> dict[str, str]:
    """``{"key:<digest>": "<member id>"}`` validated; ``ValueError`` names the bad entry."""
    import re

    from .artifact_members import member_principal
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("artifact_library.viewers must be a mapping of key:<digest> to a member id")
    out: dict[str, str] = {}
    for key, member in raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"key:[0-9a-f]{12}", key):
            raise ValueError(f"artifact_library.viewers key {key!r} must look like key:<12 hex digits>")
        try:
            member_principal(member)
        except ValueError:
            raise ValueError(f"artifact_library.viewers[{key}] is not a valid member id") from None
        out[key] = member
    return out


def _directory_body(server) -> dict | None:
    source = getattr(server, "member_directory", None)
    if source is None and getattr(server, "collab", None) is not None:
        source = getattr(server.collab, "member_directory", None)
    return source() if source is not None else None


def resolve_viewer(handler, server) -> Viewer:
    """The library owner of this request.  Only the authenticated key digest (``key:<digest>``) is looked up in
    ``library_viewers``; the client-supplied ``X-Herdr-User`` header is never read.  An unmapped key keeps its own key
    principal.  A mapped key needs a fresh directory that lists its member as approved, else it fails closed with
    ``503 directory_stale`` (``403 not_a_member`` for a disabled or removed member): never a guess."""
    from .artifact_members import member_principal
    from .collab_bridge_files import FEED_MAX_AGE
    key_principal = handler._resource_principal()
    member_id = server.library_viewers.get(handler._principal().get("id"))
    if member_id is None:
        return Viewer(key_principal)
    stale = LibraryError(503, "directory_stale", "the member directory is unavailable; try again shortly")
    body = _directory_body(server)
    if not isinstance(body, dict) or not isinstance(body.get("users"), dict):
        raise stale
    updated = body.get("updated")
    if updated is not None and (isinstance(updated, bool) or not isinstance(updated, (int, float))
                                or not math.isfinite(updated) or abs(time.time() - updated) > FEED_MAX_AGE):
        raise stale
    entry = body["users"].get(member_id)
    if not isinstance(entry, dict):
        raise stale
    if entry.get("status") != "approved":
        raise LibraryError(403, "not_a_member", "this member is not active")
    email, groups = entry.get("email"), entry.get("groups")
    return Viewer(member_principal(member_id), member_id, email if isinstance(email, str) else "",
                  frozenset(g for g in groups or [] if isinstance(g, str)), True)


def error_body(exc: LibraryError) -> tuple[int, dict]:
    """``(status, body)`` of a ``LibraryError``; the body is the code and message only."""
    return exc.status, {"error": exc.code, "message": exc.message}


def _bad(handler, message: str = "invalid request") -> bool:
    handler._json({"error": "invalid_request", "message": message}, 400)
    return True


def _body(handler, allowed: frozenset[str]) -> dict:
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("this route requires application/json")
    value = handler._read_json() if handler.headers.get("Content-Length") else {}
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("invalid request fields; identity is derived from authentication")
    return value


def _text(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps need a time zone")
    return parsed


def parse_feed_query(query: dict[str, list[str]]) -> dict[str, Any]:
    """The ``feed`` keyword arguments of a ``GET /api/v1/library`` query string; ``ValueError`` for anything malformed."""
    def one(name: str) -> str | None:
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1:
            raise ValueError(f"{name} may be given once")
        return values[0] or None

    sources = one("sources")
    chosen = None
    if sources:
        chosen = frozenset(sources.split(","))
        if not chosen <= SOURCES:
            raise ValueError("sources must be from: " + ", ".join(sorted(SOURCES)))
    limit, offset = one("limit"), one("offset")
    return {
        "q": one("q"), "sources": chosen, "folder_id": one("folder_id"), "artifact_type": one("type"),
        "since": _time(one("since")), "until": _time(one("until")),
        "limit": min(int(limit), MAX_PAGE) if limit else 50, "offset": int(offset) if offset else 0,
    }


def _classify(method: str, parts: list[str]) -> str | None:
    """The action for ``parts`` (``['', 'api', 'v1', 'library', ...]``), or None for an unknown route."""
    n = len(parts)
    if n == 4:
        return "feed" if method == "GET" else None
    if parts[4] == "folders":
        if n == 5 and method == "POST":
            return "folder_create"
        if n == 6 and method in {"PUT", "DELETE"}:
            return "folder_rename" if method == "PUT" else "folder_delete"
        if n == 8 and parts[6] == "items" and method == "DELETE":
            return "unfile"
    if parts[4] == "items" and n == 5 and method == "POST":
        return "file"
    if parts[4] == "pending" and n == 7 and method == "POST" and parts[6] in {"accept", "discard"}:
        return parts[6]
    return None


def make_library_adapter(pool_factory):
    """A ``SyncArtifactAdapter`` over a ``LibraryStore`` (``pool_factory`` is a coroutine function returning a pool)."""
    from .artifact_adapter import SyncArtifactAdapter

    async def create():
        return LibraryStore(await pool_factory())

    return SyncArtifactAdapter(create)


def _library_call(server, method: str, *args, **kwargs):
    from .artifact_share_api import _call
    return _call(server.library, method, *args, **kwargs)


def _retract_pending(handler, server, principal: str):
    """The delete path of a discard: retract the artifact through the registry and revoke any share, as a member's
    delete does.  Runs in a worker thread because the registry adapter is synchronous."""
    def retract(artifact_id: str) -> None:
        handler._artifact_call("record_retraction", artifact_id, reason="dashboard discard")
        from .artifact_delete_api import revoke_shares
        if revoke_shares(handler, server, artifact_id, principal) is None:    # the retraction stands; a repeat retries
            raise LibraryError(500, "share_revoke_failed",
                               "the artifact was retracted but its shares could not all be revoked; discard it again")

    async def delete(artifact_id: str) -> None:
        await asyncio.to_thread(retract, artifact_id)
    return delete


def route_library(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/library...``; false for any other path."""
    if resource != "library":
        return False
    method = handler.command
    action = _classify(method, parts)
    if action is None:
        handler._deny(404, "not found")
        return True
    if handler._require("viewer" if method == "GET" else "operator"):
        return True
    if getattr(server, "library", None) is None or server.artifact_store is None:
        handler._deny(503, "the library is not enabled")
        return True
    origin = handler.headers.get("Origin")
    if method != "GET" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                        or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin library mutation refused")
        return True
    try:
        _dispatch(handler, server, action, parts, resolve_viewer(handler, server))
    except LibraryError as exc:
        status, body = error_body(exc)
        handler._json(body, status)
    except ValueError as exc:
        _bad(handler, str(exc))
    return True


def _dispatch(handler, server, action: str, parts: list[str], viewer: Viewer) -> None:
    from .artifact_member_intents import _folder_json, _item_json
    principal = viewer.principal
    call = lambda method, *a, **k: _library_call(server, method, *a, **k)  # noqa: E731
    if action == "feed":
        kwargs = parse_feed_query(parse_qs(urlsplit(handler.path).query))
        page = call("feed", viewer.member_id or principal, viewer.email, viewer.groups, principal=principal, **kwargs)
        folders = call("list_folders", principal)
        items = [_item_json(i) for i in page.items]
        if viewer.member:       # a member's rows can be ones the dashboard's ACL will not open: say so, the page unlinks them
            for item in items:
                item["readable"] = item["status"] in {"active", "pending"} and bool(
                    handler._artifact_allowed(item["artifact_id"], "read"))
        handler._json({"items": items, "total": page.total,
                       "next_offset": page.next_offset, "folders": [_folder_json(f) for f in folders]})
    elif action == "folder_create":
        folder = call("create_folder", principal, _text(_body(handler, _NAME_FIELDS), "name"))
        handler._json({"folder": _folder_json(folder)}, 201)
    elif action == "folder_rename":
        name = _text(_body(handler, _NAME_FIELDS), "name")
        handler._json({"folder": _folder_json(call("rename_folder", principal, parts[5], name))})
    elif action == "folder_delete":
        call("delete_folder", principal, parts[5])
        handler._json({"deleted": parts[5]})
    elif action == "file":
        body = _body(handler, _ITEM_FIELDS)
        call("file_artifact", principal, _text(body, "folder_id"), _text(body, "artifact_id"))
        handler._json({"filed": body["artifact_id"], "folder_id": body["folder_id"]}, 201)
    elif action == "unfile":
        call("unfile_artifact", principal, parts[5], parts[7])
        handler._json({"unfiled": parts[7], "folder_id": parts[5]})
    elif action == "accept":
        call("accept_pending", principal, parts[5])
        handler._json({"result": "accepted", "artifact_id": parts[5]})
    elif viewer.member:         # the member delete itself: shares revoked inside the transaction, quota settled
        call("discard_member_pending", principal, parts[5], shares_root=getattr(server, "shares_root", None),
             rustfs=getattr(server, "rustfs", None))
        handler._json({"result": "deleted", "artifact_id": parts[5]})
    else:
        call("discard_pending", principal, parts[5], _retract_pending(handler, server, principal))
        handler._json({"result": "deleted", "artifact_id": parts[5]})


__all__ = ["error_body", "make_library_adapter", "parse_feed_query", "route_library"]
