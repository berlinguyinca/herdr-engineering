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
* Tokens, sessions and Authorization values are never written to events or logs (the stdlib request log is disabled).
* No request body is ever read; a request that announces one gets its response and the connection closed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes

from .share_bundle import ShareBundleError, content_type_for, normalise_name

log = logging.getLogger("herdr.share_public")

CHUNK_BYTES = 1024 * 1024
EVENTS_MAX_BYTES = 8 * 1024 * 1024
STATE_MAX_BYTES = 64 * 1024 * 1024
DENIED_PER_MINUTE = 20          # denied events per reason per minute (a scanner cannot fill the events file)
OPENED_PER_MINUTE = 60          # opened events per share per minute
LOG_PER_MINUTE = 20             # log lines per reason per minute
MAX_CONNECTIONS = 128
MAX_RAW_PATH = 8192
STATE_CACHE_SIZE = 256

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_SHARE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RAW_PATH_OK = re.compile(r"[\x21-\x5b\x5d-\x7e]*")   # printable ASCII without space and backslash
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RANGE_SPEC = re.compile(r"(\d{0,19})-(\d{0,19})")

CSP = ("sandbox allow-scripts allow-downloads; default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
       "worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'")
PDF_CSP = "frame-ancestors 'none'"
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

    Over ``EVENTS_MAX_BYTES`` lines are dropped and counted (never rotated: the ingest's marker is keyed to the file's
    content). Lines only ever carry the share id, a kind, an actor, a timestamp and fixed reason codes or index names.
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
        if self._denied.allow(reason):
            self.append(share_id, "denied", actor, {"reason": reason})

    def append(self, share_id: str, kind: str, actor: str, detail: dict) -> None:
        at = datetime.fromtimestamp(self.now(), UTC).isoformat()
        line = (json.dumps({"share_id": share_id, "kind": kind, "actor": actor[:200], "at": at, "detail": detail},
                           sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
        with self._lock:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o640)
            except OSError:
                self._drop("events file unavailable")
                return
            try:
                if os.fstat(fd).st_size + len(line) > EVENTS_MAX_BYTES:
                    self._drop("events file is full")
                    return
                if os.write(fd, line) != len(line):
                    self._drop("short write to events file")
            except OSError:
                self._drop("events file write failed")
            finally:
                os.close(fd)

    def _drop(self, why: str) -> None:
        self.dropped += 1
        _log(why, "dropped=%d" % self.dropped)


_log_limiter = RateLimiter(LOG_PER_MINUTE, time.monotonic)


def _log(reason: str, extra: str = "") -> None:
    """A rate-limited warning carrying a fixed reason code only (never a path, token or header value)."""
    if _log_limiter.allow(reason):
        log.warning("share-public: %s %s", reason, extra)


# ----------------------------------------------------------------------------- safe filesystem access

def _open_beneath(root: Path, parts: list[str], *, max_size: int | None = None) -> tuple[int, os.stat_result] | None:
    """Open ``root/parts...`` without following a symlink at ANY component; a regular file, or None."""
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd)
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


_cache_lock = threading.Lock()
_state_cache: OrderedDict[tuple, dict] = OrderedDict()


def _read_state(root: Path, token: str) -> dict | None:
    """The raw ``state.json`` for ``token`` (any state), or None. Opens and fstat's on every call; caches the parse."""
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
    with _cache_lock:
        _state_cache[key] = raw
        while len(_state_cache) > STATE_CACHE_SIZE:
            _state_cache.popitem(last=False)
    return raw


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


def _check_state(raw: dict, now: float) -> tuple[ShareState | None, str]:
    """(state, "") for a live, unexpired, well-formed share; else (None, reason code)."""
    if raw.get("state") != "live":
        return None, "not_live"
    try:
        expires = _expiry(raw.get("expires_at"))
    except (ValueError, TypeError, OverflowError):
        return None, "invalid_state"
    if expires is None and raw.get("audience") == "public":
        return None, "invalid_state"       # public shares always expire: fail closed
    if expires is not None and now >= expires:
        return None, "expired"
    share_id = raw.get("share_id")
    if (not isinstance(share_id, str) or not _SHARE_ID.fullmatch(share_id) or share_id == "by-token"
            or raw.get("audience") not in ("public", "lab")):
        return None, "invalid_state"
    try:
        index = _build_index(raw.get("files"))
        entry = normalise_name(raw.get("entry"))
    except (ValueError, ShareBundleError, TypeError):
        return None, "invalid_state"
    if entry not in index:
        return None, "invalid_state"
    state = ShareState(raw)
    state["entry"] = entry
    state.index = index
    return state, ""


def load_state(root: Path, token: str, now: float) -> ShareState | None:
    """The share's state when ``token`` is well formed and its state is ``live``, unexpired and well formed; else None."""
    raw = _read_state(root, token)
    if raw is None:
        return None
    return _check_state(raw, now)[0]


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


def _content_headers(name: str, etag: str) -> list[tuple[str, str]]:
    content_type = content_type_for(name)
    if content_type == "application/pdf":
        headers = [("Content-Type", "application/pdf"), ("Content-Disposition", "inline"),
                   ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", PDF_CSP)]
    else:
        headers = [("Content-Type", content_type), ("Content-Security-Policy", CSP),
                   ("X-Content-Type-Options", "nosniff")]
    headers += [("Cache-Control", "private, no-cache"), ("Referrer-Policy", "no-referrer"),
                ("X-Robots-Tag", "noindex"), ("ETag", etag), ("Accept-Ranges", "bytes")]
    return headers


def _announces_body(handler) -> bool:
    length = handler.headers.get("Content-Length")
    return bool(handler.headers.get("Transfer-Encoding")) or (length is not None and length.strip() != "0")


def serve_bundle_file(handler, state: dict, rel_path: str, *, cors: bool = True, actor: str = "public") -> bool:
    """Serve ``rel_path`` (raw, still percent-encoded) from ``state``'s bundle; True when served, False after a 404.

    ``state`` is what ``load_state`` returned. Handles HEAD, If-Match (412), If-None-Match (304), If-Range and a single
    Range (206/416); a multi-range request gets the full 200. Streams in ``CHUNK_BYTES`` chunks with ``os.pread``.
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
        headers = _content_headers(name, etag)
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
                handler.wfile.write(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
        except (OSError, ValueError):
            handler.close_connection = True
        return True
    finally:
        os.close(fd)


# ----------------------------------------------------------------------------- handler and server

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
        if self.command == "OPTIONS":
            _send_headers(self, 204, PREFLIGHT_HEADERS)
            return None
        parts = raw_path.split("/")
        if len(parts) < 4 or not TOKEN_RE.fullmatch(parts[2]):
            return deny(self, None, "bad_token")
        is_link = len(parts) == 4 and parts[3] == ""
        is_file = len(parts) >= 5 and parts[3] == "f"
        if not (is_link or is_file):
            return deny(self, None, "unknown_route")
        raw_state = _read_state(self.server.config.shares_root, parts[2])
        if raw_state is None:
            return deny(self, None, "unknown_token")
        state, reason = _check_state(raw_state, self.server.now())
        if state is None:
            return deny(self, raw_state, reason)
        if state.get("audience") != "public":
            return deny(self, state, "audience")    # lab shares get their viewer in the lab routes
        if is_link:
            location = "f/" + quote(state["entry"], safe="/")
            _send_headers(self, 302, [("Location", location), ("Content-Length", "0"), ("Cache-Control", "no-store"),
                                      ("Referrer-Policy", "no-referrer"), ("X-Robots-Tag", "noindex"),
                                      ("X-Content-Type-Options", "nosniff")])
            return None
        serve_bundle_file(self, state, "/".join(parts[4:]))
        return None


class ShareServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, config: ShareConfig, now: Callable[[], float]):
        self.config = config
        self.now = now
        self.events = EventLog(config.events_path, now)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, ShareHandler)

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
