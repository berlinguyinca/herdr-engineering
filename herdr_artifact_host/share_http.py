"""Pure helpers of the public share surface: constants, CSP strings, request-path decoding, ETag and Range parsing, the
strict HTML bar-injection gate and the landing bar, plus the symlink-safe static-file reader.

Ported unchanged from ``share_public.py`` of the control plane (the security contract lives in ``public_server``); nothing
here touches the network or the database.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import stat
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote_to_bytes

from .share_bundle import ShareBundleError, content_type_for, normalise_name

CHUNK_BYTES = 1024 * 1024
DENIED_PER_MINUTE = (
    20  # denied events per (share, reason) per minute (a scanner cannot fill the events file)
)
OPENED_PER_MINUTE = 60  # opened events per share per minute
LOG_PER_MINUTE = 20  # log lines per reason per minute
MAX_CONNECTIONS = 128
MAX_RAW_PATH = 8192
SESSION_PER_SHARE_PER_MINUTE = 30  # session attempts per share per minute (blunts token guessing)
SESSION_GLOBAL_PER_MINUTE = (
    600  # verified session attempts across all lab shares per minute (bounds JWT work)
)
STATIC_MAX_BYTES = 1024 * 1024
VENDOR_MAX_BYTES = 4 * 1024 * 1024  # the vendored pdf.js worker is ~1.4 MB
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "share_static"
SHARED_BY_MAX = 120  # ``state.json`` ``shared_by``: the creator's display name on a member-created link (artifact_share_api)
VIEWER_FILE = "viewer.html"
STATIC_FILES = {
    "lab-signin.js": "text/javascript; charset=utf-8",
    "share.js": "text/javascript; charset=utf-8",
    "share.css": "text/css; charset=utf-8",
    "annotate.js": "text/javascript; charset=utf-8",
    "anchor.js": "text/javascript; charset=utf-8",
    "annotate.css": "text/css; charset=utf-8",
    "landing.js": "text/javascript; charset=utf-8",
    "landing.css": "text/css; charset=utf-8",
    "tokens.css": "text/css; charset=utf-8",
}
# The bundled Inter files (``share_static/fonts/``).  The names carry no version, so the cache is one day, not immutable.
FONT_FILES = {"inter-latin-400.woff2": "font/woff2", "inter-latin-600.woff2": "font/woff2"}
FONT_CACHE = "public, max-age=86400"
VIEW_FILE = "view.html"
MODE_SWITCH_MAX_BYTES = (
    2 * 1024 * 1024
)  # larger HTML files are served untouched (streamed), without the link
MODE_SWITCH_ETAG_SUFFIX = "-m1"
NONCE_BYTES = 18  # token_urlsafe(18) is 24 characters
# One level below static/vendor/pdfjs/: exactly the two vendored pdf.js modules (the licence and SHA256SUMS stay unserved).
VENDOR_FILES = {
    "pdf.min.mjs": "text/javascript; charset=utf-8",
    "pdf.worker.min.mjs": "text/javascript; charset=utf-8",
}
# The URL carries no version, so a bump keeps the path: browsers revalidate every time (no-cache) against a strong
# ETag that is the file's sha256 (the SHA256SUMS value), and get a 304 while it is unchanged.
VENDOR_CACHE = "no-cache"

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_SHARE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
RAW_PATH_OK = re.compile(r"[\x21-\x5b\x5d-\x7e]*")  # printable ASCII without space and backslash
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RANGE_SPEC = re.compile(r"(\d{0,19})-(\d{0,19})")
_SESSION_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,768}")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,6}")
_VIEW_PLACEHOLDER = re.compile(
    r"\{\{(NONCE|MODE|CSRF_META|SWITCH|DATA_JSON|PANEL|CONTENT|BANNER)\}\}"
)

_CSP_TEMPLATE = (
    "sandbox allow-scripts allow-downloads; default-src {self}; script-src {self} 'unsafe-inline' "
    "'wasm-unsafe-eval'; style-src {self} 'unsafe-inline'; img-src {self} data: blob:; font-src {self} data:; "
    "connect-src {self}; worker-src {self} blob:; object-src 'none'; base-uri 'self'; form-action 'none'; "
    "frame-ancestors 'none'"
)
CSP = _CSP_TEMPLATE.format(self="'self'")  # without a configured public origin
PDF_CSP = "frame-ancestors 'none'"
VIEWER_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' {origin}; img-src 'self' data:; "
    "font-src {public}/s/static/; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
VIEW_CSP = (
    "default-src 'none'; script-src 'nonce-{nonce}' 'strict-dynamic'; style-src-elem {origin}/s/static/ "
    "{origin}/s/{token}/ 'unsafe-inline'; style-src-attr 'unsafe-inline'; img-src {origin}/s/{token}/ data: blob:; "
    "font-src {origin}/s/{token}/ {origin}/s/static/ data:; connect-src {origin}/s/{token}/; "
    "worker-src {origin}/s/static/vendor/pdfjs/; frame-src 'none'; child-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# The annotation view: the plan's view policy (``VIEW_CSP``), path-scoped to this share, with these deviations.
#   * img-src has NO ``blob:`` (the plan's ``data: blob:``): sanitised content images are same-share ``f/`` URLs or
#     ``data:`` images, and pdf.js draws on canvases, so nothing needs a blob URL.
#   * ``style-src-elem`` and ``style-src-attr`` keep ``'unsafe-inline'`` (as the plan does): the sanitiser keeps content
#     <style> elements and style attributes inside the shadow root and they cannot carry our nonce. A style cannot run
#     code, and no remote origin is allowed in either directive (an @import of a remote sheet cannot load).
#   * font-src keeps ``data:`` (plan); there is no ``style-src`` (the two split directives cover every style use).
# Verified in a real browser by tests/playwright/annotate-view.test.mjs.
ANNOTATE_CSP = VIEW_CSP.replace(
    "img-src {origin}/s/{token}/ data: blob:;", "img-src {origin}/s/{token}/ data:;"
)
PDFJS_CONFIG = {
    "module": "/s/static/vendor/pdfjs/pdf.min.mjs",
    "worker": "/s/static/vendor/pdfjs/pdf.worker.min.mjs",
    "options": {"isEvalSupported": False, "enableXfa": False},
}
MODE_SWITCH_TEMPLATE = (
    '<a data-herdr-mode-switch href="{href}" rel="noopener" style="position:fixed;right:12px;bottom:12px;'
    "z-index:2147483647;display:block;margin:0;padding:6px 12px;font:600 14px/1.4 system-ui,sans-serif;color:#fff;"
    'background:#1f4fd1;border-radius:6px;text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,.4)">Annotate</a>'
)
SHARED_BY_BAR = (
    '<div data-herdr-shared-by role="note" style="position:fixed!important;top:0!important;left:0!important;'
    "right:0!important;z-index:2147483647!important;display:block!important;visibility:visible!important;"
    "opacity:1!important;margin:0!important;padding:8px 12px!important;font:600 14px/1.4 system-ui,sans-serif!important;"
    "color:#4a3b00!important;background:#fff3cd!important;border-bottom:1px solid #b8a04a!important;"
    'text-align:center!important;transform:none!important;clip:auto!important;height:auto!important">{text}</div>'
)
SHARED_BY_ETAG_SUFFIX = "-b1"
# The recipient landing bar: "Shared by NAME . title . expires in N days", and for a visitor whose session the server has
# not verified, the sign-in notice and a link to the sign-in step. Plain markup with inline light styles (the page is
# the author's, in a sandbox: no script and no stylesheet of ours runs there). It sits in the flow, so the notice never
# covers the content; ``margin_top`` clears the fixed shared-by label bar when the page has one.
LANDING_FALLBACK_SHARER = "a lab member"
LANDING_NOTICE = (
    "Sign in with your lab account to add this to your library. Signing in does not change who can "
    "see it."
)
LANDING_BUTTON = "Sign in to keep this"
LANDING_TITLE_MAX = 120
LANDING_ETAG_SUFFIX = "-l1"
CLAIM_COOKIE = "herdr_claim"  # a public link's verified-visitor session (HttpOnly, Path=/s/<token>/): never in a URL
CLAIM_SEEN_MAX = 50000  # (share, session) pairs remembered so one session records a claim once
_LANDING_STYLE = (
    "display:block!important;visibility:visible!important;opacity:1!important;margin:{margin}px 0 0 0!important;"
    "padding:8px 16px!important;font:500 14px/1.4 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif!important;"
    "color:#353740!important;background:#fff!important;border-bottom:1px solid #d9d9e3!important;"
    "text-align:left!important"
)
_LANDING_LINK_STYLE = (
    "display:inline-block!important;margin:0 0 0 12px!important;padding:4px 12px!important;"
    "background:#10a37f!important;color:#fff!important;border-radius:8px!important;"
    "font:600 14px/1.4 system-ui,sans-serif!important;text-decoration:none!important"
)
UNAVAILABLE_BODY = b"Unavailable\n"
JSON_HEADERS = (
    ("Content-Type", "application/json"),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Robots-Tag", "noindex"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox"),
)
TOO_LARGE_NOTE = (
    "This page is too large to show as a static copy \u2014 open the live page, or comment on the "
    "whole file"
)
CORS_HEADERS = (
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges, ETag"),
)
PREFLIGHT_HEADERS = (
    ("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS"),
    ("Access-Control-Allow-Headers", "Range, If-Range, If-None-Match, If-Match"),
    ("Access-Control-Max-Age", "600"),
)
NOT_FOUND_BODY = b"Not found\n"
_NOT_FOUND_HEADERS = (
    ("Content-Type", "text/plain; charset=utf-8"),
    ("Content-Length", str(len(NOT_FOUND_BODY))),
    ("X-Content-Type-Options", "nosniff"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Robots-Tag", "noindex"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox"),
)


def sandbox_csp(public_origin: str | None, token: str | None) -> str:
    """The sandbox CSP for a bundle file of the share ``token``.

    In the sandbox the page's origin is opaque. WebKit matches ``'self'`` against that opaque origin, so it refuses
    every fetch to the bundle (Chromium and Firefox match ``'self'`` against the page URL). With a configured public
    origin, each fetch directive therefore also names this share's own prefix ``<origin>/s/<token>/``: a CSP path
    source ending in ``/`` matches by prefix, so it covers ``f/...`` and the lab ``k/<session>/f/...`` paths, and
    nothing of any other share. Without one (``public_url`` unset or not a bare https origin) the policy keeps
    ``'self'`` only: the request's Host header is never trusted.
    """
    if not public_origin or not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return CSP
    return _CSP_TEMPLATE.format(self=f"'self' {public_origin}/s/{token}/")


class RateLimiter:
    """Fixed one-minute windows per key, on the injected clock; the key table is bounded."""

    def __init__(
        self, limit: int, now: Callable[[], float], window: float = 60.0, max_keys: int = 4096
    ):
        self.limit, self.now, self.window, self.max_keys = limit, now, window, max_keys
        self._lock = threading.Lock()
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def blocked(self, key: str) -> bool:
        """True when ``key`` has used its whole window (nothing is counted)."""
        now = self.now()
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            return not (now - start >= self.window or now < start) and count >= self.limit

    def allow(self, key: str) -> bool:
        now = self.now()
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self.window or now < start:
                start, count = now, 0
            if count >= self.limit:
                return False
            if key not in self._windows and len(self._windows) >= self.max_keys:
                self._evict(now)
            self._windows[key] = (start, count + 1)
            self._windows.move_to_end(key)
            return True

    def _evict(self, now: float) -> None:
        """Make room: drop the least-recently-used keys that are not blocked; blocked ones are kept."""
        for k in list(self._windows):
            start, count = self._windows[k]
            if not (count >= self.limit and 0 <= now - start < self.window):
                del self._windows[k]
                if len(self._windows) < self.max_keys:
                    return
        # every key is blocked: refuse to grow past the bound by dropping the oldest window
        self._windows.popitem(last=False)


def decode_rel_path(raw: str) -> str | None:
    """The normalised index name for a raw (still percent-encoded) relative path, or None.

    Segments are split on the raw ``/`` first, then each is decoded exactly once as strict UTF-8; a decoded ``/``,
    ``\\`` or NUL, a malformed escape, a dot-segment or anything ``normalise_name`` refuses gives None.
    """
    if (
        not raw
        or len(raw) > MAX_RAW_PATH
        or not RAW_PATH_OK.fullmatch(raw)
        or _BAD_ESCAPE.search(raw)
    ):
        return None
    segments = []
    for segment in raw.split("/"):
        try:
            decoded = unquote_to_bytes(segment).decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
        if "/" in decoded or "\\" in decoded or "\x00" in decoded:
            return None
        segments.append(decoded)
    try:
        return normalise_name("/".join(segments))
    except ShareBundleError:
        return None


def parse_etags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def weak(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def vendor_etag(body: bytes) -> str:
    """Strong validator for a vendored file: its sha256, the same digest SHA256SUMS records."""
    return f'"{hashlib.sha256(body).hexdigest()}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match (weak comparison, ``*`` matches) against one ETag."""
    if if_none_match is None:
        return False
    tags = [weak(t) for t in parse_etags(if_none_match)]
    return "*" in tags or etag in tags


def parse_range(value: str, size: int):
    """None to ignore the header (serve 200), "unsatisfiable" for 416, or an inclusive (start, end) for 206.

    Multi-range requests are answered with the full 200 unless every range is unsatisfiable (416).
    """
    unit, sep, specs = value.partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    ranges = []
    for spec in specs.split(","):
        match = _RANGE_SPEC.fullmatch(spec.strip())
        if not match or (not match.group(1) and not match.group(2)):
            return None
        first, last = match.group(1), match.group(2)
        if first:
            start = int(first)
            end = int(last) if last else size - 1
            if last and int(last) < start:
                return None
            ranges.append((start, min(end, size - 1)) if start < size else "unsatisfiable")
        else:
            n = int(last)
            ranges.append((max(0, size - n), size - 1) if n > 0 and size > 0 else "unsatisfiable")
    if all(r == "unsatisfiable" for r in ranges):
        return "unsatisfiable"
    if len(ranges) > 1:
        return None
    return ranges[0]


def content_headers(
    name: str, etag: str, csp: str = CSP, *, sandbox_pdf: bool = False
) -> list[tuple[str, str]]:
    content_type = content_type_for(name)
    if content_type == "application/pdf":
        # A lab PDF (its URL holds a session) gets the sandbox policy too: a viewer with PDF scripting must not be
        # able to reach the session URL. Public shares keep the plain frame-ancestors policy.
        headers = [
            ("Content-Type", "application/pdf"),
            ("Content-Disposition", "inline"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", csp if sandbox_pdf else PDF_CSP),
        ]
    else:
        headers = [
            ("Content-Type", content_type),
            ("Content-Security-Policy", csp),
            ("X-Content-Type-Options", "nosniff"),
        ]
    headers += [
        ("Cache-Control", "private, no-cache"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Robots-Tag", "noindex"),
        ("ETag", etag),
        ("Accept-Ranges", "bytes"),
    ]
    return headers


def mode_switch_link(depth: int, path: str) -> str:
    """The fixed mode-switch link for the HTML file at the raw request path ``path`` (still percent-encoded).

    ``depth`` is the number of ``../`` steps from the page to the session base: one for the mode segment (``f/``) plus
    one per directory of ``path``. The only bytes we add to bundle content; script-free, built from a constant."""
    return MODE_SWITCH_TEMPLATE.format(href=html.escape("../" * depth + "a/" + path))


# One pass over the bytes (no lower-cased copy). Tokens that can hide a tag: comments and the raw-text / RCDATA
# elements (their bodies run to the matching closer); the three markers we may insert after are `<body`, `<html` and
# the doctype. Anything unterminated (a doctype too, so repeats cannot rescan) runs to the end of the data, so nothing after it is ever a candidate.
_SCAN = re.compile(
    rb"<!--.*?(?:-->|\Z)"
    rb"|<(?P<raw>script|style|textarea|title)(?=[\s/>])(?:[^>]*>)?.*?(?:</(?P=raw)(?=[\s/>])|\Z)"
    rb"|<(?P<tag>body|html)(?=[\s/>])|<(?P<doctype>!doctype)(?:[^>]*>|.*\Z)",
    re.I | re.S,
)
_TAG_END = re.compile(rb"""(?:[^>"']|"[^"]*"|'[^']*')*>""")
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xef\xbb\xbf\x00")


def ascii_compatible(content_type: str, data: bytes) -> bool:
    """False for a response whose bytes are not an ASCII-compatible text (a UTF-16/32 BOM, NUL bytes at the start, or a
    declared charset other than utf-8 / us-ascii): inserting ASCII bytes there would corrupt it."""
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset" and value.strip().strip('"').lower() not in (
            "utf-8",
            "utf8",
            "us-ascii",
        ):
            return False
    return not data.startswith(_WIDE_BOMS) and b"\x00" not in data[:1024]


def _anchor_index(data: bytes) -> int | None:
    """The offset right after the first real ``<body ...>`` start tag (outside comments, script, style, textarea and
    title), else after the first ``<html ...>`` tag, else after the doctype; None when the data has none of them."""
    html_end = doctype_end = None
    for match in _SCAN.finditer(data):
        tag = match.group("tag")
        if tag is not None:
            if tag.lower() == b"html" and html_end is not None:
                continue  # only the first <html> tag can be the anchor; matching its end again would rescan
            end = _TAG_END.match(data, match.end())
            if end is None:
                return None  # an unterminated start tag: nothing safe to anchor on
            if tag.lower() == b"body":
                return end.end()
            if html_end is None:
                html_end = end.end()
        elif (
            match.group("doctype") is not None
            and doctype_end is None
            and data[match.end() - 1 : match.end()] == b">"
        ):
            doctype_end = (
                match.end()
            )  # unterminated: it ran to the end of the data, nothing to anchor on
    return html_end if html_end is not None else doctype_end


# ---- the strict gate: member-link HTML is labelled only when its markup is unambiguous for every HTML5 tokenizer.
# The scanner above reads comments and CDATA differently from a browser, so it cannot be
# trusted to say where the bar lands. Instead of reconciling them, pages containing any construct whose tokenization is
# a browser-specific corner are refused (a uniform 404 at serve time, ``unsupported_html`` at approve).
_AMBIGUOUS = re.compile(
    rb"<!\[|<!-->|<!--->|--!>|<\?|\x00|</(?![A-Za-z])|<frame(?:set)?(?=[\s/>])", re.I
)  # CDATA, abrupt comments, PI, bogus end tags, frames (a frameset page ignores <body>, so it cannot carry the bar)
_BANG = re.compile(rb"<!")
_BANG_OK = re.compile(
    rb"<!(?:--(?:[^-]|-(?!-))*-->|doctype(?=[\s>]))", re.I
)  # a clean comment (no "--" inside) or a doctype
_COMMENT = re.compile(rb"<!--(?:[^-]|-(?!-))*-->")
_WS = re.compile(rb"[ \t\r\n\f]+")
_DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.I)
_ATTRS = (
    rb"(?:[ \t\r\n\f]+[^\s\"'<>/=`]+(?:[ \t\r\n\f]*=[ \t\r\n\f]*(?:\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'<>=`]+))?)*"
    rb"[ \t\r\n\f]*"
)
_START = re.compile(rb"<([A-Za-z][A-Za-z0-9]*)" + _ATTRS + rb"(/?)>")
_HEAD_END = re.compile(rb"</head[ \t\r\n\f]*>", re.I)
_VOID_HEAD = frozenset({b"meta", b"link", b"base"})
_RAW_HEAD = frozenset({b"title", b"style", b"script", b"noscript"})
_NOSCRIPT_BODY = re.compile(
    rb"(?:[ \t\r\n\f]+|<(?:meta|link)"
    + _ATTRS
    + rb"/?>|<style"
    + _ATTRS
    + rb">[^<]*</style[ \t\r\n\f]*>)*",
    re.I,
)


def _well_formed_markup(data: bytes) -> bool:
    """True when ``data`` has none of: ``<![``, ``<!-->``, ``<!--->``, ``--!>``, ``<?``, NUL, ``</`` + a non-letter, a
    ``<!`` that is not ``<!doctype`` or a comment without ``--`` inside it (and ending in ``-->``)."""
    if _AMBIGUOUS.search(data):
        return False
    return all(_BANG_OK.match(data, m.start()) for m in _BANG.finditer(data))


def _raw_element_end(prefix: bytes, pos: int, name: bytes) -> int | None:
    """Offset after the plain closer of the raw-text element whose start tag ends at ``pos``, or None when there is no
    closer, the first ``</name`` is not a plain ``</name>``, or the body holds markup that changes how it ends."""
    closer = re.compile(rb"</" + name + rb"(?=[\s/>])", re.I).search(prefix, pos)
    if closer is None:
        return None
    plain = re.compile(rb"</" + name + rb"[ \t\r\n\f]*>", re.I).match(prefix, closer.start())
    body = prefix[pos : closer.start()]
    if plain is None or (name in (b"script", b"style") and re.search(rb"<!--|<script", body, re.I)):
        return None
    if name == b"noscript" and not _NOSCRIPT_BODY.fullmatch(body):
        return None
    return plain.end()


def _prefix_allowed(prefix: bytes) -> bool:
    """Structural allowlist for the bytes before the injection anchor (``prefix`` ends with the anchor tag itself). Only
    these may appear: whitespace, one doctype (first), well-formed comments, the start tags html and head (once, in
    order) with plain attributes, meta / link / base, and title / style / script / noscript elements with a plain
    closer (noscript only holding meta, link and style), ``</head>``, and finally ``<body ...>`` as the last token.
    Any other element, any text, a stray closer, or a ``/>`` on a non-void element refuses the page, so nothing
    before the anchor can open an element the parser would still have open (or a mode it would stay in) at the bar."""
    pos, size, stage = (
        0,
        len(prefix),
        0,
    )  # stage: 0 nothing yet, 1 after html, 2 in head, 3 after </head>
    seen_doctype = False
    while pos < size:
        if match := _WS.match(prefix, pos) or _COMMENT.match(prefix, pos):
            pos = match.end()
        elif match := _DOCTYPE.match(prefix, pos):
            if seen_doctype or stage:
                return False
            seen_doctype, pos = True, match.end()
        elif match := _HEAD_END.match(prefix, pos):
            if stage != 2:
                return False
            stage, pos = 3, match.end()
        elif match := _START.match(prefix, pos):
            name, slash = match.group(1).lower(), match.group(2)
            pos = match.end()
            if name == b"body":
                return pos == size and not slash
            if name in _VOID_HEAD and stage < 3:
                stage = 2 if stage < 2 else stage
            elif slash:
                return False
            elif name == b"html" and stage == 0:
                stage = 1
            elif name == b"head" and stage < 2:
                stage = 2
            elif name in _RAW_HEAD and stage < 3:
                stage = 2
                end = _raw_element_end(prefix, pos, name)
                if end is None:
                    return False
                pos = end
            else:
                return False
        else:
            return False
    return True


def inject_mode_switch(data: bytes, link: bytes) -> bytes | None:
    """``data`` with ``link`` inserted at ``_anchor_index``; None when there is no anchor (the caller then serves the
    file untouched). Never inserts inside a script string or a comment (but see ``labelled_html`` for the strict check)."""
    cut = _anchor_index(data)
    return None if cut is None else data[:cut] + link + data[cut:]


def labelled_html(data: bytes, content_type: str, bar: bytes, link: bytes = b"") -> bytes | None:
    """``data`` with ``bar`` (and then ``link``) inserted where a browser will render the bar as a top-level element, or
    None when that cannot be shown: non-ASCII-compatible bytes, markup the strict gate calls ambiguous
    (``_well_formed_markup``) or anything but the allowlisted head constructs before the anchor (``_prefix_allowed``).
    A page with no html, head or body anchor at all gets the bar first. Never returns an unlabelled page."""
    if not ascii_compatible(content_type, data):
        return None
    cut = _anchor_index(data)
    cut = 0 if cut is None else cut
    if not _well_formed_markup(data) or not _prefix_allowed(data[:cut]):
        return None
    return data[:cut] + bar + link + data[cut:]


def shared_by_bar(name: str) -> bytes:
    """The fixed, non-dismissable top bar for a member-created link; ``name`` is HTML-escaped."""
    text = f"Shared by {html.escape(name)} \u2014 content was not reviewed by the operator"
    return SHARED_BY_BAR.format(text=text).encode("utf-8")


def expires_text(expires_at: float | None, now: float) -> str:
    """``expires in N days`` (whole days), ``expires in 1 day``, ``expires today``; empty when the share has no expiry."""
    if expires_at is None:
        return ""
    days = int((expires_at - now) // 86400)
    if days < 1:
        return "expires today"
    return "expires in 1 day" if days == 1 else f"expires in {days} days"


def landing_bar(
    sharer: str | None,
    title: str | None,
    expires_at: float | None,
    now: float,
    *,
    token: str,
    signed_in: bool,
    can_sign_in: bool,
    margin_top: int = 0,
) -> bytes:
    """The landing bar markup. Every dynamic part is HTML-escaped; the sign-in link carries no query and no return path."""
    parts = [
        f"Shared by {sharer if isinstance(sharer, str) and sharer.strip() else LANDING_FALLBACK_SHARER}"
    ]
    if isinstance(title, str) and title.strip():
        parts.append(title.strip()[:LANDING_TITLE_MAX])
    left = expires_text(expires_at, now)
    if left:
        parts.append(left)
    text = html.escape(" \u00b7 ".join(parts))
    extra = ""
    if not signed_in and can_sign_in:
        extra = (
            f'<a data-herdr-signin href="/s/{token}/signin" rel="noopener" style="{_LANDING_LINK_STYLE}">'
            f'{html.escape(LANDING_BUTTON)}</a><div data-herdr-notice style="margin:6px 0 0 0!important;'
            f'font-weight:400!important">{html.escape(LANDING_NOTICE)}</div>'
        )
    return (
        f'<div data-herdr-landing role="note" style="{_LANDING_STYLE.format(margin=int(margin_top))}">{text}{extra}'
        "</div>"
    ).encode()


def landing_tag(landing: bytes) -> str:
    """An ETag part for the landing bar: its bytes change with the viewer's state and the days left."""
    return LANDING_ETAG_SUFFIX + hashlib.sha256(landing).hexdigest()[:10]


def _safe_parts(parts: list[str]) -> bool:
    return all(
        p not in ("", ".", "..") and "/" not in p and "\\" not in p and "\x00" not in p
        for p in parts
    )


def open_beneath(
    root: Path, parts: list[str], *, max_size: int | None = None
) -> tuple[int, os.stat_result] | None:
    """Open ``root/parts...`` without following a symlink at ANY component; a regular file, or None."""
    if not parts or not _safe_parts(parts):
        return None
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nfd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd
            )
            os.close(dfd)
            dfd = nfd
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd
        )
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (max_size is not None and st.st_size > max_size):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd, st


def read_all(fd: int, size: int) -> bytes | None:
    """Exactly ``size`` bytes of ``fd`` (``pread``), or None when the file is shorter than promised."""
    chunks, offset = [], 0
    while offset < size:
        chunk = os.pread(fd, min(CHUNK_BYTES, size - offset), offset)
        if not chunk:
            return None
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)
