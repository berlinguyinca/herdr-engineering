"""The database-free public share server (``herdr-share-public``).

Serves frozen share bundles from a read-only volume: ``GET/HEAD /s/<token>/`` redirects to the entry and
``GET/HEAD/OPTIONS /s/<token>/f/<path>`` serves one file listed in the share's ``state.json`` file index. There is no
database: the only inputs are ``<shares_root>/by-token/<sha256(token)>/state.json`` (re-read, or re-stat'ed, on every
request so a revoke or an expiry takes effect on the very next request) and the bundle files under
``<shares_root>/<share_id>/files/``. The only output besides responses is an append-only JSONL audit file.

Security rules (see the plan's Global Constraints and Review Focus):

* Every failure -- unknown/malformed token, revoked/expired/not-live state, wrong audience, traversal or odd encoding,
  a file not in the index, a symlink, a size mismatch, an unsupported method, a malformed request, an internal error --
  returns the SAME 404 bytes with CORS headers and ``Connection: close``. Reasons go only to the events file (when the
  share is known) or a rate-limited log line that never contains the path.
* The request path is checked as printable ASCII, split on ``/`` BEFORE decoding, each segment percent-decoded exactly
  once as strict UTF-8 (a decoded ``/``, ``\\`` or NUL is refused), normalised by ``normalise_name`` and looked up in
  the state's file index. Request paths are never joined onto the filesystem; files are opened by walking the
  index name component by component with ``openat`` and ``O_NOFOLLOW`` and must be regular files of the indexed size.
* Tokens, sessions, CSRF tokens, nonces and Authorization values are never written to events or logs (the stdlib request
  log is disabled).
* No request body is ever read, EXCEPT the three annotation POSTs (below); any other request that announces a body gets
  its response and the connection closed.

Lab shares (``audience: lab``) add, only when the lab settings are complete (else every lab route is the 404):

* ``GET/HEAD /s/<token>/`` and ``/s/callback`` -> the static viewer page; ``/s/static/{share.js,share.css}`` and a
  generated ``/s/static/config.json`` -- a fixed allow-list read from ``static_dir`` with ``O_NOFOLLOW``, served with
  the strict ``VIEWER_CSP`` (no inline script or style) and no CORS.
* ``POST /s/<token>/session`` (body-less; ``Authorization: Bearer <access>`` and ``X-Id-Token``, exactly one each) ->
  ``share_lab.authorise`` against the share's allow-list, then a ``share_session`` AEAD session (identity and email encrypted inside) capped at the earliest of
  ``session_seconds``, the share's expiry and the access token's ``exp``. Rate-limited per share and globally. OPTIONS
  on it is the 404, so a cross-origin page cannot get a preflight for the ``Authorization`` header.
* ``GET/HEAD/OPTIONS /s/<token>/k/<session>/f/<path>`` -> the session is checked against the share's CURRENT state on
  every request (a revoke or expiry kills it at once), then the file is served exactly like a public one. For a
  document navigation of an HTML file of a lab share with annotations enabled, one fixed mode-switch link is injected
  (``mode_switch_link``); every other response is byte-for-byte the raw file.

The lab route table (``_ROUTES``; everything else, every other method and every OPTIONS on the ``v/``, ``a/``,
annotation and ``session`` routes is the uniform 404; POST is accepted ONLY for ``session`` and the two annotation POST
routes). Path parts are ``['', 's', token, 'k', session, ...]``:

====================================================  =============  ================================================
path after ``/s/<token>/``                            methods        match
====================================================  =============  ================================================
``session``                                           POST           ``len == 4``
``k/<session>/f/<path>``                              GET, HEAD      ``len >= 7``, ``parts[5] == 'f'``
``k/<session>/v/<path>``, ``k/<session>/a/<path>``    GET, HEAD      ``len >= 7``, ``parts[5] in ('v', 'a')``
``k/<session>/annotations``                           GET, POST      ``len == 6``, ``parts[5] == 'annotations'``
``k/<session>/annotations/<aid>/edit|delete``         POST           ``len == 8``, ``parts[5] == 'annotations'``,
                                                                     ``parts[7] in ('edit', 'delete')``
====================================================  =============  ================================================

* ``v/<path>`` is the live view of a non-HTML file, built from our own template (``share_static/view.html``) under a
  nonce CSP; it is sent without CORS. ``a/<path>`` is the annotation view of ANY kind (``ANNOTATE_CSP``: per-response
  nonce + ``strict-dynamic``, same-origin fetches only, no frames). HTML is sanitised in the isolated render child and
  mounted in a declarative shadow root inside ``#herdr-viewport`` (``contain: paint``, ``overflow: clip``), so neither
  its markup, ids nor styles reach our mode bar, panel, data node or the CSRF meta, which is delivered ONLY in this
  page. It needs annotations enabled; otherwise the uniform 404.
* The annotations GET needs the same write cookie (a stolen session URL alone reads nothing). The ``a/`` page, ``f/`` and
  ``v/`` do not: a navigation out of the opaque-origin ``f/`` page is cross-site and arrives without it.
* The annotation POSTs are the only routes that read a body. A POST is refused (uniform 404, reason ``annotate_refused``)
  unless the session is valid and the share's allow-list still admits its email (``share_lab.still_admitted``: an email
  removed from ``allow_emails`` stops writing at once when the session's MAC'd admission basis is ``e``; a session
  admitted by a group claim (basis ``g``) needs the share to still have ``allow_groups``, but a removed group MEMBER
  keeps writing until the session expires), ``Origin`` equals the share origin exactly, the CSRF
  header passes a constant-time check, the write cookie (set by the ``session`` POST: HttpOnly, Secure, SameSite=Strict,
  ``Path=/s/<token>/``, a MAC over the session under its own domain label) is present and valid, ``Content-Type`` is exactly ``application/json`` and there is exactly one ``Content-Length`` of 1 to
  32768 bytes and no ``Transfer-Encoding``. Accepted requests append one line to ``annotations.jsonl`` for the worker;
  author identity and email come from the session only. Edits and deletes are decided against the viewer's own
  read-back projection, so an annotation is editable only after the worker has ingested it.
  A 201 or 202 means the request passed EDGE validation and its line was appended -- nothing more: the worker may still
  deny it at ingest (caps, an unpublished version, a withdrawn share), and the viewer sees the outcome only through
  the read-back projection. The lines are ASCII and refused (503) above ``ANNOTATION_LINE_MAX``.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes, urlsplit

from ..https_origin import HOST_PORT as _HOST
from ..https_origin import https_origin as _https_origin
from .annotation_anchor import (
    ANNOTATION_ID_RE,
    MAX_BODY_BYTES,
    AnnotationInputError,
    annotation_settings,
    parse_annotation_request,
    parse_delete_request,
    parse_edit_request,
)
from .collab_bridge_files import _open_beneath
from .collab_cognito import CognitoAuthenticator, SyncedDirectory
from .share_bundle import MAX_PROJECTION_BYTES, ShareBundleError, content_type_for, normalise_name
from .share_lab import authorise, still_admitted
from .share_render import (
    MAX_RENDER_INPUT,
    RESOLVER_VERSION,
    RenderBusy,
    RenderCache,
    RenderFailed,
    RenderTooLarge,
    render_kind,
    render_text,
    sanitize_html,
    share_resolver,
    wrap_content,
)
from .share_rich_view import MAX_RICH_BYTES, RICH_NAME, SHARE_RICH_HEADERS
from .share_session import (
    MIN_SECRET_BYTES,
    check_csrf,
    check_session,
    check_write_cookie,
    csrf_token,
    mint_session,
    write_cookie_header,
)

log = logging.getLogger("herdr.share_public")

CHUNK_BYTES = 1024 * 1024
WRITE_SLICE_BYTES = 64 * 1024
EVENTS_MAX_BYTES = 8 * 1024 * 1024
STATE_MAX_BYTES = 64 * 1024 * 1024
DENIED_PER_MINUTE = 20          # denied events per (share, reason) per minute (a scanner cannot fill the events file)
OPENED_PER_MINUTE = 60          # opened events per share per minute
LOG_PER_MINUTE = 20             # log lines per reason per minute
MAX_CONNECTIONS = 128
MAX_RAW_PATH = 8192
STATE_CACHE_SIZE = 256
SESSION_PER_SHARE_PER_MINUTE = 30   # session attempts per share per minute (blunts token guessing)
SESSION_GLOBAL_PER_MINUTE = 600     # verified session attempts across all lab shares per minute (bounds JWT work)
STATIC_MAX_BYTES = 1024 * 1024
VENDOR_MAX_BYTES = 4 * 1024 * 1024   # the vendored pdf.js worker is ~1.4 MB
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "share_static"
SHARED_BY_MAX = 120      # ``state.json`` ``shared_by``: the creator's display name on a member-created link (artifact_share_api)
VIEWER_FILE = "viewer.html"
STATIC_FILES = {"lab-signin.js": "text/javascript; charset=utf-8", "share.js": "text/javascript; charset=utf-8", "share.css": "text/css; charset=utf-8",
                "annotate.js": "text/javascript; charset=utf-8", "anchor.js": "text/javascript; charset=utf-8",
                "annotate.css": "text/css; charset=utf-8"}
VIEW_FILE = "view.html"
ANNOTATIONS_MAX_BYTES = 32 * 1024 * 1024     # the annotations file cap (the worker rotates it long before)
ANNOTATION_LINE_MAX = 65536                  # the worker's max_line_bytes: a longer line would be dropped at ingest
MODE_SWITCH_MAX_BYTES = 2 * 1024 * 1024      # larger HTML files are served untouched (streamed), without the link
MODE_SWITCH_ETAG_SUFFIX = "-m1"
NONCE_BYTES = 18                             # token_urlsafe(18) is 24 characters
# One level below static/vendor/pdfjs/: exactly the two vendored pdf.js modules (the licence and SHA256SUMS stay unserved).
VENDOR_FILES = {"pdf.min.mjs": "text/javascript; charset=utf-8", "pdf.worker.min.mjs": "text/javascript; charset=utf-8"}
# The URL carries no version, so a bump keeps the path: browsers revalidate every time (no-cache) against a strong
# ETag that is the file's sha256 (the SHA256SUMS value), and get a 304 while it is unchanged.
VENDOR_CACHE = "no-cache"

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_SHARE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RAW_PATH_OK = re.compile(r"[\x21-\x5b\x5d-\x7e]*")   # printable ASCII without space and backslash
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RANGE_SPEC = re.compile(r"(\d{0,19})-(\d{0,19})")
_SESSION_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,768}")
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,6}")
_VIEW_PLACEHOLDER = re.compile(r"\{\{(NONCE|MODE|CSRF_META|SWITCH|DATA_JSON|PANEL|CONTENT|BANNER)\}\}")

_CSP_TEMPLATE = ("sandbox allow-scripts allow-downloads; default-src {self}; script-src {self} 'unsafe-inline' "
                 "'wasm-unsafe-eval'; style-src {self} 'unsafe-inline'; img-src {self} data: blob:; font-src {self} data:; "
                 "connect-src {self}; worker-src {self} blob:; object-src 'none'; base-uri 'self'; form-action 'none'; "
                 "frame-ancestors 'none'")
CSP = _CSP_TEMPLATE.format(self="'self'")   # without a configured public origin
PDF_CSP = "frame-ancestors 'none'"
VIEWER_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' {origin}; img-src 'self' data:; "
              "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
VIEW_CSP = ("default-src 'none'; script-src 'nonce-{nonce}' 'strict-dynamic'; style-src-elem {origin}/s/static/ "
            "{origin}/s/{token}/ 'unsafe-inline'; style-src-attr 'unsafe-inline'; img-src {origin}/s/{token}/ data: blob:; "
            "font-src {origin}/s/{token}/ {origin}/s/static/ data:; connect-src {origin}/s/{token}/; "
            "worker-src {origin}/s/static/vendor/pdfjs/; frame-src 'none'; child-src 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# The annotation view: the plan's view policy (``VIEW_CSP``), path-scoped to this share, with these deviations.
#   * img-src has NO ``blob:`` (the plan's ``data: blob:``): sanitised content images are same-share ``f/`` URLs or
#     ``data:`` images, and pdf.js draws on canvases, so nothing needs a blob URL.
#   * ``style-src-elem`` and ``style-src-attr`` keep ``'unsafe-inline'`` (as the plan does): the sanitiser keeps content
#     <style> elements and style attributes inside the shadow root and they cannot carry our nonce. A style cannot run
#     code, and no remote origin is allowed in either directive (an @import of a remote sheet cannot load).
#   * font-src keeps ``data:`` (plan); there is no ``style-src`` (the two split directives cover every style use).
# Verified in a real browser by tests/playwright/annotate-view.test.mjs.
ANNOTATE_CSP = VIEW_CSP.replace("img-src {origin}/s/{token}/ data: blob:;", "img-src {origin}/s/{token}/ data:;")
PDFJS_CONFIG = {"module": "/s/static/vendor/pdfjs/pdf.min.mjs", "worker": "/s/static/vendor/pdfjs/pdf.worker.min.mjs",
                "options": {"isEvalSupported": False, "enableXfa": False}}
MODE_SWITCH_TEMPLATE = (
    '<a data-herdr-mode-switch href="{href}" rel="noopener" style="position:fixed;right:12px;bottom:12px;'
    'z-index:2147483647;display:block;margin:0;padding:6px 12px;font:600 14px/1.4 system-ui,sans-serif;color:#fff;'
    'background:#1f4fd1;border-radius:6px;text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,.4)">Annotate</a>')
SHARED_BY_BAR = (
    '<div data-herdr-shared-by role="note" style="position:fixed!important;top:0!important;left:0!important;'
    'right:0!important;z-index:2147483647!important;display:block!important;visibility:visible!important;'
    'opacity:1!important;margin:0!important;padding:8px 12px!important;font:600 14px/1.4 system-ui,sans-serif!important;'
    'color:#4a3b00!important;background:#fff3cd!important;border-bottom:1px solid #b8a04a!important;'
    'text-align:center!important;transform:none!important;clip:auto!important;height:auto!important">{text}</div>')
SHARED_BY_ETAG_SUFFIX = "-b1"
UNAVAILABLE_BODY = b"Unavailable\n"
JSON_HEADERS = (("Content-Type", "application/json"), ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex"),
                ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox"))
TOO_LARGE_NOTE = ("This page is too large to show as a static copy \u2014 open the live page, or comment on the "
                  "whole file")
CORS_HEADERS = (("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges, ETag"))
PREFLIGHT_HEADERS = (("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS"),
                     ("Access-Control-Allow-Headers", "Range, If-Range, If-None-Match, If-Match"),
                     ("Access-Control-Max-Age", "600"))
NOT_FOUND_BODY = b"Not found\n"
_NOT_FOUND_HEADERS = (("Content-Type", "text/plain; charset=utf-8"),
                      ("Content-Length", str(len(NOT_FOUND_BODY))),
                      ("X-Content-Type-Options", "nosniff"),
                      ("Cache-Control", "no-store"),
                      ("Referrer-Policy", "no-referrer"),
                      ("X-Robots-Tag", "noindex"),
                      ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox"))
_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


@dataclass
class ShareConfig:
    shares_root: Path
    events_path: Path
    sync_dir: Path | None = None
    pool_id: str | None = None
    client_ids: tuple[str, ...] = ()
    web_client_id: str | None = None
    cognito_domain: str | None = None
    public_url: str | None = None
    signing_secret: bytes = field(default=b"", repr=False)
    session_seconds: int = 14400
    static_dir: Path | None = None
    annotations: dict = field(default_factory=lambda: annotation_settings(None))   # control_plane.share.annotations
    annotations_path: Path | None = None        # default: ``annotations.jsonl`` beside the events file
    public_host: str | None = None              # control_plane.share.public_host: /s/ is served on this Host only (D9)

    def __post_init__(self) -> None:
        if self.public_host is not None:
            from .members_config import valid_public_host
            if not valid_public_host(self.public_host):
                raise ValueError("public_host must be a lowercase DNS name (no scheme, port or path)")
        if self.annotations_path is None:
            self.annotations_path = Path(self.events_path).with_name("annotations.jsonl")


@dataclass(frozen=True)
class LabSettings:
    """Everything the lab routes need, validated once at start-up (None in the server when lab is not configured)."""

    authenticator: CognitoAuthenticator
    secret: bytes = field(repr=False)
    session_seconds: int
    static_dir: Path
    viewer_csp: str
    config_json: bytes


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


def lab_settings(config: ShareConfig) -> LabSettings | None:
    """The validated lab settings, or None (fail closed) when anything required is missing or malformed."""
    secret = config.signing_secret
    if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
        return None
    origin = _https_origin(config.cognito_domain)
    public = config.public_url
    if (origin is None or not isinstance(public, str) or not config.sync_dir or not isinstance(config.pool_id, str)
            or not isinstance(config.web_client_id, str) or not _CLIENT_ID.fullmatch(config.web_client_id)):
        return None
    public_parts = urlsplit(public.strip())
    if (public_parts.scheme != "https" or public_parts.query or public_parts.fragment or public_parts.username
            or not _HOST.fullmatch(public_parts.netloc.lower())):
        return None
    try:
        seconds = int(config.session_seconds)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    client_ids = [c for c in (*config.client_ids, config.web_client_id) if isinstance(c, str) and _CLIENT_ID.fullmatch(c)]
    directory = SyncedDirectory(str(config.sync_dir), "share-lab")      # only jwks() is used: fail closed if absent
    try:
        authenticator = CognitoAuthenticator(pool_id=config.pool_id, client_ids=client_ids, required_group="share-lab",
                                             jwks_fetch=directory.jwks, open_signup=True)
    except ValueError:
        return None
    config_json = json.dumps({"cognito_domain": origin, "client_id": config.web_client_id,
                              "redirect_uri": public.strip().rstrip("/") + "/s/callback"},
                             sort_keys=True, ensure_ascii=True).encode("ascii")
    return LabSettings(authenticator=authenticator, secret=secret, session_seconds=seconds,
                       static_dir=Path(config.static_dir) if config.static_dir else DEFAULT_STATIC_DIR,
                       viewer_csp=VIEWER_CSP.format(origin=origin), config_json=config_json)


class ShareState(dict):
    """A validated, live ``state.json``; ``index`` maps each normalised file name to its index entry."""

    index: dict[str, dict]


# ----------------------------------------------------------------------------- rate limiting, events, logging

class RateLimiter:
    """Fixed one-minute windows per key, on the injected clock; the key table is bounded."""

    def __init__(self, limit: int, now: Callable[[], float], window: float = 60.0, max_keys: int = 4096):
        self.limit, self.now, self.window, self.max_keys = limit, now, window, max_keys
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> bool:
        now = self.now()
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self.window or now < start:
                start, count = now, 0
            if count >= self.limit:
                return False
            if key not in self._windows and len(self._windows) >= self.max_keys:
                self._windows.clear()
            self._windows[key] = (start, count + 1)
            return True


class EventLog:
    """Append-only JSONL audit lines for the worker's ingest: one ``os.write`` per line under ``O_APPEND``.

    The path is opened (``O_CREAT``) per line, so when the worker rotates the file (renames it to ``events.jsonl.1``
    once ingested past ``artifact_shares.EVENTS_ROTATE_BYTES``) the next line starts a fresh file. Over
    ``EVENTS_MAX_BYTES`` (only reached when the worker is not rotating) lines are dropped and counted, with a
    rate-limited warning. Lines only ever carry the share id, a kind, an actor, a timestamp and fixed reason codes or
    index names.
    """

    def __init__(self, path: Path, now: Callable[[], float]):
        self.path = Path(path)
        self.now = now
        self.dropped = 0
        self._lock = threading.Lock()
        self._denied = RateLimiter(DENIED_PER_MINUTE, now)
        self._opened = RateLimiter(OPENED_PER_MINUTE, now)

    def opened(self, share_id: str, actor: str, detail: dict) -> None:
        if self._opened.allow(share_id):
            self.append(share_id, "opened", actor, detail)

    def denied(self, share_id: str, actor: str, reason: str) -> None:
        if self._denied.allow(f"{share_id}\x00{reason}"):   # per share: one link cannot silence the others
            self.append(share_id, "denied", actor, {"reason": reason})

    def append(self, share_id: str, kind: str, actor: str, detail: dict) -> None:
        at = datetime.fromtimestamp(self.now(), UTC).isoformat()
        line = (json.dumps({"share_id": share_id, "kind": kind, "actor": actor[:200], "at": at, "detail": detail},
                           sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        with self._lock:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
            except OSError:
                self._drop("events file unavailable")
                return
            try:
                self._readable(fd)
                if os.fstat(fd).st_size + len(line) > EVENTS_MAX_BYTES:
                    self._drop("events file is full")
                    return
                if os.write(fd, line) != len(line):
                    self._drop("short write to events file")
            except OSError:
                self._drop("events file write failed")
            finally:
                os.close(fd)

    @staticmethod
    def _readable(fd: int) -> None:
        """The ingesting worker runs as another uid: make the file 0644 whatever the umask or an older mode was."""
        try:
            st = os.fstat(fd)
            if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) != 0o644:
                os.fchmod(fd, 0o644)
        except PermissionError:
            pass

    def _drop(self, why: str) -> None:
        self.dropped += 1
        _log(why, f"dropped={self.dropped}")


class AnnotationLog:
    """Append-only ``annotations.jsonl`` for the worker's ingest: one ``os.write`` per ASCII line under ``O_APPEND``.

    Same rotation story as ``EventLog`` (the path is opened per line, so the worker's rename starts a fresh file).
    ``append`` returns False -- and writes nothing -- when the line is over ``ANNOTATION_LINE_MAX`` (the worker would
    drop it), the file is over ``ANNOTATIONS_MAX_BYTES``, or the file cannot be opened or written; the caller answers
    503 and the viewer keeps their text. Never logs a record."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.dropped = 0
        self._lock = threading.Lock()

    def append(self, record: dict) -> bool:
        try:
            line = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        except (TypeError, ValueError):
            return self._drop("annotation not serialisable")
        if len(line) > ANNOTATION_LINE_MAX:
            return self._drop("annotation line is over the line cap")
        with self._lock:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
            except OSError:
                return self._drop("annotations file unavailable")
            try:
                EventLog._readable(fd)
                if os.fstat(fd).st_size + len(line) > ANNOTATIONS_MAX_BYTES:
                    return self._drop("annotations file is full")
                if os.write(fd, line) != len(line):
                    return self._drop("short write to annotations file")
                return True
            except OSError:
                return self._drop("annotations file write failed")
            finally:
                os.close(fd)

    def _drop(self, why: str) -> bool:
        self.dropped += 1
        _log(why, f"dropped={self.dropped}")
        return False


_log_limiter = RateLimiter(LOG_PER_MINUTE, time.monotonic)


def _log(reason: str, extra: str = "") -> None:
    """A rate-limited warning carrying a fixed reason code only (never a path, token or header value)."""
    if _log_limiter.allow(reason):
        log.warning("share-public: %s %s", reason, extra)


def _record_event(append, *args) -> None:
    """Run one event-log append (``events.opened`` / ``events.denied``); a failure is logged and never reaches the
    reader (the audit trail must not break serving)."""
    try:
        append(*args)
    except Exception:
        _log("event append failed")


def _write_body(handler, body: bytes) -> None:
    """Write ``body`` in slices; a client that went away closes the connection instead of raising."""
    try:
        view = memoryview(body)
        for at in range(0, len(view), WRITE_SLICE_BYTES):
            handler.wfile.write(view[at:at + WRITE_SLICE_BYTES])
    except (OSError, ValueError):
        handler.close_connection = True


# safe filesystem access: ``_open_beneath`` lives in ``collab_bridge_files`` (shared with the collab artifact bridge)

@dataclass(frozen=True)
class _Prepared:
    """A parsed ``state.json`` with everything that depends only on its content already checked (the cached unit)."""

    raw: dict
    state: ShareState | None
    reason: str
    expires: float | None


_cache_lock = threading.Lock()
_state_cache: OrderedDict[tuple, _Prepared] = OrderedDict()


def _read_state(root: Path, token: str) -> _Prepared | None:
    """The prepared ``state.json`` for ``token`` (any state), or None.

    Opens and fstat's the file on EVERY call (so a revoke or a republish is seen by the very next request); only the
    parse and the content checks are cached, keyed by (dev, inode, mtime, ctime, size).
    """
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    opened = _open_beneath(Path(root), ["by-token", token_hash, "state.json"], max_size=STATE_MAX_BYTES)
    if opened is None:
        return None
    fd, st = opened
    try:
        key = (str(root), token_hash, st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
        with _cache_lock:
            cached = _state_cache.get(key)
            if cached is not None:
                _state_cache.move_to_end(key)
                return cached
        chunks, remaining = [], st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, CHUNK_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            raw = json.loads(b"".join(chunks))
        except ValueError:
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    if not isinstance(raw, dict):
        return None
    prepared = _prepare(raw)
    with _cache_lock:
        _state_cache[key] = prepared
        while len(_state_cache) > STATE_CACHE_SIZE:
            _state_cache.popitem(last=False)
    return prepared


def _expiry(value) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expires_at is not a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:          # tz-naive values are read as UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _build_index(files) -> dict[str, dict]:
    if not isinstance(files, list):
        raise ValueError("files is not a list")
    index: dict[str, dict] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("file entry is not an object")
        name, sha, size = item.get("name"), item.get("sha256"), item.get("size")
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ValueError("file entry has no sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("file entry has no size")
        name = normalise_name(name)
        if name in index:
            raise ValueError("duplicate file entry")
        index[name] = {"name": name, "sha256": sha, "size": size}
    return index


def _prepare(raw: dict) -> _Prepared:
    """Every check that does not depend on the clock: live, well formed, a public share has an expiry."""
    def bad(reason: str) -> _Prepared:
        return _Prepared(raw, None, reason, None)

    if raw.get("state") != "live":
        return bad("not_live")
    try:
        expires = _expiry(raw.get("expires_at"))
    except (ValueError, TypeError, OverflowError):
        return bad("invalid_state")
    if expires is None and raw.get("audience") == "public":
        return bad("invalid_state")       # public shares always expire: fail closed
    share_id = raw.get("share_id")
    if (not isinstance(share_id, str) or not _SHARE_ID.fullmatch(share_id) or share_id == "by-token"
            or raw.get("audience") not in ("public", "lab")):
        return bad("invalid_state")
    try:
        index = _build_index(raw.get("files"))
        entry = normalise_name(raw.get("entry"))
    except (ValueError, ShareBundleError, TypeError):
        return bad("invalid_state")
    if entry not in index:
        return bad("invalid_state")
    rich = raw.get("rich")
    if "rich" in raw and (rich != RICH_NAME or rich not in index or rich == entry or index[rich]["size"] > MAX_RICH_BYTES):
        return bad("invalid_state")       # the author page is one fixed, bounded, non-entry file or nothing
    shared_by = raw.get("shared_by")
    if "shared_by" in raw and (not isinstance(shared_by, str) or not shared_by or len(shared_by) > SHARED_BY_MAX):
        return bad("invalid_state")       # the banner name is ours to bound: a bad one fails closed like any bad state
    state = ShareState(raw)
    state["entry"] = entry
    state.index = index
    return _Prepared(raw, state, "", expires)


def _check_state(prepared: _Prepared, now: float) -> tuple[ShareState | None, str]:
    """(state, "") for a live, unexpired, well-formed share; else (None, reason code). The expiry is checked here, per call."""
    if prepared.state is None:
        return None, prepared.reason
    if prepared.expires is not None and now >= prepared.expires:
        return None, "expired"
    state = ShareState(prepared.state)      # a per-request copy: callers cannot alter the cached state
    state.index = prepared.state.index      # shared, read-only by convention
    return state, ""


def load_state(root: Path, token: str, now: float) -> ShareState | None:
    """The share's state when ``token`` is well formed and its state is ``live``, unexpired and well formed; else None."""
    prepared = _read_state(root, token)
    if prepared is None:
        return None
    return _check_state(prepared, now)[0]


# ----------------------------------------------------------------------------- request paths

def decode_rel_path(raw: str) -> str | None:
    """The normalised index name for a raw (still percent-encoded) relative path, or None.

    Segments are split on the raw ``/`` first, then each is decoded exactly once as strict UTF-8; a decoded ``/``,
    ``\\`` or NUL, a malformed escape, a dot-segment or anything ``normalise_name`` refuses gives None.
    """
    if not raw or len(raw) > MAX_RAW_PATH or not _RAW_PATH_OK.fullmatch(raw) or _BAD_ESCAPE.search(raw):
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


def _parse_etags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _weak(tag: str) -> str:
    return tag[2:] if tag.startswith("W/") else tag


def vendor_etag(body: bytes) -> str:
    """Strong validator for a vendored file: its sha256, the same digest SHA256SUMS records."""
    return f'"{hashlib.sha256(body).hexdigest()}"'


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """If-None-Match (weak comparison, ``*`` matches) against one ETag."""
    if if_none_match is None:
        return False
    tags = [_weak(t) for t in _parse_etags(if_none_match)]
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


# ----------------------------------------------------------------------------- responses

def _send_headers(handler, status: int, headers, *, cors: bool = True, close: bool = False) -> None:
    handler._headers_started = True
    handler.send_response_only(status)
    handler.send_header("Server", handler.version_string())
    handler.send_header("Date", handler.date_time_string())
    for name, value in headers:
        handler.send_header(name, value)
    if not any(name.lower() == "referrer-policy" for name, _ in headers):
        handler.send_header("Referrer-Policy", "no-referrer")     # every response: the URL path may carry a session
    if cors:
        for name, value in CORS_HEADERS:
            handler.send_header(name, value)
    if close or handler.close_connection:
        handler.close_connection = True
        handler.send_header("Connection", "close")
    handler.end_headers()


def send_not_found(handler) -> None:
    """The uniform 404: identical bytes for every failure, CORS headers, ``Connection: close``."""
    if getattr(handler, "request_version", "HTTP/0.9") == "HTTP/0.9":
        handler.request_version = "HTTP/1.0"     # always emit headers (no header-less HTTP/0.9 responses)
    handler.close_connection = True
    _send_headers(handler, 404, _NOT_FOUND_HEADERS, close=True)
    if getattr(handler, "command", None) != "HEAD":
        try:
            handler.wfile.write(NOT_FOUND_BODY)
        except _DISCONNECTS:
            pass
        except OSError:
            pass


def deny(handler, state: dict | None, reason: str, actor: str = "public") -> None:
    """Send the uniform 404 and record ``reason``: an event when the share is known, a rate-limited log line always."""
    _log("denied", reason)
    share_id = state.get("share_id") if isinstance(state, dict) else None
    if isinstance(share_id, str) and _SHARE_ID.fullmatch(share_id):
        _record_event(handler.server.events.denied, share_id, actor, reason)
    send_not_found(handler)


def _content_headers(name: str, etag: str, csp: str = CSP, *, sandbox_pdf: bool = False) -> list[tuple[str, str]]:
    content_type = content_type_for(name)
    if content_type == "application/pdf":
        # A lab PDF (its URL holds a session) gets the sandbox policy too: a viewer with PDF scripting must not be
        # able to reach the session URL. Public shares keep the plain frame-ancestors policy.
        headers = [("Content-Type", "application/pdf"), ("Content-Disposition", "inline"),
                   ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", csp if sandbox_pdf else PDF_CSP)]
    else:
        headers = [("Content-Type", content_type), ("Content-Security-Policy", csp),
                   ("X-Content-Type-Options", "nosniff")]
    headers += [("Cache-Control", "private, no-cache"), ("Referrer-Policy", "no-referrer"),
                ("X-Robots-Tag", "noindex"), ("ETag", etag), ("Accept-Ranges", "bytes")]
    return headers


def _announces_body(handler) -> bool:
    """True unless the request provably has no body: no Transfer-Encoding header at all (even empty), and either no
    Content-Length or exactly one whose value is exactly ``0``. Every occurrence is checked (``get_all``), so a
    duplicate or conflicting header cannot smuggle a second request past the close-on-body rule."""
    headers = handler.headers
    if headers is None:
        return True
    if getattr(headers, "defects", None):
        return True      # a malformed header line stops the parser early: later framing headers would be hidden
    if headers.get_all("Transfer-Encoding"):
        return True
    lengths = headers.get_all("Content-Length") or []
    return bool(lengths) and lengths != ["0"]


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
    rb"|<(?P<tag>body|html)(?=[\s/>])|<(?P<doctype>!doctype)(?:[^>]*>|.*\Z)", re.I | re.S)
_TAG_END = re.compile(rb"""(?:[^>"']|"[^"]*"|'[^']*')*>""")
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xef\xbb\xbf\x00")


def _ascii_compatible(content_type: str, data: bytes) -> bool:
    """False for a response whose bytes are not an ASCII-compatible text (a UTF-16/32 BOM, NUL bytes at the start, or a
    declared charset other than utf-8 / us-ascii): inserting ASCII bytes there would corrupt it."""
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset" and value.strip().strip('"').lower() not in ("utf-8", "utf8", "us-ascii"):
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
                continue         # only the first <html> tag can be the anchor; matching its end again would rescan
            end = _TAG_END.match(data, match.end())
            if end is None:
                return None                      # an unterminated start tag: nothing safe to anchor on
            if tag.lower() == b"body":
                return end.end()
            if html_end is None:
                html_end = end.end()
        elif match.group("doctype") is not None and doctype_end is None and data[match.end() - 1 : match.end()] == b">":
            doctype_end = match.end()  # unterminated: it ran to the end of the data, nothing to anchor on
    return html_end if html_end is not None else doctype_end


# ---- the strict gate: member-link HTML is labelled only when its markup is unambiguous for every HTML5 tokenizer.
# The scanner above reads comments and CDATA differently from a browser, so it cannot be
# trusted to say where the bar lands. Instead of reconciling them, pages containing any construct whose tokenization is
# a browser-specific corner are refused (a uniform 404 at serve time, ``unsupported_html`` at approve).
_AMBIGUOUS = re.compile(rb"<!\[|<!-->|<!--->|--!>|<\?|\x00|</(?![A-Za-z])|<frame(?:set)?(?=[\s/>])", re.I)        # CDATA, abrupt comments, PI, bogus end tags, frames (a frameset page ignores <body>, so it cannot carry the bar)
_BANG = re.compile(rb"<!")
_BANG_OK = re.compile(rb"<!(?:--(?:[^-]|-(?!-))*-->|doctype(?=[\s>]))", re.I)     # a clean comment (no "--" inside) or a doctype
_COMMENT = re.compile(rb"<!--(?:[^-]|-(?!-))*-->")
_WS = re.compile(rb"[ \t\r\n\f]+")
_DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.I)
_ATTRS = (rb"(?:[ \t\r\n\f]+[^\s\"'<>/=`]+(?:[ \t\r\n\f]*=[ \t\r\n\f]*(?:\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'<>=`]+))?)*"
          rb"[ \t\r\n\f]*")
_START = re.compile(rb"<([A-Za-z][A-Za-z0-9]*)" + _ATTRS + rb"(/?)>")
_HEAD_END = re.compile(rb"</head[ \t\r\n\f]*>", re.I)
_VOID_HEAD = frozenset({b"meta", b"link", b"base"})
_RAW_HEAD = frozenset({b"title", b"style", b"script", b"noscript"})
_NOSCRIPT_BODY = re.compile(rb"(?:[ \t\r\n\f]+|<(?:meta|link)" + _ATTRS + rb"/?>|<style" + _ATTRS + rb">[^<]*</style[ \t\r\n\f]*>)*",
                            re.I)


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
    body = prefix[pos:closer.start()]
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
    pos, size, stage = 0, len(prefix), 0       # stage: 0 nothing yet, 1 after html, 2 in head, 3 after </head>
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
    if not _ascii_compatible(content_type, data):
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


def _read_all(fd: int, size: int) -> bytes | None:
    """Exactly ``size`` bytes of ``fd`` (``pread``), or None when the file is shorter than promised."""
    chunks, offset = [], 0
    while offset < size:
        chunk = os.pread(fd, min(CHUNK_BYTES, size - offset), offset)
        if not chunk:
            return None
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def serve_bundle_file(handler, state: dict, rel_path: str, *, cors: bool = True, actor: str = "public",
                      token: str | None = None, mode_switch: str | None = None, sandbox_pdf: bool = False) -> bool:
    """Serve ``rel_path`` (raw, still percent-encoded) from ``state``'s bundle; True when served, False after a 404.

    ``state`` is what ``load_state`` returned. Handles HEAD, If-Match (412), If-None-Match (304), If-Range and a single
    Range (206/416); a multi-range request gets the full 200. Streams in ``CHUNK_BYTES`` chunks with ``os.pread``.
    ``token`` (the share's link token) scopes the sandbox CSP's explicit source; see ``sandbox_csp``.
    ``mode_switch`` (computed by the router, only for a lab document navigation of an HTML file) is the link to inject:
    the response is then the full 200 of the modified bytes under the ``-m1`` ETag, ``Vary: Sec-Fetch-Dest``, no
    ``Accept-Ranges`` and any ``Range`` ignored. HTML over ``MODE_SWITCH_MAX_BYTES`` is served unmodified.
    """
    index = getattr(state, "index", None)
    if index is None:
        try:
            index = _build_index(state.get("files"))
        except (ValueError, ShareBundleError, TypeError):
            deny(handler, state, "invalid_state", actor)
            return False
    name = decode_rel_path(rel_path)
    if name is None:
        deny(handler, state, "bad_path", actor)
        return False
    item = index.get(name)
    if item is None:
        deny(handler, state, "not_in_bundle", actor)
        return False
    share_id = state.get("share_id")
    if not isinstance(share_id, str) or not _SHARE_ID.fullmatch(share_id) or share_id == "by-token":
        deny(handler, None, "invalid_state", actor)
        return False
    root = handler.server.config.shares_root
    opened = _open_beneath(Path(root), [share_id, "files", *name.split("/")])
    if opened is None:
        deny(handler, state, "missing_file", actor)
        return False
    fd, st = opened
    try:
        size = item["size"]
        if st.st_size != size:
            deny(handler, state, "size_mismatch", actor)
            return False
        if name == state.get("rich"):
            return _serve_rich(handler, state, item, fd, actor)
        etag = f'"{item["sha256"]}"'
        csp = sandbox_csp(getattr(handler.server, "public_origin", None), token)
        injected = None
        is_html = content_type_for(name).split(";")[0].strip() == "text/html"
        shared_by = state.get("shared_by") if is_html else None
        bar = shared_by_bar(shared_by) if isinstance(shared_by, str) else b""
        if is_html and (bar or mode_switch is not None):
            if size > MODE_SWITCH_MAX_BYTES:
                if bar:      # a labelled page is never served unlabelled
                    deny(handler, state, "banner_unavailable", actor)
                    return False
            else:
                data = _read_all(fd, size)
                if data is None:
                    deny(handler, state, "size_mismatch", actor)
                    return False
                link = mode_switch.encode("ascii") if mode_switch is not None else b""
                if bar:
                    injected = labelled_html(data, content_type_for(name), bar, link)
                    if injected is None:      # fail closed: a labelled page is never served unlabelled or with a hidden bar
                        deny(handler, state, "banner_unavailable", actor)
                        return False
                elif _ascii_compatible(content_type_for(name), data):
                    injected = inject_mode_switch(data, link)
                if injected is not None:
                    suffix = (SHARED_BY_ETAG_SUFFIX if bar else "") + (MODE_SWITCH_ETAG_SUFFIX if mode_switch is not None else "")
                    return _serve_with_link(handler, state, name, item, injected, csp, cors=cors, actor=actor,
                                            etag_suffix=suffix)
        headers = _content_headers(name, etag, csp, sandbox_pdf=sandbox_pdf)
        if _announces_body(handler):
            handler.close_connection = True
        if_match = handler.headers.get("If-Match")
        if if_match is not None:
            tags = _parse_etags(if_match)
            if "*" not in tags and etag not in tags:
                _send_headers(handler, 412, [("Content-Length", "0"), ("X-Content-Type-Options", "nosniff"),
                                             ("Cache-Control", "no-store")], cors=cors)
                return True
        if_none_match = handler.headers.get("If-None-Match")
        if if_none_match is not None:
            tags = [_weak(t) for t in _parse_etags(if_none_match)]
            if "*" in tags or etag in tags:
                _send_headers(handler, 304, [h for h in headers if h[0] != "Content-Type"], cors=cors)
                return True
        start, end, status = 0, size - 1, 200
        range_value = handler.headers.get("Range")
        if_range = handler.headers.get("If-Range")
        if range_value is not None and (if_range is None or if_range.strip() == etag):
            parsed = parse_range(range_value, size)
            if parsed == "unsatisfiable":
                _send_headers(handler, 416, [("Content-Range", f"bytes */{size}"), ("Content-Length", "0"),
                                             ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")],
                              cors=cors)
                return True
            if parsed is not None:
                start, end = parsed
                status = 206
                headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        length = end - start + 1 if size else 0
        headers.append(("Content-Length", str(length)))
        _send_headers(handler, status, headers, cors=cors)
        if handler.command == "GET" and status == 200 and name == state.get("entry"):
            _record_event(handler.server.events.opened, share_id, actor, {"file": name[:200]})
        if handler.command == "HEAD":
            return True
        offset, remaining = start, length
        try:
            while remaining > 0:
                chunk = os.pread(fd, min(CHUNK_BYTES, remaining), offset)
                if not chunk:          # the file shrank underneath us: the length promised cannot be kept
                    handler.close_connection = True
                    break
                view = memoryview(chunk)
                for at in range(0, len(view), WRITE_SLICE_BYTES):   # the socket timeout applies per slice
                    handler.wfile.write(view[at:at + WRITE_SLICE_BYTES])
                offset += len(chunk)
                remaining -= len(chunk)
        except (OSError, ValueError):
            handler.close_connection = True
        return True
    finally:
        os.close(fd)


def _serve_rich(handler, state: dict, item: dict, fd: int, actor: str) -> bool:
    """The opted-in author page of a report share: hash-verified, under the dashboard Rich view's no-script headers
    (``share_rich_view.SHARE_RICH_HEADERS``: the dashboard's minus frame-ancestors / X-Frame-Options, which an
    opaque-origin parent can never satisfy), never the bundle's permissive policy. No CORS, no ranges, no conditionals."""
    data = _read_all(fd, item["size"])
    if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
        deny(handler, state, "size_mismatch", actor)
        return False
    if _announces_body(handler):
        handler.close_connection = True
    _send_headers(handler, 200, [*SHARE_RICH_HEADERS, ("X-Robots-Tag", "noindex"), ("Content-Length", str(len(data)))],
                  cors=False)
    if handler.command != "HEAD":
        _write_body(handler, data)
    return True


def _serve_with_link(handler, state: dict, name: str, item: dict, body: bytes, csp: str, *, cors: bool,
                     actor: str, etag_suffix: str = MODE_SWITCH_ETAG_SUFFIX) -> bool:
    """The full 200 of ``body`` (an HTML file with the mode-switch link injected; see ``serve_bundle_file``). The ETag is
    the index sha256 plus ``-m1``, as for the raw file (the same size-only integrity check as the raw route)."""
    etag = f'"{item["sha256"]}{etag_suffix}"'
    headers = [h for h in _content_headers(name, etag, csp) if h[0] != "Accept-Ranges"] + [("Vary", "Sec-Fetch-Dest")]
    if _announces_body(handler):
        handler.close_connection = True
    if_match = handler.headers.get("If-Match")
    if if_match is not None:
        tags = _parse_etags(if_match)
        if "*" not in tags and etag not in tags:
            _send_headers(handler, 412, [("Content-Length", "0"), ("X-Content-Type-Options", "nosniff"),
                                         ("Cache-Control", "no-store")], cors=cors)
            return True
    if_none_match = handler.headers.get("If-None-Match")
    if if_none_match is not None:
        tags = [_weak(t) for t in _parse_etags(if_none_match)]
        if "*" in tags or etag in tags:
            _send_headers(handler, 304, [h for h in headers if h[0] != "Content-Type"], cors=cors)
            return True
    _send_headers(handler, 200, headers + [("Content-Length", str(len(body)))], cors=cors)
    if handler.command == "GET" and name == state.get("entry"):
        _record_event(handler.server.events.opened, state["share_id"], actor, {"file": name[:200]})
    if handler.command != "HEAD":
        _write_body(handler, body)
    return True


# ----------------------------------------------------------------------------- handler and server

_ROUTE_METHODS: dict[str, tuple[str, ...]] = {
    "link": ("GET", "HEAD"), "file": ("GET", "HEAD"), "session": ("POST",), "lab_file": ("GET", "HEAD"),
    "lab_view": ("GET", "HEAD"), "annotations": ("GET", "POST"), "annotation_item": ("POST",)}
_NO_PREFLIGHT = frozenset({"session", "lab_view", "annotations", "annotation_item"})   # OPTIONS is the uniform 404


def _classify(parts: list[str]) -> str | None:
    """The route name of the request path parts (``['', 's', token, ...]``), or None (see the module docstring)."""
    n = len(parts)
    if n == 4 and parts[3] == "":
        return "link"
    if n == 4 and parts[3] == "session":
        return "session"
    if n >= 5 and parts[3] == "f":
        return "file"
    if n >= 6 and parts[3] == "k":
        if n >= 7 and parts[5] == "f":
            return "lab_file"
        if n >= 7 and parts[5] in ("v", "a"):
            return "lab_view"
        if n == 6 and parts[5] == "annotations":
            return "annotations"
        if n == 8 and parts[5] == "annotations" and parts[7] in ("edit", "delete"):
            return "annotation_item"
    return None


class ShareHandler(BaseHTTPRequestHandler):
    server_version = "herdr-share"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30
    _headers_started = False

    def version_string(self) -> str:
        return "herdr-share"

    # The stdlib logs the request line, which carries the token: never log it.
    def log_message(self, format, *args):  # noqa: A002
        pass

    def log_error(self, format, *args):  # noqa: A002
        pass

    def log_request(self, code="-", size="-"):
        pass

    def handle_expect_100(self):
        return True          # never invite a body that will not be read

    def send_error(self, code, message=None, explain=None):
        _log("malformed_request", str(int(code)) if isinstance(code, int) else "")
        send_not_found(self)

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.request_version == "HTTP/0.9":
            self.send_error(400)
            return False
        return True

    def handle_one_request(self):
        self._headers_started = False
        super().handle_one_request()

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._unsupported_method
        raise AttributeError(name)

    def _unsupported_method(self):
        _log("denied", "method")
        send_not_found(self)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        try:
            if _announces_body(self):
                self.close_connection = True
            self._route()
        except Exception as exc:  # never a traceback (it could hold the path): one fixed line, then the uniform 404
            _log("internal_error", type(exc).__name__)
            if not self._headers_started:
                send_not_found(self)
            else:
                self.close_connection = True

    def _host_ok(self) -> bool:
        """With a configured ``public_host`` the request must carry exactly one Host naming it (D9)."""
        wanted = self.server.config.public_host
        if wanted is None:
            return True
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1:
            return False
        host = hosts[0].strip().lower()
        return host == wanted or host == wanted + ":443"

    def _route(self):
        if not self._host_ok():
            return deny(self, None, "wrong_host")
        raw = self.path
        if len(raw) > MAX_RAW_PATH or not raw.startswith("/s/"):
            return deny(self, None, "unknown_route")
        raw_path = raw.split("?", 1)[0]
        if not _RAW_PATH_OK.fullmatch(raw):
            return deny(self, None, "bad_path")
        parts = raw_path.split("/")
        if parts[2] in ("static", "callback"):
            return self._viewer_asset(parts)
        route = _classify(parts)
        if self.command == "OPTIONS":
            if route in _NO_PREFLIGHT:
                return deny(self, None, "method")    # no preflight: a foreign origin cannot send Authorization or the CSRF header
            _send_headers(self, 204, PREFLIGHT_HEADERS)
            return None
        if len(parts) == 3 and self.command in ("GET", "HEAD") and TOKEN_RE.fullmatch(parts[2]):
            # a link pasted without its trailing slash: answered for ANY well-formed token without looking it up,
            # so the redirect says nothing about which tokens exist
            _send_headers(self, 302, [("Location", parts[2] + "/"), ("Content-Length", "0"),
                                      ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                                      ("X-Robots-Tag", "noindex"), ("X-Content-Type-Options", "nosniff")])
            return None
        if len(parts) < 4 or not TOKEN_RE.fullmatch(parts[2]):
            return deny(self, None, "bad_token")
        if route is None:
            return deny(self, None, "unknown_route")
        if self.command not in _ROUTE_METHODS[route]:
            return deny(self, None, "method")
        prepared = _read_state(self.server.config.shares_root, parts[2])
        if prepared is None:
            return deny(self, None, "unknown_token")
        state, reason = _check_state(prepared, self.server.now())
        if state is None:
            return deny(self, prepared.raw, reason)
        audience = state.get("audience")
        if audience == "public" and route in ("link", "file"):
            if route == "link":
                location = "f/" + quote(state["entry"], safe="/")
                _send_headers(self, 302, [("Location", location), ("Content-Length", "0"),
                                          ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer"),
                                          ("X-Robots-Tag", "noindex"), ("X-Content-Type-Options", "nosniff")])
                return None
            serve_bundle_file(self, state, "/".join(parts[4:]), token=parts[2])
            return None
        if audience != "lab" or route == "file":
            return deny(self, state, "audience")    # lab files only under a session; sessions only for lab shares
        lab = self.server.lab
        if lab is None:
            return deny(self, state, "lab_unconfigured")
        if route == "link":
            return self._send_static(lab, VIEWER_FILE, "text/html; charset=utf-8")
        if route == "session":
            return self._session(state, parts[2], lab)
        session = parts[4]
        got = check_session(lab.secret, state["share_id"], session, self.server.now()) \
            if _SESSION_SEGMENT.fullmatch(session) else None
        if got is None:
            return deny(self, state, "bad_session")
        enabled = self.server.annotations_enabled
        if route == "lab_file":
            link = self._mode_switch(parts) if enabled else None
            serve_bundle_file(self, state, "/".join(parts[6:]), actor=got["identity"], token=parts[2],
                              mode_switch=link, sandbox_pdf=True)
            return None
        if route == "lab_view":
            if parts[5] == "v":
                return self._view(state, parts, got, "live", lab)
            if not enabled:
                return deny(self, state, "annotations_disabled", got["identity"])
            return self._view(state, parts, got, "annotate", lab)
        if not enabled:
            return deny(self, state, "annotations_disabled", got["identity"])
        if route == "annotations" and self.command == "GET":
            if not self._has_write_cookie(state, session, lab):
                return deny(self, state, "no_write_cookie", got["identity"])   # the URL alone is not a credential
            return self._annotations_get(state, got)
        return self._annotation_post(state, parts, got, lab)

    def _mode_switch(self, parts) -> str | None:
        """The link to inject into this lab ``f/`` response, or None: only HTML, only a document navigation."""
        if self.headers.get_all("Sec-Fetch-Dest") != ["document"]:
            return None
        rel = "/".join(parts[6:])
        name = decode_rel_path(rel)
        if name is None or content_type_for(name).split(";")[0].strip() != "text/html":
            return None
        return mode_switch_link(rel.count("/") + 1, rel)

    # -- lab: live view of non-HTML files

    def _send_json(self, status: int, payload: object) -> None:
        # "<" is escaped so that a viewer's text never appears as markup in any body this host sends (the no-frames guard
        # checks the annotations JSON); the escape is plain JSON, so every client parses the same value.
        body = json.dumps(payload, sort_keys=True, ensure_ascii=True).replace("<", "\\u003c").encode("ascii")
        _send_headers(self, status, [*JSON_HEADERS, ("Content-Length", str(len(body)))], cors=False)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _unavailable(self) -> None:
        """The uniform 503 (a render is busy or the annotations file is unavailable): fixed body, no detail."""
        _send_headers(self, 503, [("Content-Type", "text/plain; charset=utf-8"),
                                  ("Content-Length", str(len(UNAVAILABLE_BODY))), ("Retry-After", "5"),
                                  ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store"),
                                  ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex")], cors=False)
        if self.command != "HEAD":
            self.wfile.write(UNAVAILABLE_BODY)

    def _view(self, state: ShareState, parts, got: dict, mode: str, lab: LabSettings):
        """``v/<path>`` (live) and ``a/<path>`` (annotate): one template, our own markup around the static render.

        ``v/`` of an HTML file is the 404 (its live view is ``f/``). ``a/`` serves every kind: HTML is sanitised in the
        isolated render child, text is escaped, image / PDF / other kinds are built by ``annotate.js`` from the data."""
        server = self.server
        token = parts[2]
        origin = server.public_origin
        name = decode_rel_path("/".join(parts[6:]))
        index = state.index
        item = index.get(name) if name is not None else None
        actor = got["identity"]
        if origin is None or name is None or item is None:
            return deny(self, state, "not_in_bundle" if origin is not None else "invalid_state", actor)
        if name == state.get("rich"):
            return deny(self, state, "not_in_mode", actor)     # the author page is framed from the preview, never annotated
        kind = render_kind(name)
        if kind == "html" and mode == "live":
            return deny(self, state, "not_in_mode", actor)
        share_id = state["share_id"]
        opened = _open_beneath(Path(server.config.shares_root), [share_id, "files", *name.split("/")])
        if opened is None:
            return deny(self, state, "missing_file", actor)
        fd, st = opened
        try:
            if st.st_size != item["size"]:
                return deny(self, state, "size_mismatch", actor)
            content, shown = "", kind
            if kind in ("text", "html"):
                if item["size"] > MAX_RENDER_INPUT:
                    content, shown = self._too_large(), "document"
                else:
                    data = _read_all(fd, item["size"])
                    if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
                        return deny(self, state, "size_mismatch", actor)
                    try:
                        content = self._static_copy(kind, share_id, name, item["sha256"], data, index)
                    except RenderTooLarge:
                        content, shown = self._too_large(), "document"
                    except RenderBusy:
                        return self._unavailable()
                    except RenderFailed:
                        return deny(self, state, "render_failed", actor)
        finally:
            os.close(fd)
        nonce, body = self._render_view(state, parts, got, name, shown, content, mode, lab)
        csp = (ANNOTATE_CSP.format(origin=origin, token=token, nonce=nonce) if mode == "annotate"
               else VIEW_CSP.format(origin=origin, token=token, nonce=nonce))
        headers = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                   ("Content-Security-Policy", csp),
                   ("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex")]
        if mode == "annotate":
            headers += [("Cross-Origin-Opener-Policy", "same-origin"), ("Cross-Origin-Resource-Policy", "same-origin")]
        _send_headers(self, 200, headers, cors=False)
        if self.command == "GET" and name == state.get("entry"):
            _record_event(server.events.opened, share_id, actor, {"file": name[:200], "mode": mode})
        if self.command != "HEAD":
            _write_body(self, body)
        return None

    @staticmethod
    def _too_large() -> str:
        return f'<p class="herdr-static-note">{html.escape(TOO_LARGE_NOTE)}</p>'

    def _static_copy(self, kind: str, share_id: str, name: str, sha256: str, data: bytes, index: dict) -> str:
        """The wrapped static render of ``data`` (raises ``RenderTooLarge`` / ``RenderBusy`` / ``RenderFailed``).

        HTML: the sanitised output depends on the bytes, the file name and WHICH names the bundle holds (the resolver
        drops links to absent files), so the cache key names all of them plus the share and the resolver version."""
        if kind == "html":
            fingerprint = hashlib.sha256("\0".join(sorted(index)).encode("utf-8")).hexdigest()
            key = (share_id, name, sha256, RESOLVER_VERSION, fingerprint)
            return wrap_content("html", sanitize_html(data, resolve=share_resolver(name, index), cache_key=key))
        cache = self.server.render_cache
        cached = cache.get(sha256, "text\0" + name)
        if cached is None:
            cached = wrap_content("text", render_text(data))
            cache.put(sha256, "text\0" + name, cached)
        return cached

    def _render_view(self, state: ShareState, parts, got: dict, name: str, kind: str, content: str, mode: str,
                     lab: LabSettings) -> tuple[str, bytes]:
        """``(nonce, page)``: ``view.html`` with each placeholder replaced once, in one pass (so no substituted value is
        ever re-scanned). Every value of ours is escaped or JSON-encoded; ``content`` is the only untrusted markup and
        it is already sanitised and sits inside a shadow root."""
        server = self.server
        up = "../" * (name.count("/") + 1)
        quoted = quote(name, safe="/")
        nonce = secrets.token_urlsafe(NONCE_BYTES)
        annotate = mode == "annotate"
        live_href = up + ("f/" if render_kind(name) == "html" else "v/") + quoted
        links = [f'<a data-mode="live" href="{html.escape(live_href)}"' + ("" if annotate else ' aria-current="page"')
                 + ">Live</a>"]
        if server.annotations_enabled:
            links.append(f'<a data-mode="annotate" href="{html.escape(up + "a/" + quoted)}"'
                         + (' aria-current="page"' if annotate else "") + ">Annotate</a>")
        data = {"file": name, "kind": kind, "mode": mode, "entry": state["entry"], "expires_at": int(got["exp"]),
                "max_comment_chars": server.config.annotations["max_comment_chars"], "raw_href": up + "f/" + quoted}
        if annotate and kind == "pdf":
            data["pdfjs"] = PDFJS_CONFIG
        data_json = json.dumps(data, sort_keys=True, ensure_ascii=True)
        for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
            data_json = data_json.replace(ch, esc)
        csrf = ""
        panel = ""
        if annotate:
            token = csrf_token(lab.secret, state["share_id"], parts[4])
            csrf = f'<meta name="herdr-annotate-csrf" content="{html.escape(token)}">'
            panel = '<aside data-annotate-panel aria-label="Annotations"></aside>'
        shared_by = state.get("shared_by")
        banner = (f'<aside class="herdr-shared-by" role="note">Shared by {html.escape(shared_by)} \u2014 content was not '
                  'reviewed by the operator</aside>') if isinstance(shared_by, str) else ""
        values = {
            "BANNER": banner, "NONCE": nonce, "MODE": mode, "CSRF_META": csrf, "PANEL": panel,
            "SWITCH": '<nav data-mode-switch aria-label="View mode">' + "".join(links) + "</nav>",
            "DATA_JSON": f'<script type="application/json" id="herdr-view-data" nonce="{nonce}">{data_json}</script>',
            "CONTENT": content}
        page = _VIEW_PLACEHOLDER.sub(lambda m: values[m.group(1)], server.view_template)
        return nonce, page.encode("utf-8")

    # -- lab: annotations API

    def _annotations_get(self, state: ShareState, got: dict):
        """The caller's OWN annotations from the worker's per-viewer projection; never another viewer's."""
        name = hashlib.sha256(got["identity"].encode("utf-8")).hexdigest() + ".json"
        rows = self._own_rows(state["share_id"], name)
        if rows is None:
            return self._annotate_error(state, got, 503, "unavailable")
        self._send_json(200, {"annotations": rows})
        return None

    def _own_rows(self, share_id: str, name: str) -> list | None:
        opened = _open_beneath(Path(self.server.config.shares_root), [share_id, "annotations", name],
                               max_size=MAX_PROJECTION_BYTES)
        if opened is None:
            return []                                    # no projection yet: nothing of theirs has been ingested
        fd, st = opened
        try:
            data = _read_all(fd, st.st_size)
            doc = json.loads(data) if data is not None else None
        except (OSError, ValueError):
            return None
        finally:
            os.close(fd)
        rows = doc.get("annotations") if isinstance(doc, dict) else None
        return rows if isinstance(rows, list) and all(isinstance(r, dict) for r in rows) else None

    def _annotate_error(self, state: ShareState, got: dict, status: int, code: str):
        """A JSON refusal (400/409/429/503) that also leaves a ``denied`` event with a fixed reason code."""
        _record_event(self.server.events.denied, state["share_id"], got["identity"], "annotate_" + code)
        self._send_json(status, {"error": code})
        return None

    def _has_write_cookie(self, state: ShareState, session: str, lab: LabSettings) -> bool:
        return check_write_cookie(lab.secret, state["share_id"], session, self.headers.get_all("Cookie"))

    def _gate_ok(self, state: ShareState, session: str, got: dict, lab: LabSettings) -> bool:
        """Every POST precondition except the body: share allow-list, Origin, CSRF, content type and framing."""
        headers = self.headers
        if getattr(headers, "defects", None) or headers.get_all("Transfer-Encoding"):
            return False
        origin, csrf, ctype = (self._single_header(n) for n in ("Origin", "X-Herdr-Annotate", "Content-Type"))
        lengths = headers.get_all("Content-Length") or []
        expected = self.server.annotate_origin
        if (expected is None or origin is None or origin != expected or csrf is None or ctype is None
                or len(lengths) != 1 or not _CONTENT_LENGTH.fullmatch(lengths[0])
                or not 1 <= int(lengths[0]) <= MAX_BODY_BYTES):
            return False
        if ctype != "application/json":              # exactly, case-sensitive, no parameters
            return False
        return check_csrf(lab.secret, state["share_id"], session, csrf) and self._has_write_cookie(state, session, lab)

    def _annotation_post(self, state: ShareState, parts, got: dict, lab: LabSettings):
        server, share_id = self.server, state["share_id"]
        self.close_connection = True                      # the body is read once; the connection never carries a second request
        if not self._gate_ok(state, parts[4], got, lab):
            return deny(self, state, "annotate_refused", got["identity"])
        if not still_admitted(state, got["email"], got["basis"]):
            return deny(self, state, "not_on_list", got["identity"])      # removed from the allow-list since the session
        route_edit = len(parts) == 8
        annotation_id = parts[6] if route_edit else None
        if route_edit and not ANNOTATION_ID_RE.fullmatch(annotation_id):
            return deny(self, state, "annotate_refused", got["identity"])
        identity = got["identity"]
        if not (server.annotate_user.allow(f"{share_id}\x00{identity}") and server.annotate_share.allow(share_id)
                and server.annotate_global.allow("*")):
            return self._annotate_error(state, got, 429, "rate_limited")
        length = int(self.headers.get("Content-Length"))
        try:
            raw = self.rfile.read(length)
        except OSError:
            raw = b""
        if len(raw) != length:
            return deny(self, state, "annotate_refused", identity)
        max_chars = server.config.annotations["max_comment_chars"]
        at = datetime.fromtimestamp(server.now(), UTC).isoformat()
        try:
            if not route_edit:
                anchor, comment = parse_annotation_request(raw, max_chars=max_chars)
                if anchor["file"] not in state.index:
                    return self._annotate_error(state, got, 400, "bad_file")
                annotation_id = "ann_" + secrets.token_urlsafe(16)
                record = {"v": 1, "op": "create", "annotation_id": annotation_id, "share_id": share_id,
                          "author_identity": identity, "author_email": got["email"], "basis": got["basis"], "at": at,
                          "anchor": anchor,
                          "body": comment}
            else:
                verb = parts[7]
                record = {"v": 1, "op": verb, "annotation_id": annotation_id, "share_id": share_id,
                          "author_identity": identity, "author_email": got["email"], "basis": got["basis"], "at": at}
                if verb == "edit":
                    record["body"] = parse_edit_request(raw, max_chars=max_chars)
                else:
                    parse_delete_request(raw)
        except AnnotationInputError as exc:
            return self._annotate_error(state, got, 400, exc.code)
        if route_edit:
            name = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json"
            rows = self._own_rows(share_id, name)
            if rows is None:
                return self._annotate_error(state, got, 503, "unavailable")
            if not any(r.get("annotation_id") == annotation_id and r.get("status") == "open" for r in rows):
                return self._annotate_error(state, got, 409, "not_editable")
        if not server.annotation_log.append(record):
            return self._annotate_error(state, got, 503, "unavailable")
        if route_edit:
            self._send_json(202, {"annotation_id": annotation_id, "status": "pending"})
        else:
            self._send_json(201, {"annotation": {"annotation_id": annotation_id, "anchor": record["anchor"],
                                                 "body": record["body"], "status": "submitted", "created_at": at}})
        return None

    # -- lab: viewer page, static assets, session exchange

    def _viewer_asset(self, parts):
        if self.command not in ("GET", "HEAD"):
            return deny(self, None, "method")
        lab = self.server.lab
        if lab is None:
            return deny(self, None, "lab_unconfigured")
        if len(parts) == 3 and parts[2] == "callback":
            return self._send_static(lab, VIEWER_FILE, "text/html; charset=utf-8")
        if len(parts) == 4 and parts[2] == "static":
            name = parts[3]
            if name == "config.json":
                return self._send_viewer_bytes(lab, lab.config_json, "application/json")
            if name in STATIC_FILES:
                return self._send_static(lab, name, STATIC_FILES[name])
        if len(parts) == 6 and parts[2:5] == ["static", "vendor", "pdfjs"] and parts[5] in VENDOR_FILES:
            return self._send_static(lab, parts[5], VENDOR_FILES[parts[5]], subdir=("vendor", "pdfjs"))
        return deny(self, None, "unknown_route")

    def _send_static(self, lab: LabSettings, name: str, content_type: str, subdir: tuple[str, ...] = ()):
        """One allow-listed file straight from ``static_dir`` (no directory walk, no symlink), or the 404.
        ``subdir`` is the fixed vendored-library directory: bigger cap, immutable cache (the path pins the release)."""
        opened = _open_beneath(lab.static_dir, [*subdir, name], max_size=VENDOR_MAX_BYTES if subdir else STATIC_MAX_BYTES)
        if opened is None:
            return deny(self, None, "static_missing")
        fd, st = opened
        try:
            chunks, remaining = [], st.st_size
            while remaining > 0:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        body = b"".join(chunks)
        if not subdir:
            return self._send_viewer_bytes(lab, body, content_type)
        etag = vendor_etag(body)
        if etag_matches(self.headers.get("If-None-Match"), etag):
            return self._send_viewer_bytes(lab, body, content_type, VENDOR_CACHE, etag=etag, status=304)
        return self._send_viewer_bytes(lab, body, content_type, VENDOR_CACHE, etag=etag)

    def _send_viewer_bytes(self, lab: LabSettings, body: bytes, content_type: str, cache: str = "no-store",
                           *, etag: str | None = None, status: int = 200):
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body))),
                   ("Content-Security-Policy", lab.viewer_csp), ("X-Content-Type-Options", "nosniff"),
                   ("Cache-Control", cache), ("Referrer-Policy", "no-referrer"),
                   ("X-Robots-Tag", "noindex"), ("X-Frame-Options", "DENY")]
        if etag:
            headers.append(("ETag", etag))
        if status == 304:
            headers = [h for h in headers if h[0] not in ("Content-Type", "Content-Length")]
        _send_headers(self, status, headers, cors=False)
        if self.command != "HEAD" and status == 200:
            self.wfile.write(body)

    def _single_header(self, name: str) -> str | None:
        values = self.headers.get_all(name) or []
        return values[0] if len(values) == 1 else None

    def _session(self, state: ShareState, token: str, lab: LabSettings):
        """Exchange verified Cognito tokens for a session path. Never echoes a token; every failure is the 404."""
        share_id = state["share_id"]
        if _announces_body(self):
            return deny(self, state, "body")
        if not self.server.session_per_share.allow(share_id):
            return deny(self, state, "rate_limited")
        if not self.server.session_global.allow("*"):     # charged only by resolved live lab shares: an unknown
            return deny(self, state, "rate_limited")      # token cannot drain it and lock every share out
        authorization, id_token = self._single_header("Authorization"), self._single_header("X-Id-Token")
        if authorization is None or id_token is None:
            return deny(self, state, "bad_headers")
        scheme, _, access_token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            return deny(self, state, "bad_headers")
        grant, reason, identity = authorise(state, access_token.strip(), id_token.strip(), lab.authenticator)
        if grant is None:
            return deny(self, state, reason, identity or "public")
        now = self.server.now()
        exp = min(now + lab.session_seconds, grant["exp"])
        try:
            share_expiry = _expiry(state.get("expires_at"))
        except (ValueError, TypeError, OverflowError):
            return deny(self, state, "invalid_state", grant["identity"])
        if share_expiry is not None:
            exp = min(exp, share_expiry)
        exp_int = int(exp // 1)
        if exp_int <= now:
            return deny(self, state, "token_expired", grant["identity"])
        try:
            session = mint_session(lab.secret, share_id, grant["identity"], exp_int, email=grant["email"], basis=grant["basis"])
        except ValueError:
            return deny(self, state, "bad_identity", grant["identity"])
        except RuntimeError:
            log.error("cryptography missing for lab sessions: no session can be minted")    # no secrets in the message
            return deny(self, state, "session_unavailable", grant["identity"])
        entry = state["entry"]
        kind = "f" if content_type_for(entry).split(";")[0].strip() == "text/html" else "v"
        landing = f"/s/{token}/k/{session}/{kind}/{quote(entry, safe='/')}"
        body = json.dumps({"landing": landing, "expires_at": exp_int},
                          sort_keys=True, ensure_ascii=True).encode("ascii")
        cookie = write_cookie_header(lab.secret, share_id, token, session, exp_int - now)
        _send_headers(self, 200, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                                  ("Set-Cookie", cookie),
                                  ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                                  ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex"),
                                  ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; sandbox")],
                      cors=False)
        self.wfile.write(body)
        return None


class ShareServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, config: ShareConfig, now: Callable[[], float]):
        self.config = config
        self.now = now
        self.events = EventLog(config.events_path, now)
        self.lab = lab_settings(config)
        self.public_origin = _https_origin(config.public_url)    # None: the sandbox CSP keeps 'self' only
        self.session_per_share = RateLimiter(SESSION_PER_SHARE_PER_MINUTE, now)
        self.session_global = RateLimiter(SESSION_GLOBAL_PER_MINUTE, now, max_keys=1)
        limits = config.annotations
        self.annotations_enabled = bool(self.lab is not None and limits["enabled"])
        self.annotate_origin = self.public_origin          # the exact Origin an annotation POST must carry
        self.annotate_user = RateLimiter(limits["per_user_per_minute"], now)
        self.annotate_share = RateLimiter(limits["per_share_per_minute"], now)
        self.annotate_global = RateLimiter(limits["global_per_minute"], now, max_keys=1)
        self.annotation_log = AnnotationLog(config.annotations_path)
        self.render_cache = RenderCache()
        self.view_template = self._load_view_template()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, ShareHandler)

    def _load_view_template(self) -> str:
        """``view.html`` once at start-up; every placeholder must occur exactly once (else the server does not start)."""
        static = Path(self.config.static_dir) if self.config.static_dir else DEFAULT_STATIC_DIR
        text = (static / VIEW_FILE).read_text(encoding="utf-8") if (static / VIEW_FILE).is_file() else ""
        found = [m.group(1) for m in _VIEW_PLACEHOLDER.finditer(text)]
        if self.lab is not None and sorted(found) != sorted(["NONCE", "MODE", "CSRF_META", "SWITCH", "DATA_JSON",
                                                              "PANEL", "CONTENT", "BANNER"]):
            raise RuntimeError("share_static/view.html must hold each placeholder exactly once")
        return text

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            _log("connection_limit")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        _log("connection_error")       # no traceback: it could contain the request path


def make_share_server(config: ShareConfig, *, host: str, port: int, now: Callable[[], float] = time.time) -> ShareServer:
    return ShareServer((host, port), config, now)
