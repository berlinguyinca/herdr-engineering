"""The author's HTML page in a report share (opt-in "Rich view"; spec 2026-10-30 addendum 2026-11-05).

A report share normally holds only the generated ``preview.html``. When the owner asks for it (``include_author_html``)
and the approver sees it listed in the plan, the version's single ``text/html`` asset (chosen by the SAME rule as the
dashboard Rich view, ``artifact_rich_view.pick_html_asset``) is copied into the bundle as ``author-page.html``. The share
host serves that one file under the dashboard's no-script ``sandbox`` CSP (never the bundle's permissive CSP), and the
preview page gets a "Rich view" toggle that frames it in an iframe with an empty ``sandbox``.

No schema change: the opt-in rides in the pending share's ``entry`` column as ``PENDING_MARK`` (a report share's entry is
otherwise ``None`` or ``preview.html``); once live, ``state.json`` carries ``"rich": "author-page.html"`` instead.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from . import artifact_rich_view as dashboard_rich
from .artifact_report_render import _CSP as PREVIEW_CSP

RICH_NAME = "author-page.html"
PENDING_MARK = "preview.html+author-html"
MAX_RICH_BYTES = dashboard_rich.MAX_RICH_BYTES
# The dashboard's headers minus the two framing controls: the share preview is served in a sandbox (opaque origin), and
# an opaque parent never matches 'self' or SAMEORIGIN. The page stays inert through the bare ``sandbox`` CSP directive
# (no allow-*), and only the preview's meta CSP (``frame-src 'self'``) lets it be framed at all.
SHARE_RICH_CSP = "; ".join(d for d in dashboard_rich.RICH_CSP.split("; ") if not d.startswith("frame-ancestors"))
SHARE_RICH_HEADERS = tuple(
    ("Content-Security-Policy", SHARE_RICH_CSP) if k == "Content-Security-Policy" else (k, v)
    for k, v in dashboard_rich.RICH_HEADERS if k != "X-Frame-Options")
_TOGGLE_JS = Path(__file__).with_name("share_static") / "rich-toggle.js"

_PANEL = ('<style>[data-rich-view]{margin:16px 0}[data-rich-view] button{border:0;cursor:pointer;font:inherit;'
          'font-weight:600;font-size:13px}[data-rich-frame]{display:block;width:100%;height:70vh;margin-top:12px;'
          'border:1px solid var(--border);border-radius:var(--radius);background:#fff}</style>'
          '<section class="card" data-rich-view hidden><h2>Rich view</h2>'
          "<p>The author's own page, shown in an isolated frame.</p>"
          '<button type="button" class="btn" data-rich-toggle aria-expanded="false">Show rich view</button>'
          "<div data-rich-slot></div></section>")


def with_toggle(page: str) -> str:
    """``page`` (a generated report preview) with the hidden-until-script Rich view panel after its header.

    The preview's own meta CSP forbids scripts and frames. For an opted-in page it gains exactly two things: this one
    script, allowed by its sha256 (nothing else on the page may run), and ``frame-src 'self'`` (the share host's header
    CSP still narrows frames to this share's own prefix)."""
    code = _TOGGLE_JS.read_text(encoding="utf-8")
    digest = base64.b64encode(hashlib.sha256(code.encode("utf-8")).digest()).decode("ascii")
    meta = f'<meta http-equiv="Content-Security-Policy" content="{PREVIEW_CSP}">'
    marker = "</header>"
    if page.count(meta) != 1 or page.count(marker) < 1:
        raise ValueError("report preview does not have the expected policy and header")
    page = page.replace(meta, meta.replace(PREVIEW_CSP, f"{PREVIEW_CSP}; script-src 'sha256-{digest}'; frame-src 'self'"))
    at = page.find(marker) + len(marker)
    return page[:at] + _PANEL + "<script>" + code + "</script>" + page[at:]


def select_author_asset(descriptor: dict) -> dict | None:
    """The one HTML asset of the report (same selection as the dashboard route), or None."""
    return dashboard_rich.pick_html_asset(descriptor)
