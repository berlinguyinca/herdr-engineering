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
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..version import __version__
from .redact import redact_payload

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
            self._hits[key] = hits
            return True


class ControlPlaneServer:
    """Structured control-plane API + SSE + static web server."""

    def __init__(self, repo, *, api_key=None, api_keys=None, host="127.0.0.1",
                 port=0, web_root=None, source=None, allow_open=False,
                 rustfs=None):
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
                f"non-loopback address {host!r}; configure --api-keys / "
                "HERDR_CP_API_KEYS, or pass --allow-open to trust the "
                "Tailnet boundary (Spec §104)")
        self.host = host
        self.port = port
        self.web_root = Path(web_root) if web_root else _WEB_ROOT
        self._subscribers = set()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._httpd = None
        # live stream (Spec §52)
        repo.subscribe(self._broadcast)

    def _health(self):
        """Repo stats + deployment context (source backend, image version/commit)."""
        h = self.repo.health()
        h["source"] = "postgres" if self.source is not None else "memory"
        h["rustfs"] = "s3" if self.rustfs is not None else "memory"
        h["version"] = __version__
        h["commit"] = _COMMIT
        return h

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
        self._httpd = _DaemonThreadingServer(
            (self.host, self.port), _make_handler(self))
        self.port = self._httpd.server_address[1]
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
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


def _make_handler(server):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "HerdrControlPlane/0.1"
        protocol_version = "HTTP/1.1"

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

        def _require(self, min_role):
            """Return an error response (or None) for the current request.

            Role model (Spec §104): viewer = read-only; operator = read +
            mutate; ingest = read + POST /api/v1/ingest only (read-only
            everywhere else). Unknown roles are treated as viewer (fail-closed
            for mutations).
            """
            role = self._role()
            if role is None:
                return self._send_json(401, {"error": "unauthorized"})
            if min_role == "operator":
                if role == "operator":
                    return None
                path = self.path.split("?")[0]
                if (role == "ingest" and self.command == "POST"
                        and path == "/api/v1/ingest"):
                    return None
                return self._send_json(403, {"error": "read-only"})
            if min_role == "ingest":
                if role in ("operator", "ingest"):
                    return None
                return self._send_json(403, {"error": "read-only"})
            return None

        # ------------------------------------------------------------ routes
        def do_GET(self):
            err = self._require("viewer")
            if err:
                return err
            if self.path.startswith("/api/v1/"):
                return self._route_api()
            return self._serve_static()

        def do_POST(self):
            # The ingest route is reachable by the ingest role; every other
            # POST requires the operator role (Spec §104).
            path = self.path.split("?")[0]
            min_role = ("ingest" if path == "/api/v1/ingest"
                       else "operator")
            err = self._require(min_role)
            if err:
                return err
            if self.path.startswith("/api/v1/"):
                return self._route_api()
            return self._send_json(404, {"error": "not found"})

        def _rate_limit(self):
            # Skip SSE streams: they are long-lived connections, not bursts.
            if self.path.split("?")[0].rstrip("/").endswith("/events/stream"):
                return None
            if server._limiter.allow(self.client_address[0]):
                return None
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(server._limiter.window))
            payload = json.dumps({"error": "too many requests"}).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return None

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
                return self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # never drop a connection: surface 500
                return self._send_json(500, {"error": str(exc)})

        def _api_get(self, parts, resource):
            r = server.repo
            if resource == "health":
                return self._json(server._health())
            if resource == "missions":
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
                return self._json({"services": r.list_services()})
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
            if resource == "ci":
                return self._json(_ci_view())
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
            if resource == "hosts" and len(parts) == 6 and parts[5] == "telemetry":
                if not body:
                    return self._json({"error": "empty body"}, 400)
                r.record_telemetry(parts[4], body)
                return self._json({"ok": True})
            if resource == "sessions" and len(parts) == 6 and parts[5] == "messages":
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "session not found"}, 404)
                r.send_message(parts[4],
                               redact_payload(body.get("message", "")),
                               body.get("artifact_ids"))
                return self._json({"ok": True})
            if resource == "sessions" and len(parts) == 6 and parts[5] == "actions":
                s = r.get_session(parts[4])
                if s is None:
                    return self._json({"error": "session not found"}, 404)
                r.session_action(parts[4], body.get("action"))
                return self._json({"ok": True})
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
                # Inspector envelopes are redacted per field by the store: the
                # blanket key-hint redaction would mangle ``input_tokens`` and
                # ``passed``.
                if isinstance(body, dict) and body.get("entity_type") == "inspector":
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
    """CI view: available flag + repos + recent pipelines. Fail-closed."""
    from ..ci import WoodpeckerProvider
    try:
        provider = WoodpeckerProvider()
        avail = provider.available
        if not avail:
            return {"available": False, "repos": [], "pipelines": {}}
        repos = provider.repos()
        pipelines = {}
        for repo in repos[:10]:
            try:
                rows = provider.pipelines(repo)
                pipelines[repo] = [v.to_dict() if hasattr(v, "to_dict")
                                   else v for v in rows]
            except Exception:
                pipelines[repo] = []
        return {"available": True, "repos": repos, "pipelines": pipelines}
    except Exception as exc:
        return {"available": False, "repos": [], "pipelines": {},
                "error": str(exc)}


def _tests_view() -> dict[str, Any]:
    """Test-run view: adapters + a coarse per-adapter summary. Fail-closed."""
    from ..tests import TestExplorer
    try:
        explorer = TestExplorer()
        return {"adapters": explorer.adapters()}
    except Exception as exc:
        return {"adapters": [], "error": str(exc)}


def _q(path, key):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(path).query).get(key, [None])[0]


def _qint(path, key, default):
    v = _q(path, key)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default
