"""Tailnet-only HTTP listener for the assistant hub (``/assistant/mcp``, ``/assistant/enroll``, ``/assistant/hook-events``).

Security posture (all of it enforced here, none of it delegated to a proxy):

* The bind address must lie inside ``allowed_networks`` (so ``0.0.0.0``/``::``/public IPs are refused at construction).
* Every request's peer is the socket's ``client_address``; forwarding headers are never read. A peer outside
  ``peer_networks`` (default: ``allowed_networks``) gets one uniform 403 before a single request byte is parsed.
* Per-peer rate limiting runs BEFORE any authentication, so failed-auth audit entries cannot be flooded.
* Then: Host allow-list, unambiguous framing, no ``Origin``, JSON content type, bounded body, strict JSON parse.
* Errors never echo request content or exception text.

``peer_networks`` exists so a loopback client can exercise peer rejection in tests while the bind stays on loopback.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .events import record_event
from .mcp import Hub
from .mcp import handle as mcp_handle
from .netguard import bind_allowed, host_header_allowed, parse_networks, peer_allowed
from .workers import WorkerAuthError

log = logging.getLogger(__name__)

ROUTE_MCP, ROUTE_ENROLL, ROUTE_HOOK = "/assistant/mcp", "/assistant/enroll", "/assistant/hook-events"
_ROUTES = frozenset({ROUTE_MCP, ROUTE_ENROLL, ROUTE_HOOK})
_MAX_CODE, _MAX_DEVICE = 256, 128
_RATE_WINDOW = 60.0
_MAX_TRACKED_PEERS = 4096
_SOCKET_TIMEOUT = 10
_MAX_CONNECTIONS = 64

_ERRORS = {400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found", 405: "method not allowed",
           413: "request body size not allowed", 415: "application/json required", 429: "too many requests",
           500: "internal", 503: "unavailable"}


class _RateLimiter:
    """Sliding-window counter per key (thread-safe, bounded memory)."""

    def __init__(self, window: float = _RATE_WINDOW) -> None:
        self._window = window
        self._hits: dict[Any, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: Any, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= _MAX_TRACKED_PEERS:
                self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self._window}
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self._window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, family) -> None:
        self.address_family = family
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:  # never print request details
        log.warning("hub connection error")


def _content_length(raw: str | None) -> int | None:
    """Non-negative int, or None when malformed."""
    if raw is None or raw == "":
        return 0
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else None


class HubServer:
    def __init__(self, hub: Hub, *, host: str, port: int, allowed_networks, allowed_hosts,
                 max_body: int = 65536, peer_networks=None, rate_limit: int = 120,
                 enroll_rate_limit: int = 10) -> None:
        networks = parse_networks(allowed_networks)
        if not bind_allowed(host, networks):
            raise ValueError("hub bind address is not inside allowed_networks")
        self.hub = hub
        self.host = host
        self.max_body = max_body
        self.peer_networks = parse_networks(peer_networks) if peer_networks is not None else networks
        self.allowed_hosts = [*allowed_hosts, host]
        self.rate_limit, self.enroll_rate_limit = rate_limit, enroll_rate_limit
        self._limiter = _RateLimiter()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._serving = False
        self._closed = False
        self._lock = threading.Lock()
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self._httpd = _Server((host, port), _make_handler(self), family)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def serve(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._serving = True
        self._ready.set()
        self._httpd.serve_forever(poll_interval=0.2)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve, name="assistant-hub", daemon=True)
        self._thread.start()

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            serving = self._serving
        if serving:
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _make_handler(owner: HubServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = _SOCKET_TIMEOUT
        server_version = "herdr-hub"
        sys_version = ""

        def log_message(self, format, *args) -> None:  # noqa: A002 - request lines may carry secrets
            return

        def handle(self) -> None:
            # Peer check first: nothing from a disallowed peer is parsed.
            if not peer_allowed(self.client_address[0], owner.peer_networks):
                self._reply(403)
                return
            super().handle()

        def send_error(self, code, message=None, explain=None) -> None:
            self._reply(code if code in _ERRORS else 400)
            self.close_connection = True

        def _reply(self, status: int, payload: Any = None, *, head_only: bool = False,
                   extra: dict[str, str] | None = None) -> None:
            body = b"" if payload == b"" else json.dumps(
                payload if payload is not None else {"error": _ERRORS.get(status, "error")}).encode()
            try:
                phrase = HTTPStatus(status).phrase
            except ValueError:
                phrase = "Status"
            lines = [f"HTTP/1.1 {status} {phrase}", "Content-Type: application/json",
                     f"Content-Length: {len(body)}", "Cache-Control: no-store",
                     "X-Content-Type-Options: nosniff", "Connection: close"]
            lines += [f"{k}: {v}" for k, v in (extra or {}).items()]
            data = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head_only else body)
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True

        def _empty(self, status: int) -> None:
            self._reply(status, b"")

        # every HTTP method goes through the same gate so method handling is uniform
        def do_GET(self) -> None: self._serve()      # noqa: E704
        do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = do_PATCH = do_GET

        def _serve(self) -> None:
            try:
                self._serve_checked()
            except (TimeoutError, OSError):
                self.close_connection = True
            except Exception as exc:  # noqa: BLE001 - never leak detail
                log.error("hub request failed (%s)", type(exc).__name__)
                self._reply(500)

        def _serve_checked(self) -> None:
            path = self.path
            peer = self.client_address[0]
            bucket, limit = ((ROUTE_ENROLL, owner.enroll_rate_limit) if path == ROUTE_ENROLL
                             else ("general", owner.rate_limit))
            if not owner._limiter.allow((peer, bucket), limit):
                return self._reply(429, extra={"Retry-After": "60"})
            if not host_header_allowed(self.headers.get("Host"), owner.allowed_hosts) \
                    or len(self.headers.get_all("Host") or []) != 1:
                return self._reply(403)
            if (len(self.headers.get_all("Content-Length") or []) > 1 or "Transfer-Encoding" in self.headers):
                return self._reply(400)
            if self.command in ("GET", "HEAD", "OPTIONS", "DELETE") and self.headers.get("Content-Length") not in (
                    None, "", "0"):
                return self._reply(400)
            if "Origin" in self.headers:
                return self._reply(403)
            if path not in _ROUTES:
                return self._reply(404, head_only=self.command == "HEAD")
            if self.command != "POST":
                return self._reply(405, head_only=self.command == "HEAD", extra={"Allow": "POST"})
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self._reply(415)
            length = _content_length(self.headers.get("Content-Length"))
            if length is None or length <= 0 or length > owner.max_body:
                return self._reply(413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self._reply(400)
            try:
                message = json.loads(raw)
            except (ValueError, RecursionError):
                return self._reply(401 if path == ROUTE_ENROLL else 400)
            if path == ROUTE_MCP:
                return self._mcp(message)
            if path == ROUTE_ENROLL:
                return self._enroll(message)
            return self._hook_events(message)

        def _mcp(self, message: Any) -> None:
            status, payload = mcp_handle(owner.hub, message, self.headers.get("Authorization", ""),
                                         self.headers.get("X-Herdr-Device"))
            if payload is None:
                return self._empty(status)
            self._reply(status, payload)

        def _enroll(self, message: Any) -> None:
            code = message.get("code") if isinstance(message, dict) else None
            device = message.get("device_id") if isinstance(message, dict) else None
            if not (isinstance(code, str) and 0 < len(code) <= _MAX_CODE and isinstance(device, str)
                    and 0 < len(device) <= _MAX_DEVICE and device.isprintable()):
                return self._reply(401)
            try:
                worker, credential = owner.hub.registry.redeem(code, device_id=device)
            except WorkerAuthError:
                return self._reply(401)
            self._reply(200, {"credential": credential, "worker_id": worker.worker_id,
                              "device_id": worker.device_id, "expires_at": worker.expires_at.isoformat()})

        def _hook_events(self, message: Any) -> None:
            if not owner.hub.service.is_enabled():
                return self._reply(503)
            try:
                worker = owner.hub.registry.authenticate(self.headers.get("Authorization", ""),
                                                         self.headers.get("X-Herdr-Device"))
            except WorkerAuthError:
                return self._reply(401)
            if "report" not in worker.scopes:
                return self._reply(403)
            invalid = {"error": "invalid event"}
            if not isinstance(message, dict) or message.get("kind") != "observation":
                return self._reply(400, invalid)
            try:
                event = record_event(owner.hub.store, worker, kind="observation", provenance="harness_attested",
                                     session_id=message.get("session_id"), payload=message.get("payload"))
            except ValueError:
                return self._reply(400, invalid)
            self._reply(200, {"event_id": event.event_id})

    return Handler
