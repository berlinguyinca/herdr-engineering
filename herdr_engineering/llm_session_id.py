"""Dashboard session id <-> the form that fits gateway identity metadata.

The feed plugin names a herdr session ``herdr:<host>:<server>:<pane>`` with each part
passed through ``encodeURIComponent`` (deploy/herdr-cp-feed-plugin/emit.js), so a pane
``w1:p1`` becomes ``w1%3Ap1``. Gateway metadata (X-InferWeave-Session-ID, X-Session-Id,
and the proxy's ``session:<id>`` key label) allows only ``[A-Za-z0-9-_.:/]{1,128}``, so
the encoded id cannot travel there. The *header form* is the decoded id; it is
unambiguous because host and server cannot contain ``:``, so the pane is everything
after the third colon.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote

_META = re.compile(r"^[A-Za-z0-9\-_.:/]{1,128}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
_SERVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# encodeURIComponent leaves A-Z a-z 0-9 - _ . ! ~ * ' ( ) unescaped
_JS_SAFE = "-_.!~*'()"


def dashboard_form(value: str | None) -> str | None:
    """The dashboard session id for a header-form value, or None if it is not one."""
    if not isinstance(value, str) or not _META.fullmatch(value):
        return None
    parts = value.split(":", 3)
    if len(parts) != 4 or parts[0] != "herdr":
        return None
    _, host, server, pane = parts
    if not (_HOST.fullmatch(host) and _SERVER.fullmatch(server) and pane):
        return None
    return ":".join(quote(p, safe=_JS_SAFE) for p in parts)


def header_form(session_id: str | None) -> str | None:
    """The metadata-safe form of a dashboard session id, or None if it has none."""
    if not isinstance(session_id, str) or not session_id.startswith("herdr:"):
        return None
    decoded = ":".join(unquote(p) for p in session_id.split(":"))
    return decoded if dashboard_form(decoded) == session_id else None
