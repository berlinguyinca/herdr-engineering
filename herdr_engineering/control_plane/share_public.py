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
  ``share_lab.authorise`` against the share's allow-list, then a ``share_session`` HMAC session capped at the earliest of
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
  nonce CSP; it is sent without CORS. ``a/<path>`` is routed but answers the uniform 404 until the annotation view
  lands (``a_view_pending`` in the events file).
* The annotation POSTs are the only routes that read a body. A POST is refused (uniform 404, reason ``annotate_refused``)
  unless the session is valid and the share's allow-list still admits its email (``share_lab.still_admitted``: an email
  removed from ``allow_emails`` stops writing at once; members admitted through ``allow_groups`` cannot be re-checked
  without their tokens and are bounded by the session expiry), ``Origin`` equals the share origin exactly, the CSRF
  header passes a constant-time check, ``Content-Type`` is JSON and there is exactly one ``Content-Length`` of 1 to
  32768 bytes and no ``Transfer-Encoding``. Accepted requests append one line to ``annotations.jsonl`` for the worker;
  author identity and email come from the session only. Edits and deletes are decided against the viewer's own
  read-back projection, so an annotation is editable only after the worker has ingested it.
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
    RenderBusy,
    RenderCache,
    RenderFailed,
    RenderTooLarge,
    render_kind,
    render_text,
    wrap_content,
)
from .share_session import MIN_SECRET_BYTES, check_csrf, check_session, mint_session

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
VIEWER_FILE = "viewer.html"
STATIC_FILES = {"share.js": "text/javascript; charset=utf-8", "share.css": "text/css; charset=utf-8",
                "annotate.js": "text/javascript; charset=utf-8", "annotate.css": "text/css; charset=utf-8"}
VIEW_FILE = "view.html"
ANNOTATIONS_MAX_BYTES = 32 * 1024 * 1024     # the annotations file cap (the worker rotates it long before)
ANNOTATION_LINE_MAX = 65536                  # the worker's max_line_bytes: a longer line would be dropped at ingest
MODE_SWITCH_MAX_BYTES = 8 * 1024 * 1024      # larger HTML files are served untouched, without the link
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
_VIEW_PLACEHOLDER = re.compile(r"\{\{(NONCE|MODE|CSRF_META|SWITCH|DATA_JSON|CONTENT)\}\}")

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
MODE_SWITCH_TEMPLATE = (
    '<a data-herdr-mode-switch href="{href}" rel="noopener" style="position:fixed;right:12px;bottom:12px;'
    'z-index:2147483647;display:block;margin:0;padding:6px 12px;font:600 14px/1.4 system-ui,sans-serif;color:#fff;'
    'background:#1f4fd1;border-radius:6px;text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,.4)">Annotate</a>')
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

    def __post_init__(self) -> None:
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
        if self._denied.allow("%s\x00%s" % (share_id, reason)):   # per share: one link cannot silence the others
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
        _log(why, "dropped=%d" % self.dropped)


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
        try:
            handler.server.events.denied(share_id, actor, reason)
        except Exception:
            _log("event append failed")
    send_not_found(handler)


def _content_headers(name: str, etag: str, csp: str = CSP) -> list[tuple[str, str]]:
    content_type = content_type_for(name)
    if content_type == "application/pdf":
        headers = [("Content-Type", "application/pdf"), ("Content-Disposition", "inline"),
                   ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", PDF_CSP)]
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


def inject_mode_switch(data: bytes, link: bytes) -> bytes:
    """``link`` inserted before the LAST case-insensitive ``</body`` of ``data``, or appended when there is none."""
    cut = data.lower().rfind(b"</body")
    return data + link if cut < 0 else data[:cut] + link + data[cut:]


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
                      token: str | None = None, mode_switch: str | None = None) -> bool:
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
        etag = '"%s"' % item["sha256"]
        csp = sandbox_csp(getattr(handler.server, "public_origin", None), token)
        if (mode_switch is not None and content_type_for(name).split(";")[0].strip() == "text/html"
                and size <= MODE_SWITCH_MAX_BYTES):
            return _serve_with_link(handler, state, name, item, fd, mode_switch, csp, cors=cors, actor=actor)
        headers = _content_headers(name, etag, csp)
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
                _send_headers(handler, 416, [("Content-Range", "bytes */%d" % size), ("Content-Length", "0"),
                                             ("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")],
                              cors=cors)
                return True
            if parsed is not None:
                start, end = parsed
                status = 206
                headers.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
        length = end - start + 1 if size else 0
        headers.append(("Content-Length", str(length)))
        _send_headers(handler, status, headers, cors=cors)
        if handler.command == "GET" and status == 200 and name == state.get("entry"):
            try:
                handler.server.events.opened(share_id, actor, {"file": name[:200]})
            except Exception:
                _log("event append failed")
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


def _serve_with_link(handler, state: dict, name: str, item: dict, fd: int, link: str, csp: str, *, cors: bool,
                     actor: str) -> bool:
    """The full 200 of an HTML file with the mode-switch link injected (see ``serve_bundle_file``)."""
    data = _read_all(fd, item["size"])
    if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
        deny(handler, state, "size_mismatch", actor)
        return False
    body = inject_mode_switch(data, link.encode("ascii"))
    etag = f'"{item["sha256"]}{MODE_SWITCH_ETAG_SUFFIX}"'
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
        try:
            handler.server.events.opened(state["share_id"], actor, {"file": name[:200]})
        except Exception:
            _log("event append failed")
    if handler.command != "HEAD":
        try:
            view = memoryview(body)
            for at in range(0, len(view), WRITE_SLICE_BYTES):
                handler.wfile.write(view[at:at + WRITE_SLICE_BYTES])
        except (OSError, ValueError):
            handler.close_connection = True
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

    def _route(self):
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
                              mode_switch=link)
            return None
        if route == "lab_view":
            if parts[5] == "v":
                return self._live_view(state, parts, got)
            return deny(self, state, "a_view_pending", got["identity"])     # the annotation view is not built yet
        if not enabled:
            return deny(self, state, "annotations_disabled", got["identity"])
        if route == "annotations" and self.command == "GET":
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
        body = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii")
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

    def _live_view(self, state: ShareState, parts, got: dict):
        """``v/<path>``: image, PDF, text and other kinds from our own template (HTML's live view is ``f/``)."""
        server = self.server
        token = parts[2]
        origin = server.public_origin
        name = decode_rel_path("/".join(parts[6:]))
        index = state.index
        item = index.get(name) if name is not None else None
        actor = got["identity"]
        if origin is None or name is None or item is None:
            return deny(self, state, "not_in_bundle" if origin is not None else "invalid_state", actor)
        kind = render_kind(name)
        if kind == "html":
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
            if kind == "text":
                if item["size"] > MAX_RENDER_INPUT:
                    content, shown = f'<p class="herdr-static-note">{html.escape(TOO_LARGE_NOTE)}</p>', "document"
                else:
                    data = _read_all(fd, item["size"])
                    if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
                        return deny(self, state, "size_mismatch", actor)
                    try:
                        content = server.render_cache.get(item["sha256"], "text\0" + name)
                        if content is None:
                            content = wrap_content("text", render_text(data))
                            server.render_cache.put(item["sha256"], "text\0" + name, content)
                    except RenderTooLarge:
                        content, shown = f'<p class="herdr-static-note">{html.escape(TOO_LARGE_NOTE)}</p>', "document"
                    except RenderBusy:
                        return self._unavailable()
                    except RenderFailed:
                        return deny(self, state, "render_failed", actor)
        finally:
            os.close(fd)
        page = self._render_view(state, parts, got, name, shown, content, origin)
        nonce, body = page
        headers = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                   ("Content-Security-Policy", VIEW_CSP.format(origin=origin, token=token, nonce=nonce)),
                   ("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex")]
        _send_headers(self, 200, headers, cors=False)
        if self.command == "GET" and name == state.get("entry"):
            try:
                server.events.opened(share_id, actor, {"file": name[:200], "mode": "live"})
            except Exception:
                _log("event append failed")
        if self.command != "HEAD":
            self.wfile.write(body)
        return None

    def _render_view(self, state: ShareState, parts, got: dict, name: str, kind: str, content: str,
                     origin: str) -> tuple[str, bytes]:
        """``(nonce, page)``: ``view.html`` with each placeholder replaced once, in one pass (so no substituted value is
        ever re-scanned) -- the content last in the document order is irrelevant for the same reason."""
        server = self.server
        up = "../" * (name.count("/") + 1)
        quoted = quote(name, safe="/")
        nonce = secrets.token_urlsafe(NONCE_BYTES)
        links = [f'<a data-mode="live" href="{html.escape(up + "v/" + quoted)}" aria-current="page">Live</a>']
        if server.annotations_enabled:
            links.append(f'<a data-mode="annotate" href="{html.escape(up + "a/" + quoted)}">Annotate</a>')
        data = {"file": name, "kind": kind, "mode": "live", "entry": state["entry"], "expires_at": int(got["exp"]),
                "max_comment_chars": server.config.annotations["max_comment_chars"], "raw_href": up + "f/" + quoted}
        data_json = json.dumps(data, sort_keys=True, ensure_ascii=True)
        for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
            data_json = data_json.replace(ch, esc)
        values = {
            "NONCE": nonce, "MODE": "live", "CSRF_META": "",
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
        try:
            self.server.events.denied(state["share_id"], got["identity"], "annotate_" + code)
        except Exception:
            _log("event append failed")
        self._send_json(status, {"error": code})
        return None

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
        if ctype.split(";")[0].strip().lower() != "application/json":
            return False
        return check_csrf(lab.secret, state["share_id"], session, csrf)

    def _annotation_post(self, state: ShareState, parts, got: dict, lab: LabSettings):
        server, share_id = self.server, state["share_id"]
        self.close_connection = True                      # the body is read once; the connection never carries a second request
        if not self._gate_ok(state, parts[4], got, lab):
            return deny(self, state, "annotate_refused", got["identity"])
        if not still_admitted(state, got["email"]):
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
                          "author_identity": identity, "author_email": got["email"], "at": at, "anchor": anchor,
                          "body": comment}
            else:
                verb = parts[7]
                record = {"v": 1, "op": verb, "annotation_id": annotation_id, "share_id": share_id,
                          "author_identity": identity, "at": at}
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
            session = mint_session(lab.secret, share_id, grant["identity"], exp_int, email=grant["email"])
        except ValueError:
            return deny(self, state, "bad_identity", grant["identity"])
        entry = state["entry"]
        kind = "f" if content_type_for(entry).split(";")[0].strip() == "text/html" else "v"
        landing = f"/s/{token}/k/{session}/{kind}/{quote(entry, safe='/')}"
        body = json.dumps({"landing": landing, "expires_at": exp_int},
                          sort_keys=True, ensure_ascii=True).encode("ascii")
        _send_headers(self, 200, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
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
                                                              "CONTENT"]):
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
