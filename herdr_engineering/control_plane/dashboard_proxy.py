"""Same-origin reverse proxy that lets the browser dashboard work when ``control_plane.api_keys`` is on.

Browsers cannot attach ``X-API-Key`` to ``EventSource`` and the dashboard code sends no key at all, so a keyed
control plane answers every browser request with 401. This proxy sits in front of it for BROWSER traffic only:

* GET/HEAD (static UI, JSON, SSE) are sent upstream with the ``viewer`` key;
* a mutating request is sent with the ``operator`` key only when ``METHOD /path`` matches the configured allowlist
  (default: the dashboard's own write actions); every other mutation is refused here with 403;
* a client-supplied ``X-API-Key`` (and ``X-Forwarded-*``) is always dropped, the browser never sees a key.

Configuration is YAML only (``control_plane.dashboard_proxy``); the keys are read from 0600 files.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

DEFAULT_UPSTREAM = "http://control-plane-web:8080"

# "METHOD /path": ``*`` matches exactly one path segment, a trailing ``**`` matches one or more segments.
# Derived from the write actions in packages/herdr-web-client/client.js and the dashboard views. Server-side
# roles still apply (reviewer worker routes and /api/v1/ingest are deliberately absent).
DEFAULT_OPERATOR_POST_ALLOWLIST: tuple[str, ...] = (
    "POST /api/v1/library/**", "PUT /api/v1/library/**", "DELETE /api/v1/library/**",
    "POST /api/v1/artifacts/**", "PUT /api/v1/artifacts/**",
    "POST /api/v1/artifact-members/**",
    "POST /api/v1/publications/*/approve", "POST /api/v1/publications/*/revoke",
    "POST /api/v1/scheduler/plan", "POST /api/v1/scheduler/host-policy",
    "POST /api/v1/sessions/*/messages", "POST /api/v1/sessions/*/actions", "POST /api/v1/sessions/*/summary",
    "POST /api/v1/sessions/*/summary/**", "POST /api/v1/sessions/*/terminal", "POST /api/v1/sessions/*/inspector/preferences",
    "POST /api/v1/sessions/*/rename", "POST /api/v1/sessions/*/name",
    "POST /api/v1/inspector/preferences",
    "POST /api/v1/attention/ack", "POST /api/v1/attention/snooze", "POST /api/v1/attention/clear",
    "POST /api/v1/architecture/**",
    "POST /api/v1/fabric/resolve-urls",
    "POST /api/v1/ci/actions", "POST /api/v1/ci/debug", "POST /api/v1/ci/fix", "POST /api/v1/ci/fix/*/cancel",
    "POST /api/v1/assistants", "PATCH /api/v1/assistants/*", "DELETE /api/v1/assistants/*",
    "POST /api/v1/assistants/**", "POST /api/v1/assistant-runs/**", "POST /api/v1/assistant-approvals/**",
    "POST /api/v1/assistant-routines", "POST /api/v1/assistant-routines/*/trigger",
    "DELETE /api/v1/assistant-routines/*",
    "POST /api/v1/assistant-memory/**", "PATCH /api/v1/assistant-memory/*", "DELETE /api/v1/assistant-memory/*",
    "POST /api/v1/assistant-connectors", "DELETE /api/v1/assistant-connectors/*",
    "PUT /api/v1/assistant-apps/*/client",
    "POST /api/v1/intelligence/proposals/*/*",
    "POST /api/v1/review-gate/prs/**",
    "POST /api/v1/build-cache/cleanup/preview",
)

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READING = frozenset({"GET", "HEAD"})
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                         "transfer-encoding", "upgrade", "proxy-connection"})
# Dropped on the way upstream in addition: the browser must never choose the credential or the client identity.
_NEVER_FORWARD = frozenset({"x-api-key", "host", "content-length", "x-forwarded-for", "x-forwarded-host",
                            "x-forwarded-proto", "forwarded", "x-real-ip"})
_MAX_BODY = 128 * 1024 * 1024
_ALLOW_ENTRY = re.compile(r"^(POST|PUT|PATCH|DELETE) (/\S*)$")
_SETTINGS_KEYS = frozenset({"viewer_key_file", "operator_key_file", "upstream", "operator_post_allowlist"})


class ProxyConfigError(Exception):
    """The proxy cannot start safely (bad or unsafe key file)."""


@dataclass(frozen=True)
class DashboardProxySettings:
    viewer_key_file: str
    operator_key_file: str | None = None
    upstream: str = DEFAULT_UPSTREAM
    allowlist: tuple[str, ...] = DEFAULT_OPERATOR_POST_ALLOWLIST


def parse_dashboard_proxy(raw) -> DashboardProxySettings | None:
    """Validate the ``control_plane.dashboard_proxy`` block. ``None`` or an empty block means the proxy is off."""
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError("dashboard_proxy must be a mapping")
    unknown = set(raw) - _SETTINGS_KEYS
    if unknown:
        raise ValueError(f"dashboard_proxy: unknown keys {sorted(unknown)}")
    viewer = raw.get("viewer_key_file")
    if not isinstance(viewer, str) or not viewer.strip():
        raise ValueError("dashboard_proxy.viewer_key_file is required (path to a 0600 file holding a viewer key)")
    operator = raw.get("operator_key_file")
    if operator is not None and (not isinstance(operator, str) or not operator.strip()):
        raise ValueError("dashboard_proxy.operator_key_file must be a path string")
    upstream = raw.get("upstream", DEFAULT_UPSTREAM)
    parts = urlsplit(upstream) if isinstance(upstream, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("dashboard_proxy.upstream must be an http(s) URL")
    allow = raw.get("operator_post_allowlist", list(DEFAULT_OPERATOR_POST_ALLOWLIST))
    if not isinstance(allow, list) or not all(isinstance(v, str) and _ALLOW_ENTRY.match(v) for v in allow):
        raise ValueError("dashboard_proxy.operator_post_allowlist must be a list of 'METHOD /path' strings "
                         "(POST, PUT, PATCH or DELETE; * = one segment, trailing ** = the rest)")
    return DashboardProxySettings(viewer.strip(), operator.strip() if operator else None, upstream.rstrip("/"),
                                  tuple(allow))


def read_key_file(path: str) -> str:
    """Read one API key; refuse a missing, empty, or group/other-accessible file."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ProxyConfigError(f"key file {path}: {exc.strerror or exc}") from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProxyConfigError(f"key file {path} is group/other accessible (mode {stat.S_IMODE(st.st_mode):04o}); "
                               "chmod 0600 it")
    with open(path, encoding="utf-8") as fh:
        key = fh.read().strip()
    if not key:
        raise ProxyConfigError(f"key file {path} is empty")
    return key


def _entry_matches(entry: str, method: str, segments: list[str]) -> bool:
    emethod, epath = entry.split(" ", 1)
    if emethod != method:
        return False
    pattern = [s for s in epath.split("/") if s]
    if pattern and pattern[-1] == "**":
        head = pattern[:-1]
        return len(segments) > len(head) and all(p in ("*", s) for p, s in zip(head, segments))
    return len(pattern) == len(segments) and all(p in ("*", s) for p, s in zip(pattern, segments))


def operator_allowed(allowlist: tuple[str, ...], method: str, raw_path: str) -> bool:
    """True when ``method raw_path`` is allowlisted. Ambiguous encodings and dot segments are never allowed."""
    path = raw_path.split("?", 1)[0].split("#", 1)[0]
    low = path.lower()
    if "%2f" in low or "%5c" in low or "\\" in path or "%00" in low or "//" in path:
        return False
    segments = path.split("/")[1:] if path.startswith("/") else None
    if segments is None:
        return False
    decoded = [unquote(s) for s in segments]
    if any(s in ("", ".", "..") or "/" in s for s in decoded) and decoded != [""]:
        return False
    return any(_entry_matches(e, method, decoded) for e in allowlist)


class DashboardProxy:
    """Threaded reverse proxy. ``serve()`` blocks; ``shutdown()`` stops it."""

    def __init__(self, upstream: str, *, viewer_key: str, operator_key: str | None,
                 allowlist: tuple[str, ...], host: str = "0.0.0.0", port: int = 8081):
        parts = urlsplit(upstream)
        self._scheme, self._up_host = parts.scheme, parts.hostname
        self._up_port = parts.port or (443 if parts.scheme == "https" else 80)
        self._up_netloc = parts.netloc
        self._viewer, self._operator, self._allowlist = viewer_key, operator_key, tuple(allowlist)
        self._ready = threading.Event()
        proxy = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._httpd = Server((host, port), _make_handler(proxy))
        self.port = self._httpd.server_address[1]

    def serve(self) -> None:
        self._ready.set()
        self._httpd.serve_forever(poll_interval=0.2)

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        self._ready.wait(timeout)

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def connect(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        return cls(self._up_host, self._up_port, timeout=120)

    def key_for(self, method: str, path: str) -> tuple[str | None, int | None, str]:
        """(key, refusal status, message): the credential the request may use, or why it is refused."""
        if method in _READING:
            return self._viewer, None, ""
        if method in _MUTATING:
            if self._operator is None:
                return None, 403, "proxy: no operator key configured; the dashboard is read-only"
            if not operator_allowed(self._allowlist, method, path):
                return None, 403, "proxy: this write is not in the dashboard allowlist"
            return self._operator, None, ""
        return None, 405, "proxy: method not allowed"


def _make_handler(proxy: DashboardProxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 60

        def log_message(self, fmt, *args):
            pass

        def _refuse(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _forward(self) -> None:
            method = self.command
            key, status, message = proxy.key_for(method, self.path)
            if key is None:
                return self._refuse(status or 403, message)
            if self.headers.get("Transfer-Encoding"):
                return self._refuse(411, "proxy: chunked request bodies are not supported")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._refuse(400, "proxy: bad Content-Length")
            if length < 0 or length > _MAX_BODY:
                return self._refuse(413, "proxy: request body too large")
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP and k.lower() not in _NEVER_FORWARD}
            headers["X-API-Key"] = key
            headers["Host"] = proxy._up_netloc
            headers["Connection"] = "close"
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn = proxy.connect()
            try:
                try:
                    conn.request(method, self.path, body=body, headers=headers)
                    resp = conn.getresponse()
                except OSError:
                    return self._refuse(502, "proxy: control plane unreachable")
                self._relay(method, resp)
            finally:
                conn.close()

        def _relay(self, method: str, resp: http.client.HTTPResponse) -> None:
            self.send_response(resp.status, resp.reason)
            has_length = False
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in _HOP_BY_HOP:
                    continue
                if lk == "content-length":
                    has_length = True
                self.send_header(k, v)
            # Length-less bodies (SSE) stream until upstream closes; the connection ends with them.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if method == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200:
                return
            try:
                while True:
                    chunk = resp.read1(65536) if not has_length else resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError, http.client.HTTPException):
                pass

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

        def do_OPTIONS(self):
            self._refuse(405, "proxy: method not allowed")

    return Handler


def build_from_settings(settings: DashboardProxySettings, host: str, port: int) -> DashboardProxy:
    """Read the key files (refusing unsafe ones) and build the proxy."""
    viewer = read_key_file(settings.viewer_key_file)
    operator = read_key_file(settings.operator_key_file) if settings.operator_key_file else None
    return DashboardProxy(settings.upstream, viewer_key=viewer, operator_key=operator,
                          allowlist=settings.allowlist, host=host, port=port)
