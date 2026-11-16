"""The dashboard "Rich view": a report version's author-supplied HTML asset, served for a sandboxed frame.

``GET /api/v1/artifacts/{id}/versions/{vid}/rich`` answers with exactly one asset of the version: its single
``text/html`` asset (when several, the single one with role ``preview``; otherwise a plain 404). The bytes are served
under a CSP whose ``sandbox`` directive carries no ``allow-*`` token: the document gets an opaque origin, scripts do
not run even if the markup has them, and nothing can reach the dashboard. The caller (``route_render``) has already
checked the same read permission as the other versioned render routes.
"""
from __future__ import annotations

import hashlib

from ..media_type import safe_media_type
from .artifacts import ArtifactError

MAX_RICH_BYTES = 2 * 1024 * 1024
CHUNK = 64 * 1024
RICH_CSP = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
RICH_HEADERS = (("Content-Type", "text/html; charset=utf-8"), ("Content-Security-Policy", RICH_CSP),
                ("X-Frame-Options", "SAMEORIGIN"), ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"), ("Cache-Control", "private, no-store"))


def pick_html_asset(descriptor: dict) -> dict | None:
    """The one HTML asset of a report descriptor, or None when there is none or the choice is ambiguous."""
    assets = [a for a in descriptor.get("assets") or []
              if isinstance(a, dict) and safe_media_type(a.get("media_type")) == "text/html"]
    if len(assets) > 1:
        assets = [a for a in assets if a.get("role") == "preview"]
    return assets[0] if len(assets) == 1 else None


def _read_verified(server, asset: dict) -> bytes | None:
    """The asset bytes, hash- and size-verified against the pinned metadata; None when over the cap."""
    data = bytearray()
    digest = hashlib.sha256()
    for chunk in server.rustfs.iter_get(asset["storage_key"], asset["sha256"], chunk_size=CHUNK):
        data += chunk
        digest.update(chunk)
        if len(data) > MAX_RICH_BYTES or len(data) > asset["size_bytes"]:
            return None
    if len(data) != asset["size_bytes"] or digest.hexdigest() != asset["sha256"]:
        raise ValueError("pinned report asset integrity mismatch")
    return bytes(data)


def serve_rich(handler, server, artifact_id: str, version_id: str) -> None:
    """Send the version's HTML asset sandboxed, or a uniform 404 / 413."""
    if handler._artifact_call("get_version", version_id, artifact_id) is None:
        handler._deny(404, "not found")
        return
    try:
        asset = pick_html_asset(handler._artifact_call("get_report_descriptor", artifact_id, version_id))
        if asset is not None and asset.get("size_bytes", 0) <= MAX_RICH_BYTES:
            asset = handler._artifact_call("get_report_asset", artifact_id, version_id, asset["id"])
    except ArtifactError:
        asset = None
    if asset is None or server.rustfs is None or not hasattr(server.rustfs, "iter_get"):
        handler._deny(404, "not found")
        return
    if asset["size_bytes"] > MAX_RICH_BYTES:
        handler._deny(413, "report page too large")
        return
    data = _read_verified(server, asset)
    if data is None:
        handler._deny(413, "report page too large")
        return
    handler._render_started = True
    handler._response_status = 200
    handler.send_response(200)
    for name, value in RICH_HEADERS:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
