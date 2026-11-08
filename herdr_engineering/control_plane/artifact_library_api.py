"""Dashboard routes of the artifact library: ``/api/v1/library...`` (Tailscale dashboard only, never the public host).

Read-only view over the artifact host's internal listener (``ArtifactHostClient``).  The library shows members'
artifacts only; the dashboard's own Artifacts page keeps the CI and agent reports.  There is no write route: every
method but ``GET`` is ``405``.

* ``GET /api/v1/library`` the feed (``q, sources, folder_id, type, since, until, limit, offset``), with folders
* ``GET /api/v1/library/folders``, ``/items/<id>``, ``/items/<id>/versions``, ``/shares``
* ``GET /api/v1/library/content/<item>/<version>/<path>`` the host's content, proxied onto the dashboard origin so a
  sandboxed iframe (which cannot send the key) can show it.  The response keeps the host's ``Content-Security-Policy``,
  ``X-Content-Type-Options`` and ``Content-Type`` byte for byte, sets no cookie, and is refused (``502``) when the host
  did not send a ``sandbox`` policy.  The target is always ``host_url/v1/content/<item>/<version>/<path>``.

The host being down, slow or refusing the key is ``503 library_unavailable`` for these routes only.  The host key is
never sent to the browser.
"""
from __future__ import annotations

import logging
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from .artifact_host_client import (
    BadReference,
    HostRejected,
    HostUnavailable,
    UnsafeContent,
    valid_id,
)

log = logging.getLogger("herdr.control_plane.artifact_library_api")

PROXY_EXTRA_HEADERS: Final = (("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                              ("X-Frame-Options", "SAMEORIGIN"))


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


def _query(handler) -> dict[str, str]:
    """The single-valued query parameters; a repeated one is a 400."""
    out: dict[str, str] = {}
    for name, values in parse_qs(urlsplit(handler.path).query, keep_blank_values=True).items():
        if len(values) != 1:
            raise HostRejected(400, "invalid_request", f"{name} may be given once")
        out[name] = values[0]
    return out


def _unavailable(handler) -> None:
    handler._json({"error": "library_unavailable", "message": "the artifact host is unavailable; try again shortly"}, 503)


def _classify(parts: list[str]) -> str | None:
    """The read action for ``parts`` (``['', 'api', 'v1', 'library', ...]``), or None for an unknown route."""
    n = len(parts)
    if n == 4:
        return "feed"
    kind = parts[4]
    if kind in {"folders", "shares"} and n == 5:
        return kind
    if kind == "items" and n == 6:
        return "item"
    if kind == "items" and n == 7 and parts[6] == "versions":
        return "versions"
    if kind == "content" and n >= 8:
        return "content"
    return None


def route_library(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/library...``; false for any other path."""
    if resource != "library":
        return False
    if handler._require("viewer"):
        return True
    if handler.command != "GET":
        handler.close_connection = True         # a body, if any, stays unread
        handler._json({"error": "the library is read-only"}, 405, headers={"Allow": "GET"})
        return True
    action = _classify(parts)
    if action is None:
        handler._deny(404, "not found")
        return True
    client = getattr(server, "library", None)
    if client is None:
        handler._deny(503, "the library is not enabled")
        return True
    try:
        if action == "content":
            _proxy_content(handler, client, parts)
        else:
            handler._json(_read(handler, client, action, parts))
    except BadReference:
        handler._deny(404, "not found")
    except HostRejected as exc:
        body: dict[str, Any] = {"error": exc.code}
        if exc.message:
            body["message"] = exc.message
        handler._json(body, exc.status)
    except HostUnavailable as exc:
        log.warning("artifact library: %s", exc)
        _unavailable(handler)
    return True


def _read(handler, client, action: str, parts: list[str]) -> dict:
    if action == "feed":
        return client.feed(_query(handler))
    if action == "folders":
        return client.folders()
    if action == "shares":
        return client.shares(_query(handler))
    item = valid_id(parts[5])
    return client.item(item) if action == "item" else client.versions(item)


def _proxy_content(handler, client, parts: list[str]) -> None:
    """Stream the host's content under the host's own headers.  Raises before any byte is sent when it must refuse."""
    path = "/".join(parts[7:])
    try:
        stream = client.open_content(parts[5], parts[6], path)
        with stream as (head, chunks):
            handler.send_response(200)
            for name, value in head.headers:
                handler.send_header(name, value)
            for name, value in PROXY_EXTRA_HEADERS:
                handler.send_header(name, value)
            if head.length is None:
                handler.close_connection = True
                handler.send_header("Connection", "close")
            handler.end_headers()
            try:
                for block in chunks:
                    handler.wfile.write(block)
            except HostUnavailable:
                handler.close_connection = True        # truncated: the browser sees a short body, never a reused socket
    except UnsafeContent as exc:
        log.warning("artifact library: refused host content (%s)", exc)
        handler._json({"error": "unsafe_content", "message": "the host did not send a sandboxed response"}, 502)


__all__ = ["parse_library_viewers", "route_library"]
