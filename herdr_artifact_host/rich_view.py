"""Rich-view helpers the share path needs: pick the author's HTML asset and the sandbox headers it is served under.

Copied from the control plane's dashboard Rich view minus its request handler (which needs the object store): this
package has no database or object-store imports. ``ContentError`` is the local stand-in for the control plane's
artifact error, raised when stored content is unusable.
"""

from __future__ import annotations

from .media_type import safe_media_type

MAX_RICH_BYTES = 2 * 1024 * 1024
CHUNK = 64 * 1024
RICH_CSP = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)
RICH_HEADERS = (
    ("Content-Type", "text/html; charset=utf-8"),
    ("Content-Security-Policy", RICH_CSP),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "private, no-store"),
)
# The generated report preview's own meta CSP (no scripts, no frames); the Rich view toggle extends it.
PREVIEW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"


class ContentError(Exception):
    """Stored artifact content is missing, malformed or fails its integrity check."""


def pick_html_asset(descriptor: dict) -> dict | None:
    """The one HTML asset of a report descriptor, or None when there is none or the choice is ambiguous."""
    assets = [
        a
        for a in descriptor.get("assets") or []
        if isinstance(a, dict) and safe_media_type(a.get("media_type")) == "text/html"
    ]
    if len(assets) > 1:
        assets = [a for a in assets if a.get("role") == "preview"]
    return assets[0] if len(assets) == 1 else None
