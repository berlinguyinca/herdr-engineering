"""Owner dashboard render routes: static copies of artifact bytes and the sandboxed live route.

Routes (``GET`` only; each needs the same read permission as ``/download`` / the outputs route, else a plain 404):

* ``/api/v1/artifacts/{id}/render``: ``{"kind", "html", "too_large"}`` for the current file.
* ``/api/v1/artifacts/{id}/versions/{vid}/outputs/{i}/render``: the same for a legacy report output.
* ``/api/v1/artifacts/{id}/versions/{vid}/preview/render``: ``{"kind": "html", "html", "too_large"}`` for the report preview.
* ``/api/v1/artifacts/{id}/live``: the raw bytes under a sandbox CSP, for a new tab (never framed).

These paths must be tried BEFORE ``route_report``, which claims versioned ``preview`` and ``outputs`` paths of any depth.

Errors are uniform: ``RenderBusy`` and any store, blob or driver failure give a fixed 503, every other render error a
fixed 422. No response ever carries exception text. The sanitiser is ``share_render`` (nh3 in a killable child).
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit

from .artifact_report_api import PinnedOutputError, _access, pinned_output
from .share_bundle import ShareBundleError, normalise_name
from .share_render import (
    MAX_RENDER_INPUT,
    RenderBusy,
    RenderFailed,
    RenderTooLarge,
    render_kind,
    render_text,
    sanitize_html,
)
from .share_render_worker import _DATA_IMAGE, _STRIP_IN_URL, Resolver

_LOG = logging.getLogger(__name__)
_UNAVAILABLE = {"error": "render unavailable"}
_REFUSED = {"error": "render refused"}
_INDEX = re.compile(r"[0-9]{1,6}")
_DASHBOARD_KEY = "dashboard-v1"

LIVE_CSP = ("sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")
PDF_LIVE_CSP = "frame-ancestors 'none'"


class _Unavailable(Exception):
    """Internal: answer the fixed 503 (the cause is logged by class name only)."""


def _clean(url: str) -> str | None:
    cleaned = _STRIP_IN_URL.sub("", url).strip(" \x0c")
    if not cleaned or any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        return None
    return cleaned


def _absolute_link(cleaned: str) -> str | None:
    """An absolute http(s) or mailto link, kept as written; None for anything else."""
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return None
    if cleaned.startswith(("//", "\\")) or "\\" in cleaned:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https") and parts.netloc and not parts.netloc.startswith("\\"):
        return cleaned
    if scheme == "mailto" and parts.path and not parts.netloc:
        return cleaned
    return None


def dashboard_resolver(siblings: dict[str, str] | None = None, name: str = "") -> Resolver:
    """URL rewriting for the dashboard's static copy.

    Absolute http(s)/mailto links and ``data:image/(png|jpeg|gif|webp)`` images survive; every relative reference is
    dropped (a fragment link would drive the SPA's hash router). ``siblings`` maps the normalised name of each other
    output of the same version to its outputs-route URL; an ``img``/``link``/``css`` reference naming one of them is
    rewritten to it, resolved relative to ``name``.
    """
    index = siblings or {}

    def resolve(context: str, url: str) -> str | None:
        if context not in ("a", "img", "link", "css"):
            return None
        cleaned = _clean(url)
        if cleaned is None:
            return None
        if context == "img" and _DATA_IMAGE.fullmatch(cleaned):
            return cleaned
        try:
            parts = urlsplit(cleaned)
        except ValueError:
            return None
        if parts.scheme or parts.netloc or cleaned.startswith(("//", "\\")) or "\\" in cleaned:
            return _absolute_link(cleaned) if context == "a" else None
        if context == "a" or not index or not parts.path or parts.query or parts.fragment:
            return None
        try:
            target = normalise_name(posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path))))
        except (ShareBundleError, ValueError):
            return None
        return index.get(target)

    return resolve


def _send_json(handler, body: dict, status: int = 200) -> None:
    payload = json.dumps(body).encode()
    handler._response_status = status
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(payload)))
    if handler.close_connection:
        handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(payload)


def _envelope(kind: str, data: bytes, resolve: Resolver, cache_key) -> dict:
    """The render response body for ``data`` of ``kind``; raises the ``Render*`` errors."""
    if kind not in ("html", "text"):
        return {"kind": kind, "html": None, "too_large": False}
    if len(data) > MAX_RENDER_INPUT:
        return {"kind": kind, "html": None, "too_large": True}
    html = sanitize_html(data, resolve=resolve, cache_key=cache_key) if kind == "html" else render_text(data)
    return {"kind": kind, "html": html, "too_large": False}


def _send_render(handler, kind: str, data: bytes, resolve: Resolver, cache_key) -> None:
    try:
        body = _envelope(kind, data, resolve, cache_key)
    except RenderBusy:
        _send_json(handler, _UNAVAILABLE, 503)
    except (RenderTooLarge, RenderFailed):
        _send_json(handler, _REFUSED, 422)
    else:
        _send_json(handler, body)


def _live_headers(mime: str) -> list[tuple[str, str]]:
    base = mime.split(";")[0].strip().lower()
    csp = PDF_LIVE_CSP if base == "application/pdf" else LIVE_CSP
    content_type = mime if ";" in mime or not base.startswith("text/") else mime + "; charset=utf-8"
    return [("Content-Type", content_type), ("Content-Security-Policy", csp), ("X-Frame-Options", "DENY"),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store")]


def _send_live(handler, data: bytes, mime: str) -> None:
    handler._response_status = 200
    handler.send_response(200)
    for name, value in _live_headers(mime):
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _legacy_output(handler, server, artifact_id: str, version_id: str, index: int):
    """``(output_manifest, pinned_output, bytes)`` for a legacy output, or None for a plain 404."""
    version = handler._artifact_call("get_version", version_id, artifact_id)
    outputs = (version or {}).get("output_manifest") or []
    if index >= len(outputs):
        return None
    try:
        pinned = pinned_output(outputs[index])
    except PinnedOutputError:
        return None
    if server.rustfs is None:
        raise _Unavailable
    return outputs, pinned, server.rustfs.get(pinned["storage_key"], pinned["sha256"])


def _sibling_urls(artifact_id: str, version_id: str, outputs: list, own: int) -> dict[str, str]:
    prefix = f"/api/v1/artifacts/{quote(artifact_id, safe='')}/versions/{quote(version_id, safe='')}/outputs/"
    urls: dict[str, str] = {}
    for number, output in enumerate(outputs):
        if number == own or not isinstance(output, dict) or not isinstance(output.get("name"), str):
            continue
        try:
            urls.setdefault(normalise_name(output["name"]), prefix + str(number))
        except (ShareBundleError, ValueError):
            continue
    return urls


def route_render(handler, server, parts, resource) -> bool:
    """Handle the render / live routes; return False for every other path."""
    if resource != "artifacts" or handler.command != "GET":
        return False
    count = len(parts)
    if count == 6 and parts[5] in ("render", "live"):
        route = parts[5]
    elif count == 10 and parts[5] == "versions" and parts[7] == "outputs" and parts[9] == "render":
        route = "output"
    elif count == 9 and parts[5] == "versions" and parts[7] == "preview" and parts[8] == "render":
        route = "preview"
    else:
        return False
    artifact_id = parts[4]
    versioned = route in ("output", "preview")
    try:
        if not handler._artifact_allowed(artifact_id, "read") or (
                versioned and (server.artifact_store is None or not _access(handler, artifact_id, "read"))):
            handler._deny(404, "not found")
            return True
        if route in ("render", "live"):
            record, data = handler._artifact_current_file(artifact_id)
            if record is None or data is None:
                handler._deny(404, "not found")
                return True
            mime = record.get("mime_type") or "application/octet-stream"
            if route == "live":
                _send_live(handler, data, mime)
                return True
            name = record.get("original_filename") or ""
            kind = render_kind(name, mime)
            key = (_DASHBOARD_KEY, artifact_id, record.get("sha256"))
            _send_render(handler, kind, data, dashboard_resolver(), key)
            return True
        version_id = parts[6]
        if route == "preview":
            from .artifact_report_render import render_report_preview
            descriptor = (handler._artifact_call("get_report_descriptor", artifact_id, version_id)
                          if handler._artifact_call("get_version", version_id, artifact_id) is not None else None)
            if descriptor is None:
                handler._deny(404, "not found")
                return True
            page = render_report_preview(descriptor, asset_url=lambda asset: "#").encode("utf-8")
            _send_render(handler, "html", page, dashboard_resolver(), None)
            return True
        if not _INDEX.fullmatch(parts[8]):
            handler._deny(404, "not found")
            return True
        index = int(parts[8])
        found = _legacy_output(handler, server, artifact_id, version_id, index)
        if found is None:
            handler._deny(404, "not found")
            return True
        outputs, pinned, data = found
        kind = render_kind(pinned["name"], pinned["media_type"])
        siblings = _sibling_urls(artifact_id, version_id, outputs, index)
        try:
            name = normalise_name(pinned["name"])
        except (ShareBundleError, ValueError):
            name = ""
        key = (_DASHBOARD_KEY, artifact_id, version_id, pinned["sha256"], tuple(sorted(siblings.items())))
        _send_render(handler, kind, data, dashboard_resolver(siblings, name), key)
        return True
    except Exception as exc:  # noqa: BLE001 - fixed 503; the class name is the only thing logged
        _LOG.error("artifact render route failed: %s", type(exc).__name__)
        _send_json(handler, _UNAVAILABLE, 503)
        return True
