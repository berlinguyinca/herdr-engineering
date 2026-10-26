"""Control-plane HTTP/SSE API server (Spec §20-34, §42-47, §52, §85, §91-92).

The web UI is a *client* of this structured `/api/v1` JSON + SSE stream. Uses
the stdlib http.server (no new deps, consistent with the rest of the repo),
backed by the in-memory `ControlPlaneRepo`. Static web assets (design system +
web components + web client + app) are served from `--web-root`.

Private-by-default: binds loopback (or the tailnet) unless told otherwise.
Optional `X-API-Key` auth for multi-operator deployments (Spec §104).
"""
from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..version import __version__
from .redact import redact_payload

# Routes a host-side tool (session run, plannotator bridge) may POST with the
# narrow `agent` role, besides /api/v1/ingest. Nothing else.
_AGENT_POST = re.compile(r"^/api/v1/(?:llm/keys|reviews|llm/keys/[^/]+/revoke"
                        r"|reviews/[^/]+/(?:heartbeat|result|failure))$")
# Optional build stamp; the compose build arg sets it (see Dockerfile). Falls
# back to "dev" in a local checkout / un-stamped image.
_COMMIT = os.environ.get("HERDR_ENGINEERING_COMMIT", "dev")


def _is_loopback(host: str) -> bool:
    """True if the host we bind resolves to a loopback address (127.0.0.1 / ::1)."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

_WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "packages"
_INDEX = "index.html"
_MAX_JSON_BODY = 4 * 1024 * 1024
_MAX_SSE_SECONDS = 3600


class _DaemonThreadingServer(ThreadingHTTPServer):
    # Handler threads are daemons so a stuck SSE client can never block
    # server shutdown / process exit.
    daemon_threads = True
    # The stdlib default backlog (5) resets connections when dozens of agents
    # emit at once (60+ agents is the design point).
    request_queue_size = 256

    def __init__(self, server_address, handler, *, request_workers=32,
                 request_queue=128):
        self.request_workers = max(1, int(request_workers))
        self.request_queue = max(0, int(request_queue))
        self.request_queue_size = max(5, self.request_queue)
        self._request_slots = threading.BoundedSemaphore(self.request_workers)
        super().__init__(server_address, handler)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()



def _dashboard_host_ok(host_header, extra_hosts):
    """A Host an attacker cannot make point here: an IP literal, localhost, or a configured name.
    DNS rebinding needs a DNS name, so a bare name is refused unless the operator listed it."""
    import ipaddress
    host = host_header.strip().lower()
    if host in extra_hosts:
        return True
    name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True

def _content_length(raw):
    """Non-negative int, or None when the header is malformed (never raises)."""
    if raw is None or raw == "":
        return 0
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 9 else None


class _RateLimiter:
    """Tiny in-memory per-client sliding-window limiter (best-effort).

    Guards against runaway clients / accidental tight loops; not a security
    boundary. Keyed by client IP with a generous default so it never hinders
    normal use within the Tailnet. SSE streams are excluded (long-lived).
    """

    def __init__(self, max_requests=600, window_seconds=60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits = {}  # key -> list of monotonic timestamps
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                return False
            hits.append(now)
            if len(self._hits) > 20000:   # bounded: forget keys with no recent hits
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
            self._hits[key] = hits
            return True


_FLEET_CONFIG_PATH = "/api/v1/fleet/config"


class ControlPlaneServer:
    """Structured control-plane API + SSE + static web server."""

    def __init__(self, repo, *, api_key=None, api_keys=None, host="127.0.0.1",
                 port=0, web_root=None, source=None, allow_open=False,
                 rustfs=None, dispatcher=None, github_monitor=None,
                 collab=None, collab_tls_terminated=False,
                 collab_require_https=False, fleet_config=None, ci_actions=False,
                 public_port=None, public_host=None, collab_only=False,
                 operator_port=None, operator_user=None, operator_email=None,
                 operator_hosts=(), collab_operator_url=None, collab_dashboard_hosts=(),
                 request_workers=32, request_queue=128):
        """
        Auth (Spec §104): either a single `api_key` (all-access) or a mapping
        `api_keys={key: role}` with roles "viewer" (read-only) / "operator"
        (read + mutate) / "ingest" (read + POST /api/v1/ingest only).
        No key configured = open (private-by-default bind).

        `allow_open` is an explicit opt-in that permits an unauthenticated
        server on a non-loopback bind. Only use it when the host port is bound
        to the Tailscale interface (or proxied by `tailscale serve`), because
        the Tailnet is then the trust boundary — all machines belong to each
        other and are encrypted end-to-end via WireGuard.
        """
        self.repo = repo
        from .ci_monitor import CIMonitor
        self.ci_monitor = CIMonitor(on_transition=lambda key, event, row:
                                    repo.record_event("ci_pipeline", key, event, row),
                                    actions_enabled=ci_actions)
        if github_monitor is None:
            from ..github import GitHubMonitor
            github_monitor = GitHubMonitor()
        self.github_monitor = github_monitor
        self.dispatcher = dispatcher or self._dispatch_native_message
        if source is not None:
            from .bridge import load_into
            load_into(repo, source)
        self.source = source
        # Optional durable object store (RustFS / S3). When wired, artifact
        # payloads are stored content-addressed here instead of the repo's
        # in-memory blob stand-in, so they survive a web restart.
        self.rustfs = rustfs
        from .ingest import IngestService
        self.ingest = IngestService(repo)
        self._limiter = _RateLimiter()
        # Fleet telemetry has a separate bounded budget so bursts cannot starve
        # dashboard reads or operator commands from the same host/proxy IP.
        self._ingest_limiter = _RateLimiter(max_requests=6000)
        self._stream_limiter = _RateLimiter(max_requests=120)
        # Fail-closed (Spec §104): never default a blank/missing role to
        # "operator". Keep roles verbatim after strip; empty/None/unknown
        # roles are treated as "viewer" (read-only) by `_require`.
        self._keys = {k: (v.strip() if isinstance(v, str) else "")
                      for k, v in (api_keys or {}).items()}
        if api_key is not None:
            self._keys[api_key] = "operator"
        self.api_key = api_key  # kept for backward compat / introspection
        self.allow_open = allow_open
        # Fail-closed (Spec §104): an unauthenticated server is only ever
        # allowed on a loopback bind unless the operator explicitly opts in
        # with allow_open (Tailnet trust boundary). Never silently exposed.
        if not self._keys and not _is_loopback(host) and not allow_open:
            raise ValueError(
                "refusing to bind an unauthenticated control plane to a "
                f"non-loopback address {host!r}; configure keys in the central "
                "fleet config (control_plane.api_keys) or --api-keys, or pass "
                "--allow-open to trust the Tailnet boundary (Spec §104)")
        # HCN (feature-flagged, off by default). Bearer credentials must never
        # cross a network in clear text, so a non-loopback bind needs an explicit
        # assertion that TLS is terminated in front of this server.
        if (collab is not None and not _is_loopback(host)
                and not (collab_tls_terminated or collab_require_https)):
            raise ValueError(
                "refusing to enable collaboration on non-loopback address "
                f"{host!r} without collab_tls_terminated (bearer credentials "
                "require TLS)")
        self.fleet_config = fleet_config
        self.collab = collab
        self.collab_require_https = collab_require_https
        # Optional second listener that serves ONLY collaboration (API, MCP and its page);
        # it is the only one an internet-facing proxy should be pointed at.
        if public_port is not None and collab is None:
            raise ValueError("a public collaboration listener needs collab enabled")
        self.collab_only = collab_only
        if operator_port is not None and not (collab_only and operator_user and operator_hosts):
            raise ValueError("the operator listener needs collab-only mode, a user and allowed hosts")
        self.operator_port, self.operator_user = operator_port, operator_user
        self.operator_email = operator_email
        self.operator_hosts = frozenset(h.lower() for h in operator_hosts)
        self._operator_httpd = None
        # Dashboard side: forward /api/v1/collab/* to the operator listener of the isolated
        # collab service, so the local owner needs no sign-in.
        self.collab_operator_url = collab_operator_url.rstrip("/") if collab_operator_url else None
        self.collab_dashboard_hosts = frozenset(h.lower() for h in collab_dashboard_hosts)
        if collab_only and public_port is None:
            raise ValueError("collab-only mode needs the public collaboration port")
        self.public_port = public_port
        self.public_host = public_host or host
        self._public_httpd = None
        if collab is not None:
            collab.hydrate(repo.take_collab_replay())
        self.host = host
        self.port = port
        self.request_workers = max(1, int(request_workers))
        self.request_queue = max(0, int(request_queue))
        self.web_root = Path(web_root) if web_root else _WEB_ROOT
        self._subscribers = set()
        self._lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._metrics = {"requests": 0, "by_path": {}, "total_ms": 0.0}
        self._ready = threading.Event()
        self._httpd = None
        # live stream (Spec §52)
        repo.subscribe(self._broadcast)

    def _dispatch_native_message(self, record, message, artifact_ids):
        from ..errors import InvalidError, UnsupportedError
        from ..herdr_adapter import NativeHerdrAdapter

        if artifact_ids:
            raise UnsupportedError("native prompt attachment delivery is not implemented")
        target = record.get("terminal_id")
        conversation = record.get("agent_session")
        if not target or not conversation or not record.get("host_id"):
            raise InvalidError("native terminal, conversation and host identity are required")
        adapter = NativeHerdrAdapter()
        machine = record.get("machine")
        if not machine and record["host_id"] not in {socket.gethostname(), socket.getfqdn()}:
            resolved = adapter.resolve_machine(record["host_id"])
            if resolved is None:
                raise InvalidError("remote host has no explicit native machine route")
            machine = resolved.id
        server = record.get("herdr_session")
        if not server:
            raise InvalidError("named HerdR server identity is required")
        agents = adapter.list_agents(machine=machine, session=server)
        matches = [agent for agent in agents if agent.terminal_id == target]
        if len(matches) != 1 or matches[0].agent_session != conversation:
            raise InvalidError("native session identity changed; refresh before delivery")
        if matches[0].interactive_ready is False or matches[0].launch_pending:
            raise InvalidError("native agent is not ready for prompt delivery")
        return adapter.send_input(target, message, machine=machine, session=server)

    def _health(self):
        """Repo stats + deployment context (source backend, image version/commit)."""
        h = self.repo.health()
        h["source"] = "postgres" if self.source is not None else "memory"
        h["rustfs"] = "s3" if self.rustfs is not None else "memory"
        h["version"] = __version__
        h["commit"] = _COMMIT
        h["capabilities"] = ["mission_activity_v1", "ci_v1", "plannotator_reviews_v1", "github_v1"]
        if self.collab is not None:
            h["capabilities"].append("collab_v1")
        with self._metrics_lock:
            h["performance"] = {
                "requests": self._metrics["requests"],
                "by_path": dict(self._metrics["by_path"]),
                "total_ms": round(self._metrics["total_ms"], 2),
                "request_workers": self.request_workers,
                "request_queue": self.request_queue,
            }
        return h

    def _record_request(self, path: str, elapsed_ms: float) -> None:
        path = path.split("?", 1)[0][:120]
        with self._metrics_lock:
            self._metrics["requests"] += 1
            self._metrics["by_path"][path] = self._metrics["by_path"].get(path, 0) + 1
            self._metrics["total_ms"] += elapsed_ms

    # ------------------------------------------------------- SSE plumbing
    def _broadcast(self, event):
        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    # ------------------------------------------------------------- server
    def serve(self):
        if self.collab_only:
            # No operator listener at all: this process serves the public collab surface only.
            self._httpd = _DaemonThreadingServer(
                (self.public_host, self.public_port), _make_handler(self, public=True))
            self.public_port = self.port = self._httpd.server_address[1]
            if self.operator_port is not None:
                self._operator_httpd = _DaemonThreadingServer(
                    (self.public_host, self.operator_port), _make_handler(self, operator=True))
                self.operator_port = self._operator_httpd.server_address[1]
                threading.Thread(target=self._operator_httpd.serve_forever, daemon=True).start()
            self._ready.set()
            self._httpd.serve_forever()
            return
        self._httpd = _DaemonThreadingServer(
            (self.host, self.port), _make_handler(self),
            request_workers=self.request_workers, request_queue=self.request_queue)
        self.port = self._httpd.server_address[1]
        if self.public_port is not None:
            self._public_httpd = _DaemonThreadingServer(
                (self.public_host, self.public_port), _make_handler(self, public=True))
            self.public_port = self._public_httpd.server_address[1]
            threading.Thread(target=self._public_httpd.serve_forever, daemon=True).start()
        self._ready.set()
        threading.Thread(target=self._sweep_loop, daemon=True).start()
        self._httpd.serve_forever()

    def _sweep_loop(self, interval=300):
        """Apply inspector age retention to idle sessions too (ingest only prunes the active one)."""
        while self._httpd is not None:
            time.sleep(interval)
            try:
                self.repo.inspector.sweep()
            except Exception:
                pass

    def wait_until_ready(self, timeout=5.0):
        self._ready.wait(timeout=timeout)

    def shutdown(self):
        self.ci_monitor.close()
        self.github_monitor.close()
        if self._operator_httpd:
            self._operator_httpd.shutdown()
            self._operator_httpd.server_close()
        if self._public_httpd:
            self._public_httpd.shutdown()
            self._public_httpd.server_close()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


def _fabric_session(repo, inst):
    """The fabric session an agent's instance_id points at. Besides a full session id it may be
    ``pane:<host>:<pane id>`` (what a host-side runner knows right after it opens a herdr pane, before
    the pane's plugin has reported its server name); that resolves to the newest session on that pane."""
    sess = repo.get_session(inst)
    if sess or not inst.startswith("pane:"):
        return sess
    _, _, rest = inst.partition(":")
    host, _, pane = rest.partition(":")
    for cand in repo.list_sessions():                  # newest first
        if cand.get("host_id") == host and cand.get("pane_id") == pane:
            return cand
    return None


def _fabric_enriched_flow(repo, payload: bytes) -> bytes:
    """Dashboard side only: join the owner's own agents (instance_id = a fabric session id) to the
    sessions, hosts and missions this control plane knows. The collab service never sees fabric data."""
    try:
        flow = json.loads(payload)
        if "nodes" not in flow:                      # a room-state reply carries the flow under "flow"
            if not isinstance(flow.get("flow"), dict):
                return payload
            enriched = json.loads(_fabric_enriched_flow(repo, json.dumps(flow["flow"]).encode()))
            flow["flow"] = enriched
            return json.dumps(flow).encode()
        for node in flow.get("nodes", []):
            inst = node.get("instance_id")
            if node.get("kind") != "agent" or not inst:
                continue
            sess = _fabric_session(repo, inst)
            if not sess:
                node["fabric"] = {"session_id": inst, "status": "unknown"}
                continue
            mission = repo.get_mission(sess.get("mission_id")) if sess.get("mission_id") else None
            node["fabric"] = {"session_id": sess.get("session_id", inst), "status": sess.get("status"), "host": sess.get("host_id"),
                              "agent_role": sess.get("agent_role"), "mission_id": sess.get("mission_id"),
                              "mission_title": (mission or {}).get("title"), "cwd": sess.get("cwd"),
                              "branch": sess.get("branch"), "name": sess.get("name"),
                              "parent_session_id": sess.get("parent_session_id")}
        return json.dumps(flow).encode()
    except Exception:           # the flow view must never break because fabric data is odd
        return payload


def _with_collab_tasks(server, repo, mission_key, timeline):
    """Add the collaboration tasks of agents linked to this mission's sessions to its timeline.
    Dashboard side only (it asks the operator listener); statuses and names, never task text. Any
    failure leaves the timeline as it was."""
    if not server.collab_operator_url:
        return timeline
    try:
        m = repo.get_mission(mission_key)
        keys = {mission_key, (m or {}).get("mission_id"), (m or {}).get("mission_key")} - {None}
        ids = []
        for s in repo.list_sessions():
            if s.get("mission_id") in keys:             # an agent may be linked by full id or by pane
                ids += [s["session_id"], f"pane:{s.get('host_id')}:{s.get('pane_id')}"]
        ids = ids[:100]
        if not ids:
            return timeline
        req = urllib.request.Request(
            server.collab_operator_url + "/api/v1/collab/fleet/task-activity",
            data=json.dumps({"instance_ids": ids}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:   # noqa: S310 (operator-configured URL)
            rows = json.loads(resp.read(1_000_000)).get("activity", [])
        extra = [{"ts": r["updated_at"], "kind": "collab", "status": r["status"],
                  "title": f"@{r['agent']} {r['direction']} a task in {r.get('room') or 'a room'} ({r['status']})",
                  "ref": None, "entity_id": r["task_id"]} for r in rows]
        return sorted(timeline + extra, key=lambda i: (i["ts"] or "", i["kind"], i["entity_id"] or ""))
    except Exception:
        return timeline


_PUBLIC_STATIC = {
    "/": ("collab.html", "text/html; charset=utf-8"),
    "/collab-app.js": ("collab-app.js", "text/javascript"),
    "/collab.css": ("collab.css", "text/css"),
    "/collab-view.js": ("collab-view.js", "text/javascript"),
    "/collab-scripts.js": ("collab-scripts.js", "text/javascript"),
    "/collab-flow.js": ("collab-flow.js", "text/javascript"),
    "/collab-mention.js": ("collab-mention.js", "text/javascript"),
    "/conversation-composer.js": ("conversation-composer.js", "text/javascript"),
    "/conversation-markdown.js": ("conversation-markdown.js", "text/javascript"),
    "/conversation.css": ("conversation.css", "text/css"),
    "/guide": ("collab-guide.html", "text/html; charset=utf-8"),
    "/collab-guide.js": ("collab-guide.js", "text/javascript"),
    "/collab-guide.md": ("collab-guide.md", "text/markdown; charset=utf-8"),
    "/herdr-web-client/collab.js": ("herdr-web-client/collab.js", "text/javascript"),
    "/herdr-web-components/util.js": ("herdr-web-components/util.js", "text/javascript"),
    "/herdr-design-system/theme.css": ("herdr-design-system/theme.css", "text/css"),
    "/herdr-design-system/base.css": ("herdr-design-system/base.css", "text/css"),
}
_SCRIPT_FILES = {"join.sh": "join.sh", "worker.sh": "worker.sh", "sandbox.sh": "sandbox.sh"}
_PUBLIC_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "connect-src 'self' https://*.amazoncognito.com; img-src 'self' data:; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _make_handler(server, public=False, operator=False):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "HerdrControlPlane/0.1"
        protocol_version = "HTTP/1.1"
        # Public listener only: a client that stalls mid-request cannot hold a thread for ever.
        # (The operator listener is tailnet-only and keeps its blocking behaviour.)
        timeout = 30 if public else None

        def log_message(self, fmt, *args):  # keep test output clean
            pass

        # ------------------------------------------------------------ auth
        def _role(self):
            if not server._keys:
                # open — only reachable via loopback or an explicit --allow-open
                # (Tailnet trust boundary); the fail-closed guard enforces this
                return "operator"
            supplied = self.headers.get("X-API-Key", "")
            # constant-time compare to avoid a timing side channel
            for candidate, role in server._keys.items():
                if secrets.compare_digest(supplied, candidate):
                    return role
            return None

        def _deny(self, status, message):
            """Send the error response, close the connection, and report the refusal.

            The request body (if any) is deliberately left unread, so the connection
            must not be reused: on keep-alive the unread bytes would be parsed as the
            next request (request smuggling behind a filtering proxy).
            """
            payload = json.dumps({"error": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return True

        def _require(self, min_role):
            """Return True when the request was refused (response already sent), else None.

            Role model (Spec §104): viewer = read-only; operator = read +
            mutate; ingest = read + POST /api/v1/ingest only (read-only
            everywhere else); agent = ingest plus exactly the host-side tool routes
            (LLM key mint/revoke, review create/result). Unknown roles are treated as viewer (fail-closed
            for mutations).

            The return value MUST be truthy on refusal: callers do
            ``if self._require(...): return``. (It used to return the result of
            ``_send_json`` -- always None -- so a 401/403 was sent and the request
            then executed anyway.)
            """
            role = self._role()
            if role is None:
                return self._deny(401, "unauthorized")
            if min_role == "operator":
                if role == "operator":
                    return None
                path = self.path.split("?")[0]
                if (role == "ingest" and self.command == "POST"
                        and path == "/api/v1/ingest"):
                    return None
                if (role == "agent" and self.command == "POST"
                        and (path == "/api/v1/ingest" or _AGENT_POST.match(path))):
                    return None
                return self._deny(403, "read-only")
            if min_role == "ingest":
                if role in ("operator", "ingest", "agent"):
                    return None
                return self._deny(403, "read-only")
            return None

        # ------------------------------------------------------------ routes
        # -------------------------------------------- public (collab-only) listener
        def _public(self):
            """Everything not on the allow-list is a plain 404: the legacy API, the
            dashboard and its static files do not exist on this listener."""
            path = self.path.split("?")[0]
            # Request framing must be unambiguous: the edge and this server must agree where a
            # request ends, or a pipelined second request could be smuggled behind the first.
            cl = self.headers.get_all("Content-Length") or []
            if (len(cl) > 1 or "Transfer-Encoding" in self.headers
                    or (self.command == "GET" and cl and cl[0].strip() not in ("", "0"))):
                return self._deny(400, "ambiguous request framing")
            if path == "/.well-known/agent-card.json" and self.command == "GET":
                return self._agent_card()
            if self.command == "GET" and path.startswith("/api/v1/collab/scripts/"):
                return self._script(path.rsplit("/", 1)[1])
            if self._is_collab_path() or path in ("/mcp", "/a2a"):
                if path == "/mcp":
                    return self._mcp()
                return self._a2a() if path == "/a2a" else self._collab()
            if self.command == "GET" and path in _PUBLIC_STATIC:
                name, ctype = _PUBLIC_STATIC[path]
                target = (server.web_root / name)
                if not target.is_file():
                    return self._deny(404, "not found")
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Security-Policy", _PUBLIC_CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(data)
                return None
            return self._deny(404, "not found")

        def _script(self, name):
            """The bash a `curl ... | bash` runs: templates with this site's address filled in. They contain no
            secret (the join token travels in the caller's environment, never in the URL or these files)."""
            import re as _re
            if server.collab is None or name not in _SCRIPT_FILES:
                return self._deny(404, "not found")
            if self._rate_limit(self._proxied_client()):
                return None
            if (server.collab_require_https
                    and self.headers.get("X-Forwarded-Proto", "").lower() != "https"):
                return self._deny(400, "collaboration is only served over https")
            base = ((server.collab.public_config or {}).get("public_url") or "").rstrip("/")
            target = server.web_root / "collab-scripts" / _SCRIPT_FILES[name]
            if not _re.fullmatch(r"(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:[0-9]{1,5})?", base) or not target.is_file():
                return self._deny(404, "not found")
            text = target.read_text(encoding="utf-8").replace("'__HERDR_BASE__'", f"'{base}'").replace("__HERDR_BASE__", base)
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return None

        def _agent_card(self):
            from . import collab_a2a
            if server.collab is None:
                return self._deny(404, "not found")
            base = (server.collab.public_config or {}).get("public_url") or ""
            if not base:
                return self._deny(404, "not found")
            self.send_response(200)
            data = json.dumps(collab_a2a.agent_card(base)).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _a2a(self):
            from . import collab_a2a
            return self._jsonrpc(collab_a2a.handle, "A2A")

        def _mcp(self):
            from . import collab_mcp
            return self._jsonrpc(collab_mcp.handle, "MCP")

        def _jsonrpc(self, handle, label):
            from .collab import MAX_BODY
            if server.collab is None:
                return self._deny(404, "not found")
            if self._rate_limit(self._proxied_client()):
                return None
            if self.command != "POST":
                return self._deny(405, "POST only")
            if (server.collab_require_https
                    and self.headers.get("X-Forwarded-Proto", "").lower() != "https"):
                return self._deny(400, "collaboration is only served over https")
            if self.headers.get("Origin"):
                return self._deny(403, f"browser origins may not call the {label} endpoint")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                return self._deny(415, "application/json required")
            length = _content_length(self.headers.get("Content-Length"))
            if length is None or length <= 0 or length > MAX_BODY:
                return self._deny(413, "request body size not allowed")
            try:
                message = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return self._json({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "parse error"}}, 400)
            try:
                status, payload = handle(
                    server.collab, message, self.headers.get("Authorization", ""),
                    self.headers.get("X-Herdr-Device"))
            except Exception:
                return self._json({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32603, "message": "internal error"}}, 500)
            if payload is None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._json(payload, status)

        def do_GET(self):
            started = time.monotonic()
            try:
                return self._do_GET()
            finally:
                server._record_request(self.path, (time.monotonic() - started) * 1000)

        def _do_GET(self):
            if operator:
                return self._operator()
            if public:
                return self._public()
            if self._is_collab_path():
                return self._collab()
            if self._path_only() == _FLEET_CONFIG_PATH:
                return self._fleet_config_get()
            if self._path_only() == "/api/v1/health" and self._role() is None:
                # Keyed mode without a valid key: liveness only (deploy.sh's health
                # check needs status + commit), no counters, sources or capabilities.
                h = server._health()
                return self._json({k: h.get(k) for k in ("status", "version", "commit")})
            err = self._require("viewer")
            if err:
                return err
            if self.path.startswith("/api/v1/"):
                return self._route_api()
            return self._serve_static()

        def do_POST(self):
            started = time.monotonic()
            try:
                return self._do_POST()
            finally:
                server._record_request(self.path, (time.monotonic() - started) * 1000)

        def _do_POST(self):
            if operator:
                return self._operator()
            if public:
                return self._public()
            if self._is_collab_path():
                return self._collab()
            if self._path_only() == _FLEET_CONFIG_PATH + "/applied":
                return self._fleet_config_applied()
            # The ingest route is reachable by the ingest role; every other
            # POST requires the operator role (Spec §104).
            path = self.path.split("?")[0]
            if path.startswith(("/api/v1/ci/", "/api/v1/reviews")):
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin integration mutation refused")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._deny(415, "integration mutations require application/json")
            min_role = ("ingest" if path == "/api/v1/ingest"
                       else "operator")
            err = self._require(min_role)
            if err:
                return err
            if self.path.startswith("/api/v1/"):
                return self._route_api()
            return self._send_json(404, {"error": "not found"})

        # ------------------------------------------------ fleet config (hosts)
        def _path_only(self):
            return self.path.split("?")[0].rstrip("/")

        def _fleet_host(self, cfg):
            """Host id owning the X-Host-Token in ``cfg``, or None. Never reveals why.
            Callers pass ONE snapshot per request, so a reload between the token
            check and the slice cannot hand out (or fail on) a different file."""
            token = self.headers.get("X-Host-Token", "")
            return cfg.host_for_token(token) if token else None

        def _fleet_config_get(self):
            """Host endpoints bypass the X-API-Key gate on purpose: the per-host
            token is their credential, and an operator key grants no host slice."""
            store = server.fleet_config
            if store is None:
                return self._deny(404, "not found")
            cfg = store.current()
            host = self._fleet_host(cfg)
            if host is None:
                return self._deny(401, "unauthorized")
            return self._json({"revision": cfg.revision, "values": cfg.slice_for(host)})

        def _fleet_config_applied(self):
            store = server.fleet_config
            if store is None:
                return self._deny(404, "not found")
            host = self._fleet_host(store.current())
            if host is None:
                return self._deny(401, "unauthorized")
            try:
                body = self._read_json()
            except ValueError as exc:
                return self._deny(400, str(exc))
            if (not isinstance(body, dict) or body.get("host_id") != host
                    or not isinstance(body.get("revision"), str)
                    or not body["revision"] or len(body["revision"]) > 64):
                return self._deny(401, "unauthorized")
            store.record_applied(host, body["revision"])
            return self._json({"ok": True})

        # --------------------------------------------------- collaboration (HCN)
        def _is_collab_path(self):
            path = self.path.split("?")[0]
            return path == "/api/v1/collab" or path.startswith("/api/v1/collab/")

        def _operator(self):
            """Tailnet-only listener for the local owner: collaboration API only, no sign-in.

            Open by design (the tailnet is the trust boundary, as for the dashboard), so it
            refuses DNS-rebinding (Host allowlist), cross-site writes (Origin check and
            application/json in _collab) and every path outside /api/v1/collab."""
            cl = self.headers.get_all("Content-Length") or []
            if (len(cl) > 1 or "Transfer-Encoding" in self.headers
                    or (self.command == "GET" and cl and cl[0].strip() not in ("", "0"))):
                return self._deny(400, "ambiguous request framing")
            if (self.headers.get("Host", "").lower() not in server.operator_hosts
                    or not self._is_collab_path()):
                return self._deny(404, "not found")
            return self._collab(operator=True)

        def _collab_proxy(self):
            """Dashboard -> collab service operator listener (same user, no sign-in)."""
            err = self._require("operator")
            if err:
                return err
            # This path acts as the owner without any credential, so it needs the same browser
            # defences as the operator listener: no DNS-rebound names, no cross-site writes.
            if not _dashboard_host_ok(self.headers.get("Host", ""), server.collab_dashboard_hosts):
                return self._deny(404, "not found")
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin mutation refused")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._deny(415, "mutations require application/json")
            from .collab import MAX_BODY
            length = _content_length(self.headers.get("Content-Length"))
            if self.command == "POST" and (length is None or length > MAX_BODY):
                return self._deny(413, "request body too large")
            data = self.rfile.read(length) if self.command == "POST" and length else None
            req = urllib.request.Request(
                server.collab_operator_url + self.path, data=data, method=self.command,
                headers={"Content-Type": "application/json"} if data is not None or self.command == "POST" else {})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:   # noqa: S310 (operator-configured URL)
                    status, payload = resp.status, resp.read(2_000_000)
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, exc.read(2_000_000)
            except Exception:
                return self._json({"error": "collaboration service unavailable"}, 502)
            if status == 200 and self.command == "GET" and urlsplit(self.path).path.endswith(("/flow", "/state")):
                payload = _fabric_enriched_flow(server.repo, payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _collab(self, operator=False):
            """HCN routes bypass the legacy X-API-Key gate on purpose: legacy keys are
            shared secrets with no identity, so they grant nothing here. Authentication
            is per-user / per-agent (collab.authenticate)."""
            service = server.collab
            if service is None:
                if server.collab_operator_url and not operator and not public:
                    return self._collab_proxy()
                return self._deny(404, "not found")
            if self._rate_limit(self._proxied_client()):
                return None
            # On a shared plain-HTTP listener, refuse collaboration unless the request
            # came through the TLS-terminating proxy (`tailscale serve` sets this header).
            if (server.collab_require_https and not operator
                    and self.headers.get("X-Forwarded-Proto", "").lower() != "https"):
                return self._deny(400, "collaboration is only served over https")
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and (urlsplit(origin).netloc != self.headers.get("Host")
                               or urlsplit(origin).scheme not in {"http", "https"}):
                    return self._deny(403, "cross-origin mutation refused")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._deny(415, "mutations require application/json")
            body = {}
            if self.command == "POST":
                from .collab import MAX_BODY
                length = _content_length(self.headers.get("Content-Length"))
                if length is None or length > MAX_BODY:
                    return self._deny(413, "request body too large")
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except (ValueError, RecursionError):
                        return self._json({"error": "invalid JSON body"}, 400)
                    if not isinstance(body, dict):
                        return self._json({"error": "JSON object required"}, 400)
            split = urlsplit(self.path)
            parts = [unquote(seg) for seg in split.path.split("/")[4:] if seg]
            query = {k: v[0] for k, v in parse_qs(split.query).items()}
            if operator and self.command == "GET" and parts == ["config"]:
                return self._json({**service.public_config, "operator": {
                    "user_id": server.operator_user, "email": server.operator_email}}, 200)
            try:
                result = service.dispatch(self.command, parts, query, body,
                                          self.headers.get("Authorization", ""),
                                          self.headers.get("X-Herdr-Device"),
                                          operator=server.operator_user if operator else None)
            except Exception:
                return self._json({"error": "internal error"}, 500)  # never echo internals
            if isinstance(result, tuple):
                return self._json(result[1], result[0])
            return self._collab_sse(service, result)

        def _collab_sse(self, service, stream):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.connection.settimeout(30)
            except OSError:
                pass
            last = time.monotonic()
            frames = service.pump(stream, max_seconds=_MAX_SSE_SECONDS)
            try:
                for frame in frames:
                    if frame is None:
                        if time.monotonic() - last < 15:
                            continue
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        self.wfile.write(_raw_frame(frame))
                    self.wfile.flush()
                    last = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                frames.close()
            return None

        def _proxied_client(self):
            """Behind `tailscale serve` every peer is 127.0.0.1; key the limiter on the
            forwarded address, but only trust it from a loopback peer (the proxy)."""
            peer = self.client_address[0]
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            # The public listener is reachable only from the host's own proxy (possibly
            # across the Docker bridge), so a private peer is trusted there too.
            try:
                trusted = _is_loopback(peer) or (public and ipaddress.ip_address(peer).is_private)
            except ValueError:
                trusted = False
            return fwd[:64] if fwd and trusted else peer

        def _rate_limit(self, key=None):
            path = self.path.split("?")[0]
            who = key or self.client_address[0]
            if self.command == "GET" and path.rstrip("/").endswith("/events/stream"):
                # SSE is long-lived, so it is not counted like request bursts, but how often a
                # client may open one is still bounded (it costs a token check each time).
                limiter = server._stream_limiter
            elif self.command == "POST" and path == "/api/v1/ingest":
                limiter = server._ingest_limiter
            else:
                limiter = server._limiter
            if limiter.allow(who):
                return None
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(limiter.window))
            # The rejected POST body is unread. Close rather than interpreting
            # its bytes as another request on this HTTP/1.1 connection.
            self.send_header("Connection", "close")
            self.close_connection = True
            payload = json.dumps({"error": "too many requests"}).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return True

        def _route_api(self):
            path = self.path.split("?")[0]
            # URL-decode each path segment: entity ids like ``w9:p5`` arrive
            # percent-encoded (``w9%3Ap5``) from the structured client
            # (encodeURIComponent), so decode before matching. ``unquote``
            # (not unquote_plus) keeps ``+`` literal, as path segments should.
            parts = [unquote(seg) for seg in path.split("/")]
            resource = parts[3]
            if self._rate_limit():
                return None
            try:
                if self.command == "GET":
                    return self._api_get(parts, resource)
                return self._api_post(parts, resource)
            except KeyError:
                return self._send_json(404, {"error": "not found"})
            except ValueError as exc:
                message = "Invalid GitHub query." if resource == "github" else str(exc)
                return self._send_json(400, {"error": message})
            except RuntimeError as exc:
                if resource == "github":
                    from ..github import GitHubError
                    if isinstance(exc, GitHubError):
                        status = {"not_found": 404, "permission": 403, "rate_limited": 429}.get(exc.reason, 503)
                        return self._send_json(status, {"error": str(exc), "reason": exc.reason})
                if resource == "reviews":
                    return self._send_json(503, {"error": "Review persistence is unavailable; no launch or decision was acknowledged."})
                return self._send_json(503, {"error": "Integration service is unavailable."})
            except Exception as exc:  # never drop a connection: surface 500
                message = "GitHub service is unavailable." if resource == "github" else str(exc)
                return self._send_json(500, {"error": message})

        def _api_get(self, parts, resource):
            r = server.repo
            if resource == "health":
                return self._json(server._health())
            if resource == "mission-activity" and len(parts) == 4:
                return self._json({"activities": r.mission_activity.list_summaries(),
                                   "server_time": r._stamp()})
            if resource == "missions":
                if len(parts) == 6 and parts[5] == "activity":
                    return self._json({"activity": r.mission_activity.get(parts[4])})
                if len(parts) > 5 and parts[5] == "timeline":
                    timeline = r.mission_timeline(parts[4])
                    return self._json({"timeline": _with_collab_tasks(server, r, parts[4], timeline)})
                if len(parts) > 4:
                    m = r.get_mission(parts[4])
                    return self._json(m) if m else self._json(
                        {"error": "not found"}, 404)
                status = _q(self.path, "status")
                return self._json({"missions": r.list_missions(status)})
            if resource == "inspector":
                return self._inspector_global_get(parts)
            if resource == "sessions" and len(parts) > 5 and parts[5] == "inspector":
                return self._inspector_get(parts)
            if resource == "sessions":
                if len(parts) > 4:
                    s = r.get_session(parts[4])
                    return self._json(s) if s else self._json(
                        {"error": "not found"}, 404)
                status = _q(self.path, "status")
                return self._json({"sessions": r.list_sessions(status)})
            if resource == "services":
                if len(parts) > 4:
                    s = r.get_service(parts[4])
                    return self._json(s) if s else self._json(
                        {"error": "not found"}, 404)
                services = r.list_services()
                host_filter = _q(self.path, "host_id")
                if host_filter:
                    services = [s for s in services if s.get("host_id") == host_filter]
                summary = {state: sum(s["lifecycle"]["state"] == state for s in services)
                           for state in ("current", "unavailable", "archived", "unknown")}
                summary["total"] = len(services)
                if _q(self.path, "include_archived") in {"false", "0"}:
                    services = [s for s in services if not s["lifecycle"]["archived"]]
                if _q(self.path, "fields") == "feed":
                    # The ownership feed needs identity and proof only; the full
                    # catalog projection is most of the bytes of a large inventory.
                    services = [{k: s.get(k) for k in _FEED_SERVICE_FIELDS} for s in services]
                return self._json({"services": services, "summary": summary})
            if resource == "fleet" and parts[4:] == ["config", "status"]:
                if server.fleet_config is None:
                    return self._json({"error": "not found"}, 404)
                return self._json(server.fleet_config.status())
            if resource == "fleet" and len(parts) == 5 and parts[4] == "inventory":
                return self._json(r.fleet_inventory())
            if resource == "hosts":
                if len(parts) > 5 and parts[5] == "telemetry":
                    return self._json({"samples": r.list_telemetry(parts[4])})
                if len(parts) == 5:
                    h = r.get_host(parts[4])
                    return self._json(h) if h else self._json(
                        {"error": "not found"}, 404)
                return self._json({"hosts": r.list_hosts()})
            if resource == "activity":
                evs = [_redact_event(e) for e in r.recent_events(
                    limit=_qint(self.path, "limit", 50))]
                return self._json({"events": evs})
            if resource == "analytics":
                kind = parts[4]
                if kind == "missions":
                    return self._json(r.mission_analytics())
                if kind == "models":
                    return self._json(r.model_analytics())
                if kind == "hosts":
                    return self._json(r.host_analytics())
                if kind == "tokens":
                    return self._json(r.token_analytics(
                        project=_q(self.path, "project"), since=_q(self.path, "since")))
                return self._json({"error": "unknown analytics kind"}, 404)
            if resource == "events":
                if len(parts) > 4 and parts[4] == "stream":
                    return self._sse()
                if len(parts) == 4:
                    # per-entity stream: /api/v1/events?entity_type=mission&entity_id=m1
                    etype = _q(self.path, "entity_type")
                    eid = _q(self.path, "entity_id")
                    if etype and etype.startswith("collab"):
                        return self._json({"error": "not found"}, 404)  # HCN has its own authenticated API
                    if not etype or not eid:
                        return self._json(
                            {"error": "entity_type and entity_id required"}, 400)
                    return self._json({"events": r.stream(etype, eid)})
            if resource == "artifacts" and len(parts) > 5 and parts[5] == "download":
                data = r.get_artifact_data(parts[4])
                a = r.get_artifact(parts[4])
                # payload lives in the object store, not the in-memory blob
                if data is None and a is not None and server.rustfs is not None \
                        and a.get("storage_key") and a.get("sha256"):
                    try:
                        data = server.rustfs.get(a["storage_key"], a["sha256"])
                    except Exception:
                        data = None
                if data is None or a is None:
                    return self._json({"error": "not found"}, 404)
                return self._bytes(data, a.get("mime_type", "application/octet-stream"))
            if resource == "leases":
                return self._json({"leases": r.list_leases()})
            if resource == "llm":
                what = parts[4] if len(parts) > 4 else ""
                if what == "config":
                    return self._json(r.llm_config())
                if what == "keys":
                    return self._json({"keys": r.list_project_keys()})
                if what == "models":
                    return self._json({"models": list(r.list_custom_models().values())})
                return self._json({"error": "not found"}, 404)
            if resource == "ingest":
                return self._json({"endpoint": "POST /api/v1/ingest",
                                   "entities": ["host", "session", "mission"]})
            if resource == "github":
                what = parts[4] if len(parts) == 5 else ""
                if len(parts) > 5 or (len(parts) == 5 and what not in {"issues", "issue"}):
                    return self._json({"error": "not found"}, 404)
                query = _github_query(self.path, what)
                if what == "issues":
                    repository = query.pop("repository", None)
                    return self._json(server.github_monitor.issues(repository_id=repository, **query))
                if what == "issue":
                    return self._json(server.github_monitor.issue_detail(
                        query["repository"], query["number"]))
                return self._json(server.github_monitor.repositories(**query))
            if resource == "ci":
                what = parts[4] if len(parts) > 4 else ""
                if what == "pipeline":
                    return self._json(server.ci_monitor.detail(
                        _q(self.path, "repository"), _q(self.path, "number")))
                if what == "log":
                    return self._json(server.ci_monitor.log(
                        _q(self.path, "repository"), _q(self.path, "number"), _q(self.path, "step")))
                if what:
                    return self._json({"error": "not found"}, 404)
                from .project_links import annotate_ci
                return self._json(annotate_ci(server.ci_monitor.snapshot(), r))
            if resource == "reviews":
                from .reviews import ReviewService
                reviews = ReviewService(r)
                if len(parts) > 4:
                    record = reviews.get(parts[4])
                    return self._json(record) if record else self._json({"error": "not found"}, 404)
                rows = reviews.list_reviews()
                offset = max(0, _qint(self.path, "offset", 0))
                limit = min(500, max(1, _qint(self.path, "limit", 500)))
                summaries = [{key: value for key, value in row.items()
                              if key not in {"plan", "feedback", "receipt", "instance_id"}}
                             for row in rows[offset:offset + limit]]
                return self._json({"available": True, "reviews": summaries,
                                   "total": len(rows), "has_more": offset + limit < len(rows),
                                   "offset": offset, "checked_at": r._stamp()})
            if resource == "tests":
                return self._json(_tests_view())
            return self._json({"error": "not found"}, 404)

        def _api_post(self, parts, resource):
            r = server.repo
            if resource == "inspector" and len(parts) == 5 and parts[4] == "preferences":
                return self._inspector_set_prefs(None)
            if (resource == "sessions" and len(parts) == 7 and parts[5] == "inspector"
                    and parts[6] == "preferences"):
                if r.get_session(parts[4]) is None and not r.inspector.has_session(parts[4]):
                    return self._json({"error": "session not found"}, 404)
                return self._inspector_set_prefs(parts[4])
            # artifact upload carries raw bytes, not JSON — handle first
            if resource == "artifacts" and len(parts) == 4:
                return self._upload_artifact(r, parts)
            body = self._read_json() or {}
            if resource == "ci" and len(parts) == 5 and parts[4] == "debug":
                from ..errors import HerdrEngineeringError
                from .ci_debug import debug_ci
                try:
                    receipt = debug_ci(server.ci_monitor, r, server.dispatcher,
                                       body.get("repository"), body.get("number"),
                                       body.get("session_id"), body.get("operation_id"))
                except HerdrEngineeringError as exc:
                    return self._json({"error": exc.message}, exc.http_status)
                status = {"delivered": 200, "queued": 202}.get(receipt.get("status"), 502)
                if receipt.get("error") in {"receipt_persistence_unavailable", "receipt_persistence_failed"}:
                    status = 503
                return self._json(receipt, status)
            if resource == "ci" and len(parts) == 5 and parts[4] == "actions":
                try:
                    return self._json(server.ci_monitor.action(
                        body.get("repository"), body.get("number"), body.get("action")))
                except PermissionError as exc:
                    return self._json({"error": str(exc)}, 403)
                except Exception as exc:
                    from ..errors import HerdrEngineeringError
                    if isinstance(exc, HerdrEngineeringError):
                        return self._json({"error": exc.message}, exc.http_status)
                    raise
            if resource == "reviews":
                from .reviews import ReviewService
                reviews = ReviewService(r)
                if len(parts) == 4:
                    return self._json(reviews.register(body), 201)
                if len(parts) == 6:
                    operation = {"heartbeat": reviews.heartbeat, "result": reviews.complete,
                                 "failure": reviews.fail}.get(parts[5])
                    if operation:
                        return self._json(operation(parts[4], body))
                return self._json({"error": "not found"}, 404)
            if resource == "hosts" and len(parts) == 6 and parts[5] == "telemetry":
                if not body:
                    return self._json({"error": "empty body"}, 400)
                r.record_telemetry(parts[4], body)
                return self._json({"ok": True})
            if resource == "sessions" and len(parts) == 6 and parts[5] == "messages":
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "session not found"}, 404)
                receipt = r.send_message(
                    parts[4], body.get("message", ""), body.get("artifact_ids"),
                    operation_id=body.get("operation_id"), dispatcher=server.dispatcher)
                status = {"delivered": 200, "queued": 202}.get(receipt["status"], 502)
                if receipt.get("error") in {"receipt_persistence_unavailable", "receipt_persistence_failed"}:
                    status = 503
                return self._json({"ok": receipt["status"] == "delivered", **receipt}, status)
            if resource == "sessions" and len(parts) == 6 and parts[5] == "actions":
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "session not found"}, 404)
                receipt = r.session_action(parts[4], body.get("action"),
                                           operation_id=body.get("operation_id"))
                return self._json({"ok": False, **receipt}, 501)
            if resource == "services" and len(parts) == 5 and parts[4] == "retire":
                try:
                    days = float(body.get("older_than_days", 7))
                except (TypeError, ValueError):
                    return self._json({"error": "older_than_days must be a number"}, 400)
                states = body.get("states", ["unknown"])
                if not (days >= 1 and isinstance(states, list)
                        and set(states) <= {"unknown", "unavailable"}):
                    return self._json({"error": "older_than_days >= 1 and states within unknown/unavailable"}, 400)
                # Dry run unless the caller explicitly says dry_run=false.
                return self._json(r.retire_services(
                    older_than_days=days, states=tuple(states), dry_run=body.get("dry_run", True) is not False))
            if resource == "token-usage" and len(parts) == 4:
                rows = body.get("records") if isinstance(body.get("records"), list) else [body]
                for row in rows[:500]:
                    r.record_token_usage(
                        row.get("project"), row.get("model"), row.get("input_tokens"),
                        row.get("output_tokens"), reasoning_tokens=row.get("reasoning_tokens"),
                        cache_read_tokens=row.get("cache_read_tokens"),
                        duration_ms=row.get("duration_ms"), success=row.get("success", True),
                        request_id=row.get("request_id"), session_id=row.get("session_id"),
                        mission_id=row.get("mission_id"), key_id=row.get("key_id"))
                return self._json({"ok": True, "recorded": min(len(rows), 500)})
            if resource == "llm" and len(parts) >= 5:
                what = parts[4]
                if what == "keys" and len(parts) == 5:
                    return self._json(r.create_project_key(body.get("project"),
                                                           body.get("label", "")))
                if what == "keys" and len(parts) == 7 and parts[6] == "revoke":
                    r.revoke_project_key(parts[5])
                    return self._json({"ok": True})
                if what == "models" and len(parts) == 5:
                    r.upsert_custom_model(body.get("name"), body.get("base_url"),
                                          body.get("upstream_model"), body.get("project"))
                    return self._json({"ok": True})
                if what == "models" and len(parts) == 7 and parts[6] == "delete":
                    r.delete_custom_model(parts[5])
                    return self._json({"ok": True})
            if resource == "ingest" and len(parts) == 4:
                if not body:
                    return self._json({"error": "empty body"}, 400)
                events = body.get("events") if isinstance(body, dict) else None
                if events is not None:
                    if not isinstance(events, list) or not events:
                        return self._json({"error": "events must be a non-empty list"}, 400)
                    if len(events) > 64:
                        return self._json({"error": "events must contain at most 64 envelopes"}, 400)
                    entities = []
                    for event in events:
                        if (isinstance(event, dict)
                                and event.get("entity_type") in ("inspector", "mission_activity")):
                            entities.append(server.ingest.ingest(event))
                        else:
                            entities.append(server.ingest.ingest(redact_payload(event)))
                    return self._json({"ok": True, "entities": entities})
                # Inspector envelopes are redacted per field by the store: the
                # blanket key-hint redaction would mangle ``input_tokens`` and
                # ``passed``.
                if isinstance(body, dict) and body.get("entity_type") in ("inspector", "mission_activity"):
                    entity = server.ingest.ingest(body)
                else:
                    entity = server.ingest.ingest(redact_payload(body))
                return self._json({"ok": True, "entity": entity})
            return self._json({"error": "not found"}, 404)

        # ---------------------------------------------------- agent inspector
        def _inspector_global_get(self, parts):
            store = server.repo.inspector
            what = parts[4] if len(parts) > 4 else ""
            if what == "health":
                return self._json(store.health())
            if what == "preferences":
                return self._json({"prefs": store.get_prefs(), "defaults": _defaults()})
            return self._json({"error": "not found"}, 404)

        def _inspector_get(self, parts):
            store = server.repo.inspector
            sid = parts[4]
            role = self._role() or "viewer"
            tail = parts[6:] if len(parts) > 6 else []
            try:
                if not tail:
                    if not store.has_session(sid):
                        if server.repo.get_session(sid) is None:
                            return self._json({"error": "session not found"}, 404)
                        return self._json(_empty_inspector(store, sid))
                    return self._json(store.query(
                        sid, role, view=_q(self.path, "view") or "all",
                        q=_q(self.path, "q"), agent_id=_q(self.path, "agent_id"),
                        status=_q(self.path, "status"),
                        since_rev=_qint(self.path, "since_rev", 0),
                        limit=_qint(self.path, "limit", 200),
                        before_seq=(_qint(self.path, "before_seq", -1)
                                    if _q(self.path, "before_seq") else None)))
                if tail[0] == "events" and len(tail) == 2:
                    if not tail[1].isdigit():
                        return self._json({"error": "invalid sequence"}, 400)
                    return self._json(store.get_event(sid, int(tail[1]), role))
                if tail[0] == "preferences":
                    return self._json({"prefs": store.get_prefs(sid),
                                       "session": store.raw_prefs(sid),
                                       "global": store.raw_prefs(),
                                       "defaults": _defaults()})
                if tail[0] == "stream":
                    if not store.has_session(sid) and server.repo.get_session(sid) is None:
                        return self._json({"error": "session not found"}, 404)
                    if not store.can_subscribe(sid):
                        return self._json({"error": "too many streams"}, 429)
                    return self._inspector_sse(sid, role)
            except KeyError:
                return self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

        def _inspector_set_prefs(self, sid):
            body = self._read_json() or {}
            try:
                prefs = server.repo.inspector.set_prefs(body, sid)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True, "prefs": prefs})

        def _inspector_sse(self, sid, role):
            store = server.repo.inspector
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # A stalled client must not pin a handler thread forever.
            try:
                self.connection.settimeout(30)
            except OSError:
                pass
            sub = store.subscribe(sid)
            try:
                self.wfile.write(_raw_frame({"type": "connected", "session_id": sid,
                                              "epoch": store.epoch}))
                self.wfile.flush()
                last_beat = started = time.monotonic()
                while time.monotonic() - started < _MAX_SSE_SECONDS:
                    item = sub.get(timeout=15)
                    if item is None:
                        if time.monotonic() - last_beat >= 15:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            last_beat = time.monotonic()
                        if sub.closed:
                            break
                        continue
                    if item["type"] == "event":
                        entry = store.stream_entry(sid, item["entry"], role)
                        if entry is None:
                            continue
                        item = {"type": "event", "event": entry}
                    self.wfile.write(_raw_frame(item))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
            finally:
                store.unsubscribe(sid, sub)
            return None

        def _upload_artifact(self, r, parts):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._json({"error": "empty body"}, 400)
            data = self.rfile.read(length)
            filename = _q(self.path, "filename") or "upload"
            mime = _q(self.path, "mime") or \
                self.headers.get("Content-Type", "application/octet-stream")
            session_id = _q(self.path, "session_id")
            mission_id = _q(self.path, "mission_id")
            # When a durable object store is wired, write the payload there
            # (content-addressed by sha256) and keep only metadata in the repo.
            # Otherwise fall back to the repo's in-memory blob stand-in.
            storage_key = ""
            sha256 = ""
            if server.rustfs is not None:
                storage_key, sha256 = server.rustfs.put(data)
            blob = None if server.rustfs is not None else data
            aid = r.register_artifact(session_id=session_id,
                                      mission_id=mission_id,
                                      original_filename=filename,
                                      mime_type=mime, size_bytes=len(data),
                                      sha256=sha256, storage_key=storage_key,
                                      data=blob)
            return self._json(r.get_artifact(aid))

        # ------------------------------------------------------------ SSE
        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue(maxsize=256)
            with server._lock:
                server._subscribers.add(q)
            try:
                # always emit an initial frame so a client gets data immediately
                self.wfile.write(_sse_frame({
                    "event_type": "StreamConnected",
                    "entity_type": "stream", "entity_id": "control-plane",
                    "sequence": 0, "payload": {},
                }))
                self.wfile.flush()
                # replay recent history so a fresh client catches up (Spec §52)
                for ev in reversed(server.repo.recent_events(limit=100)):
                    self.wfile.write(_sse_frame(ev))
                    self.wfile.flush()
                while True:
                    ev = q.get(timeout=15)
                    self.wfile.write(_sse_frame(ev))
                    self.wfile.flush()
            except (queue.Empty, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                server._subscribers.discard(q)
            return None

        # ------------------------------------------------------------ static
        def _serve_static(self):
            rel = self.path.lstrip("/") or _INDEX
            if ".." in rel:
                return self._send_json(403, {"error": "forbidden"})
            target = (server.web_root / rel).resolve()
            if not target.is_relative_to(server.web_root.resolve()) or \
                    not target.exists() or target.is_dir():
                return self._send_json(404, {"error": "not found"})
            ctype = {
                ".html": "text/html", ".css": "text/css",
                ".js": "text/javascript", ".svg": "image/svg+xml",
            }.get(target.suffix, "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None

        # ---------------------------------------------------------- helpers
        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return None
            if length > _MAX_JSON_BODY:
                raise ValueError("request body too large")
            try:
                return json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                raise ValueError("invalid JSON body")

        def _json(self, data, status=200):
            payload = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None

        def _bytes(self, data, mime):
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return None

        def _send_json(self, status, data):
            return self._json(data, status)

    return _Handler


def _defaults() -> dict:
    from .inspector import DEFAULT_LIMITS, DEFAULT_PREFS, HARD_CEILINGS
    return {"prefs": DEFAULT_PREFS, "limits": DEFAULT_LIMITS, "ceilings": HARD_CEILINGS}


def _empty_inspector(store, sid: str) -> dict:
    """A known session that has not (yet) sent inspector telemetry: say so, never imply data."""
    return {"session_id": sid, "view": "all", "epoch": store.epoch, "events": [], "has_more": False, "rev": 0,
            "dropped": 0, "oldest_available_seq": None, "telemetry": "none",
            "summary": None, "capabilities": {"declared": False, "reasoning": "unknown"},
            "prefs": store.get_prefs(sid)}


def _redact_event(event: dict) -> dict:
    ev = dict(event)
    ev["payload"] = redact_payload(ev.get("payload", {}))
    return ev


def _raw_frame(obj: dict) -> bytes:
    """SSE frame for already-sanitised inspector frames (no event-envelope redaction)."""
    return f"data: {json.dumps(obj)}\n\n".encode()


def _sse_frame(event: dict) -> bytes:
    return f"data: {json.dumps(_redact_event(event))}\n\n".encode()


def _ci_view() -> dict[str, Any]:
    """Legacy synchronous CLI view; HTTP clients use the server's shared monitor."""
    from .ci_monitor import CIMonitor
    monitor = CIMonitor()
    monitor.refresh()
    return monitor.snapshot(start=False)


def _tests_view() -> dict[str, Any]:
    """Test-run view: adapters + a coarse per-adapter summary. Fail-closed."""
    from ..tests import TestExplorer
    try:
        explorer = TestExplorer()
        return {"adapters": explorer.adapters()}
    except Exception as exc:
        return {"adapters": [], "error": str(exc)}


_FEED_SERVICE_FIELDS = ("service_id", "service_name", "host_id", "source_port", "session_id",
                        "discovery_source", "verified_origin", "ownership", "listener_identity")


def _q(path, key):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(path).query).get(key, [None])[0]


def _qint(path, key, default):
    v = _q(path, key)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def _github_query(path, kind):
    """Bound local dashboard filters; never forward arbitrary upstream paths."""
    values = parse_qs(urlsplit(path).query, keep_blank_values=True, max_num_fields=20)
    allowed = {"repository", "number"} if kind == "issue" else {"page", "per_page", "search"}
    if kind == "issues":
        allowed |= {"repository", "tab"}
    if values.keys() - allowed or any(len(items) != 1 for items in values.values()):
        raise ValueError("invalid GitHub query")
    query = {key: items[0] for key, items in values.items()}
    repository = query.get("repository")
    if repository is not None:
        name = repository.removeprefix("github.com/")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", name)
                or name.split("/")[1] in {".", ".."}):
            raise ValueError("invalid GitHub repository")
        query["repository"] = "github.com/" + name.lower()
    integer_fields = {"number": (None, 2147483647)} if kind == "issue" else {
        "page": (1, 1000000), "per_page": (50, 100)}
    for key, (default, maximum) in integer_fields.items():
        value = query.get(key, default)
        if value is None or not re.fullmatch(r"[0-9]{1,10}", str(value)):
            raise ValueError("invalid GitHub pagination or issue number")
        number = int(value)
        if not 1 <= number <= maximum:
            raise ValueError("invalid GitHub pagination or issue number")
        query[key] = number
    if kind == "issue":
        if not repository:
            raise ValueError("GitHub repository required")
        return query
    query.setdefault("search", "")
    if len(query["search"]) > 300 or any(ord(char) < 32 or ord(char) == 127 for char in query["search"]):
        raise ValueError("invalid GitHub search")
    if kind == "issues":
        query.setdefault("tab", "all")
        if query["tab"] not in {"all", "autospec", "preparation", "in_progress", "blocked"}:
            raise ValueError("invalid GitHub issue tab")
    return query
