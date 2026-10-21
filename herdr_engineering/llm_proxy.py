"""Fabric LLM gateway: per-project keys, token accounting, custom models.

An OpenAI-compatible forwarder for the dev fabric. Agents call it with a *project
key* (minted by the control plane); the proxy forwards to the lab gateway with ONE
dedicated upstream key (never exposed to agents) or to a registered custom model's
own endpoint, and reports the token usage of every request to the control plane,
attributed to the project. Stopgap until the Rust gateway has the ``upstream`` link
role (spec 63); it speaks the public HTTPS API only.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

log = logging.getLogger("herdr.llm_proxy")

FORWARDED_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/models")
MISS_RELOAD_SECONDS = 2.0
MAX_BODY = 16 * 1024 * 1024
# Gateway admission buckets requests by user+client+session+agent and rejects metadata
# over 128 bytes or outside this set with a 400, so only forward values it accepts.
_META_OK = re.compile(r"^[A-Za-z0-9\-_.:/]{1,128}$")
_IDENTITY_HEADERS = {"x-inferweave-client-id", "x-inferweave-session-id", "x-inferweave-agent-id",
                     "x-session-id"}
DEFAULT_MAX_INFLIGHT = 32  # per project; the gateway's whole admission ceiling is 128
DEFAULT_QUEUE_WAIT_SECONDS = 10.0


def _meta(value: str | None) -> str | None:
    return value if value is not None and _META_OK.match(value) else None


HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
               "proxy-authorization", "proxy-authenticate", "content-length", "host", "accept-encoding",
               "authorization"}


class ControlPlaneClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read() or b"{}")

    def config(self) -> dict:
        return self._call("/api/v1/llm/config")

    def report(self, record: dict) -> None:
        self._call("/api/v1/token-usage", record)


class ProxyState:
    """Key and custom-model tables, refreshed from the control plane."""

    def __init__(self, control: ControlPlaneClient, upstream: str, upstream_key: str,
                 refresh_seconds: float = 10.0, max_inflight: int = DEFAULT_MAX_INFLIGHT,
                 wait_seconds: float = DEFAULT_QUEUE_WAIT_SECONDS):
        self.control, self.upstream, self.upstream_key = control, upstream.rstrip("/"), upstream_key
        self.refresh_seconds = refresh_seconds
        self._keys: dict[str, dict] = {}
        self._models: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._slots: dict[str, threading.BoundedSemaphore] = {}
        self._inflight: dict[str, int] = {}
        self.configure_limits(max_inflight=max_inflight, wait_seconds=wait_seconds)
        self._last = 0.0

    def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last < self.refresh_seconds:
            return
        try:
            cfg = self.control.config()
        except Exception as exc:  # keep serving the last known table
            log.warning("control plane config unavailable: %s", exc)
            self._last = time.monotonic()
            return
        with self._lock:
            self._keys = {k["key_hash"]: k for k in cfg.get("keys", [])}
            self._models = {m["name"]: m for m in cfg.get("custom_models", [])}
            self._last = time.monotonic()

    def authenticate(self, presented: str) -> dict | None:
        self.refresh()
        digest = hashlib.sha256(presented.encode()).hexdigest()
        for attempt in (0, 1):
            with self._lock:
                for h, k in self._keys.items():
                    if hmac.compare_digest(h, digest):
                        return k
            # A key minted after the last refresh must work at once; unknown keys
            # may force a reload, but at most once per MISS_RELOAD_SECONDS.
            if attempt == 0:
                if time.monotonic() - self._last < MISS_RELOAD_SECONDS:
                    return None
                self.refresh(force=True)
        return None

    def configure_limits(self, max_inflight: int, wait_seconds: float) -> None:
        """Per-project concurrent request cap (0 disables) and how long a request may wait for a slot."""
        with self._lock:
            self.max_inflight, self.wait_seconds = max(0, max_inflight), max(0.0, wait_seconds)
            self._slots, self._inflight = {}, {}

    def acquire(self, project: str) -> bool:
        """Take a slot for ``project``; False when none frees up within the wait. Always pair with release()."""
        if not self.max_inflight:
            return True
        with self._lock:
            sem = self._slots.setdefault(project, threading.BoundedSemaphore(self.max_inflight))
        if not sem.acquire(timeout=self.wait_seconds):
            return False
        with self._lock:
            self._inflight[project] = self._inflight.get(project, 0) + 1
        return True

    def release(self, project: str) -> None:
        if not self.max_inflight:
            return
        with self._lock:
            sem = self._slots.get(project)
            self._inflight[project] = max(0, self._inflight.get(project, 0) - 1)
        if sem is not None:
            sem.release()

    def inflight(self, project: str) -> int:
        with self._lock:
            return self._inflight.get(project, 0)

    def route(self, model: str | None) -> tuple[str, str | None, str | None]:
        """(base_url, bearer, upstream model name) for ``model``."""
        with self._lock:
            custom = self._models.get(model or "")
        if custom:
            return custom["base_url"], None, custom.get("upstream_model") or model
        return self.upstream, self.upstream_key, model


def _usage_of(obj: dict) -> dict:
    u = obj.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    cache = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {"input_tokens": u.get("prompt_tokens", 0) or 0,
            "output_tokens": u.get("completion_tokens", 0) or 0,
            "reasoning_tokens": details.get("reasoning_tokens", 0) or 0,
            "cache_read_tokens": cache or 0}


def make_handler(state: ProxyState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        state = None  # set below; lets tests and operators reach the limits

        def log_message(self, *args):  # no request lines with keys/paths in logs
            pass

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            h = self.headers.get("Authorization", "")
            if not h.lower().startswith("bearer "):
                return None
            return state.authenticate(h[7:].strip())

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                return self._json(200, {"ok": True})
            if self.path.split("?")[0] != "/v1/models":
                return self._json(404, {"error": "not found"})
            key = self._auth()
            if key is None:
                return self._json(401, {"error": "invalid or missing project key"})
            return self._forward(key, b"", None)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path not in FORWARDED_PATHS:
                return self._json(404, {"error": "not found"})
            key = self._auth()
            if key is None:
                return self._json(401, {"error": "invalid or missing project key"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json(400, {"error": "bad length"})
            if length > MAX_BODY:
                return self._json(413, {"error": "request too large"})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid json"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "json object required"})
            return self._forward(key, raw, body)

        def _identity_headers(self, key: dict) -> dict:
            """Who the gateway should admit this request as.

            Every project shares ONE upstream key, so without these all projects and
            sessions land in the gateway's single "default" session/agent bucket
            (4 active / 12 queued) and one stalled model starves the rest. The client
            is always the project (never taken from the caller); session and agent
            pass through when well-formed, else the session falls back to the key.
            """
            project = _meta(f"herdr:{key['project']}") or "herdr"
            session = (_meta(self.headers.get("X-InferWeave-Session-ID")) or _meta(self.headers.get("X-Session-Id"))
                       or _meta(f"{key['project']}:{key.get('key_id', '')}") or "herdr")
            out = {"X-InferWeave-Client-ID": project, "X-InferWeave-Session-ID": session}
            agent = _meta(self.headers.get("X-InferWeave-Agent-ID"))
            if agent:
                out["X-InferWeave-Agent-ID"] = agent
            return out

        def _forward(self, key: dict, raw: bytes, body: dict | None):
            project = key["project"]
            if not state.acquire(project):
                wait = max(1, int(state.wait_seconds))
                payload = json.dumps({"error": {
                    "type": "proxy_backpressure", "code": "project_inflight_cap",
                    "message": f"project {project} has {state.max_inflight} requests in flight; retry shortly",
                    "retry_after_ms": wait * 1000}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", str(wait))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                return self._forward_slotted(key, raw, body)
            finally:
                state.release(project)

        def _forward_slotted(self, key: dict, raw: bytes, body: dict | None):
            started = time.monotonic()
            model = body.get("model") if body else None
            base, bearer, upstream_model = state.route(model)
            stream = bool(body and body.get("stream"))
            if body is not None:
                if upstream_model and upstream_model != model:
                    body["model"] = upstream_model
                if stream:  # make the upstream report usage on the final chunk
                    opts = dict(body.get("stream_options") or {})
                    opts["include_usage"] = True
                    body["stream_options"] = opts
                raw = json.dumps(body).encode()
            parts = urlsplit(base + self.path)
            conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP_HEADERS and k.lower() not in _IDENTITY_HEADERS}
            headers.update(self._identity_headers(key))
            headers["Content-Length"] = str(len(raw))
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            usage, status, reported = {}, 502, False
            try:
                conn = conn_cls(parts.hostname, parts.port, timeout=600)
                conn.request(self.command, parts.path + (f"?{parts.query}" if parts.query else ""),
                             body=raw or None, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                self.send_response(status)
                ctype = resp.getheader("Content-Type", "application/json")
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                tail = b""
                collected = bytearray()
                while True:
                    chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    if stream:
                        tail = (tail + chunk)[-65536:]
                    elif len(collected) < MAX_BODY:
                        collected.extend(chunk)
                usage = self._extract(stream, bytes(tail), bytes(collected))
                # Record before closing the response so a client that reads the
                # end of the stream always sees its usage counted.
                if self.command == "POST" and body is not None:
                    self._report(key, model, usage, status, started)
                    reported = True
                self.wfile.write(b"0\r\n\r\n")
            except Exception as exc:
                log.warning("forward failed: %s", exc)
                if status == 502:
                    try:
                        self._json(502, {"error": "upstream unavailable"})
                    except Exception:
                        pass
            finally:
                if self.command == "POST" and body is not None and not reported:
                    self._report(key, model, usage, status, started)

        @staticmethod
        def _extract(stream: bool, tail: bytes, whole: bytes) -> dict:
            try:
                if stream:
                    for line in reversed(tail.decode("utf-8", "replace").splitlines()):
                        if line.startswith("data:") and "usage" in line and "[DONE]" not in line:
                            return _usage_of(json.loads(line[5:].strip()))
                    return {}
                return _usage_of(json.loads(whole or b"{}"))
            except Exception:
                return {}

        def _report(self, key: dict, model, usage: dict, status: int, started: float):
            record = {"project": key["project"], "key_id": key["key_id"], "model": model or "unknown",
                      "success": 200 <= status < 300,
                      "duration_ms": int((time.monotonic() - started) * 1000), **usage}
            try:
                state.control.report(record)
            except Exception as exc:  # accounting must never fail the request
                log.warning("token report failed: %s", exc)

    Handler.state = state
    return Handler


def serve(host: str, port: int, control_url: str, upstream: str, upstream_key: str, *,
          max_inflight: int = DEFAULT_MAX_INFLIGHT, wait_seconds: float = DEFAULT_QUEUE_WAIT_SECONDS):
    state = ProxyState(ControlPlaneClient(control_url), upstream, upstream_key,
                       max_inflight=max_inflight, wait_seconds=wait_seconds)
    state.refresh(force=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    return httpd
