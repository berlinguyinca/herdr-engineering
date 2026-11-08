"""Fabric LLM gateway: per-project keys, token accounting, custom models.

An OpenAI-compatible forwarder for the dev fabric. Agents call it with a *project
key* (minted by the control plane); the proxy forwards to the lab gateway with ONE
dedicated upstream key (never exposed to agents) or to a registered custom model's
own endpoint, and reports the token usage of every request to the control plane,
attributed to the project. Stopgap until the Rust gateway has the ``upstream`` link
role (spec 63); it speaks the public HTTPS API only.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import http.client
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

log = logging.getLogger("herdr.llm_proxy")

FORWARDED_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/models")
MISS_RELOAD_SECONDS = 2.0
MAX_BODY = 16 * 1024 * 1024
CATALOG_TTL_SECONDS = 10.0
CATALOG_TIMEOUT_SECONDS = 3.0
MAX_CATALOG_BODY = 1024 * 1024
REGISTERED_MODEL_HEADER = "x-herdr-require-registered-model"
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

    def report_batch(self, records: list[dict]) -> None:
        self._call("/api/v1/token-usage", {"records": records})


def _permanent(exc: BaseException) -> bool:
    """A 4xx answer (bad record, project required) will never succeed on retry; 408/429 are transient."""
    return (isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500
            and exc.code not in (408, 429))


class ReportQueue:
    """Usage records leave the request thread: ``submit`` never blocks, one daemon thread delivers (``attempts`` tries,
    ``retry_delay`` apart), and a full queue drops the newest record with a count rather than stalling a request. A slow
    or briefly unreachable control plane used to cost every request up to the client timeout
    ('token report failed: timed out').

    Retries may resend a record the control plane already processed (a client timeout does not mean the POST failed),
    so every record carries a ``request_id`` and the control plane dedupes on it. Permanent 4xx answers are not
    retried."""

    def __init__(self, control, maxsize: int = 1000, attempts: int = 3, retry_delay: float = 1.0):
        self._control, self._attempts, self._retry_delay = control, attempts, retry_delay
        self._q: queue.Queue = queue.Queue(maxsize)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="llm-proxy-report", daemon=True)
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._dropped_lock:
            return self._dropped

    def _drop(self, why: str) -> None:
        with self._dropped_lock:
            self._dropped += 1
            total = self._dropped
        log.warning("token report dropped: %s (%d dropped so far)", why, total)

    def submit(self, record: dict) -> None:
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self._drop("queue full")

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self) -> None:
        """Stop the delivery thread once the queue drains; never blocks, even when the queue is full."""
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass                      # the thread also exits when it sees _stop on an empty queue

    def _run(self) -> None:
        while True:
            try:
                record = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if record is None:
                self._q.task_done()
                return
            try:
                self._deliver(record)
            finally:
                self._q.task_done()

    def _deliver(self, record: dict) -> None:
        for attempt in range(1, self._attempts + 1):
            try:
                self._control.report(record)
                return
            except Exception as exc:  # accounting must never fail anything
                if _permanent(exc):
                    self._drop(f"rejected by control plane: {exc}")
                    return
                if attempt == self._attempts:
                    self._drop(f"failed after {attempt} attempts: {exc}")
                else:
                    time.sleep(self._retry_delay)


class ProxyState:
    """Key and custom-model tables, refreshed from the control plane."""

    def __init__(self, control: ControlPlaneClient, upstream: str, upstream_key: str,
                 refresh_seconds: float = 10.0, max_inflight: int = DEFAULT_MAX_INFLIGHT,
                 wait_seconds: float = DEFAULT_QUEUE_WAIT_SECONDS):
        self.control, self.upstream, self.upstream_key = control, upstream.rstrip("/"), upstream_key
        self.refresh_seconds = refresh_seconds
        self._keys: dict[str, dict] = {}
        self._models: dict[str, dict] = {}
        self._egress: dict = {}
        self._lock = threading.Lock()
        self._slots: dict[str, threading.BoundedSemaphore] = {}
        self._inflight: dict[str, int] = {}
        self.configure_limits(max_inflight=max_inflight, wait_seconds=wait_seconds)
        self._last = 0.0
        self._catalog_lock = threading.Lock()
        self._catalog_last = float("-inf")
        self._catalog: list[dict] = []
        self.reports: ReportQueue | None = None      # set by serve(); None = report inline (tests)

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
            self._egress = cfg.get("egress") if isinstance(cfg.get("egress"), dict) else {}
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

    def custom_model(self, model: str | None) -> dict | None:
        with self._lock:
            custom = self._models.get(model or "")
            return dict(custom) if custom else None

    def _fabric_catalog(self) -> list[dict]:
        # Catalog I/O never holds the key/routing/admission lock. Concurrent
        # refresh contenders use CPU while the first request refreshes evidence.
        if time.monotonic() - self._catalog_last < CATALOG_TTL_SECONDS:
            return self._catalog
        if not self._catalog_lock.acquire(blocking=False):
            return []
        conn = None
        try:
            if time.monotonic() - self._catalog_last < CATALOG_TTL_SECONDS:
                return self._catalog
            parts = urlsplit(self.upstream + "/v1/models")
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = cls(parts.hostname, parts.port, timeout=CATALOG_TIMEOUT_SECONDS)
            conn.request("GET", parts.path, headers={"Authorization": f"Bearer {self.upstream_key}"})
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError("catalog unavailable")
            raw = response.read(MAX_CATALOG_BODY + 1)
            if len(raw) > MAX_CATALOG_BODY:
                raise ValueError("catalog too large")
            payload = json.loads(raw)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("invalid catalog")
            self._catalog = data
        except Exception:
            # No stale success on failure, redirects, or malformed metadata.
            self._catalog = []
        finally:
            self._catalog_last = time.monotonic()
            if conn:
                conn.close()
            self._catalog_lock.release()
        return self._catalog

    @staticmethod
    def _ready(item: dict, required) -> bool:
        """A catalog entry that can serve a request of ``required`` tokens now: hot/warm, text, ungrouped, with slots."""
        context = item.get("ctx_per_request")
        if context is None:
            context = item.get("x_context_window")
        slots = item.get("slots")
        return not (not isinstance(item.get("id"), str) or not item["id"] or item.get("x_group")
                    or item.get("x_state") not in ("hot", "warm")
                    or item.get("x_modality") not in (None, "", "text", "chat")
                    or type(context) is not int or type(required) is not int or context < required
                    or type(slots) is not int or slots <= 0)

    def _fabric_model(self, custom: dict) -> str | None:
        wanted = custom.get("fabric_model")
        if not isinstance(wanted, str) or not wanted:
            return None
        catalog = self._fabric_catalog()
        matches = [item for item in catalog if item.get("id") == wanted]
        if not matches:
            matches = [item for item in catalog if isinstance(item.get("aliases"), list)
                       and wanted in item["aliases"]]
        if len(matches) != 1:
            return None
        return matches[0]["id"] if self._ready(matches[0], custom.get("context_window", 8192)) else None

    def _fallback_model(self, custom: dict) -> str | None:
        """First ``fallback_models`` pattern with a ready gateway model; among its matches the one with most free slots."""
        patterns = custom.get("fallback_models")
        if not isinstance(patterns, list):
            return None
        catalog = self._fabric_catalog()
        required = custom.get("context_window", 8192)
        for pattern in patterns:
            ready = [item for item in catalog if isinstance(item.get("id"), str)
                     and fnmatch.fnmatchcase(item["id"].lower(), str(pattern).lower()) and self._ready(item, required)]
            if ready:
                return max(ready, key=lambda item: (item["slots"], item["id"]))["id"]
        return None

    def resolve_route(self, custom: dict, model: str | None) -> tuple[str, str | None, str | None, str]:
        """(base_url, bearer, upstream model, kind) for a registered model: the advertised model on the gateway
        ("fabric"), else a gateway model matching ``fallback_models`` ("fallback"), else the private service ("cpu")."""
        concrete = self._fabric_model(custom)
        if concrete:
            return self.upstream, self.upstream_key, concrete, "fabric"
        return self.degraded_route(custom, model)

    def degraded_route(self, custom: dict, model: str | None) -> tuple[str, str | None, str | None, str]:
        """Where a registered model goes when its advertised gateway model is not usable."""
        concrete = self._fallback_model(custom)
        if concrete:
            return self.upstream, self.upstream_key, concrete, "fallback"
        return custom.get("base_url"), None, custom.get("upstream_model") or model, "cpu"

    def custom_route(self, custom: dict, model: str | None) -> tuple[str, str | None, str | None]:
        return self.resolve_route(custom, model)[:3]

    def egress_check(self, model: str | None, data_class: str | None) -> tuple[bool, str]:
        """Classified-content gate. Route kinds come from operator config (``egress.upstream_kind``,
        ``egress.custom_kinds``), never from the request; an unconfigured route is treated as external."""
        from .control_plane.intel_egress import may_route
        with self._lock:
            egress = self._egress
        custom = self.custom_model(model)
        kinds = egress.get("custom_kinds") if isinstance(egress.get("custom_kinds"), dict) else {}
        approved = frozenset(x for x in (egress.get("approved") or []) if isinstance(x, str))

        def norm(kind):
            return kind if kind in ("local", "approved_cloud", "external") else "external"
        # A custom model may be served by its own endpoint OR resolved onto the shared upstream (fabric model), so the
        # content must be allowed on BOTH routes: marking a custom model local cannot launder data to the upstream.
        routes = [norm(egress.get("upstream_kind"))] if not custom else [norm(kinds.get(custom["name"])), norm(egress.get("upstream_kind"))]
        for kind in routes:
            ok, why = may_route(data_class, kind, approved=approved)
            if not ok:
                return ok, why
        return True, "allowed on every route the model can take"

    def route(self, model: str | None) -> tuple[str, str | None, str | None]:
        """(base_url, bearer, upstream model name) for ``model``."""
        custom = self.custom_model(model)
        if custom:
            return self.custom_route(custom, model)
        return self.upstream, self.upstream_key, model


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens")


class _ClientGone(Exception):
    """A write to the calling client failed (it disconnected)."""


def _count(container, key):
    """A reported token count, or None when the source did not report it (never 0)."""
    value = container.get(key) if isinstance(container, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage_of(obj) -> dict | None:
    """Token counts from an OpenAI ``usage`` object; None when there is none."""
    u = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(u, dict):
        return None
    return {"input_tokens": _count(u, "prompt_tokens"),
            "output_tokens": _count(u, "completion_tokens"),
            "reasoning_tokens": _count(u.get("completion_tokens_details"), "reasoning_tokens"),
            "cache_read_tokens": _count(u.get("prompt_tokens_details"), "cached_tokens")}


def _upstream_error_class(status: int) -> str | None:
    """Error class of an upstream HTTP status (design doc taxonomy). The body is not
    inspected, so a gateway 400 over identity metadata is an upstream_client_error."""
    if 200 <= status < 300:
        return None
    if status in (429, 503):
        return "upstream_admission"
    if 400 <= status < 500:
        return "upstream_client_error"
    return "upstream_server_error"


def _ms(start: float | None, end: float | None) -> int | None:
    return None if start is None or end is None else max(0, int((end - start) * 1000))


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
                # No project to attribute it to, and an anonymous caller must not be
                # able to write records: 401s are never recorded.
                return self._json(401, {"error": "invalid or missing project key"})
            started = time.monotonic()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reject(key, started, 400, {"error": "bad length"}, "request_invalid")
            if length > MAX_BODY:
                self.close_connection = True  # the unread body must not be parsed as a request
                return self._reject(key, started, 413, {"error": "request too large"}, "request_too_large")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._reject(key, started, 400, {"error": "invalid json"}, "request_invalid")
            if not isinstance(body, dict):
                return self._reject(key, started, 400, {"error": "json object required"}, "request_invalid")
            data_class = self.headers.get("X-Herdr-Data-Class")
            if data_class is not None:
                # Tagged content (mail, attachments, scientific data) is gated by class; untagged traffic is unchanged.
                allowed, why = state.egress_check(body.get("model"), data_class.strip().lower())
                if not allowed:
                    return self._reject(key, started, 403, {"error": {"type": "egress_denied", "message": why}},
                                        "egress_denied")
            return self._forward(key, raw, body)

        def _reject(self, key: dict, started: float, status: int, payload: dict, error_class: str):
            try:
                self._json(status, payload)
            finally:
                meta = self._meta(key, None)
                meta.update(status=status, error_class=error_class, queue_ms=0)
                self._report(key, meta, started)

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

        def _meta(self, key: dict, body: dict | None) -> dict:
            """The usage record's request facts; unknown stays None, never 0.

            The session id is the caller's header, else the key label ``session:<id>``
            (``herdr-eng session run`` keys), else None. The gateway-bucket fallback
            ``<project>:<key_id>`` is not a session and is never recorded as one.
            """
            label = key.get("label") if isinstance(key.get("label"), str) else ""
            session = (_meta(self.headers.get("X-InferWeave-Session-ID")) or _meta(self.headers.get("X-Session-Id"))
                       or (_meta(label[len("session:"):]) if label.startswith("session:") else None))
            model = body.get("model") if body else None
            return {"model": model if isinstance(model, str) else None, "upstream_model": None,
                    "session_id": session, "agent_id": _meta(self.headers.get("X-InferWeave-Agent-ID")),
                    "stream": bool(body.get("stream")) if body is not None else None,
                    "route": None, "fallback": False, "status": None, "error_class": None,
                    "queue_ms": None, "ttft_ms": None, "gen_ms": None, "usage": None, "engine": None}

        def _forward(self, key: dict, raw: bytes, body: dict | None):
            project = key["project"]
            started = time.monotonic()
            meta = self._meta(key, body)
            acquired = state.acquire(project)
            meta["queue_ms"] = _ms(started, time.monotonic())
            complete = False
            try:
                if not acquired:
                    wait = max(1, int(state.wait_seconds))
                    meta.update(status=429, error_class="proxy_backpressure")
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
                    complete = self._forward_slotted(key, raw, body, meta)
                finally:
                    state.release(project)
            finally:
                # Record before closing the response so a client that reads the
                # end of the stream always sees its usage counted.
                if self.command == "POST":
                    self._report(key, meta, started)
            if complete:
                # Release admission before the client observes response completion.
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    self.close_connection = True

        def _client_write(self, data: bytes):
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except OSError as exc:
                raise _ClientGone() from exc

        def _forward_slotted(self, key: dict, raw: bytes, body: dict | None, meta: dict):
            model = body.get("model") if body else None
            custom = state.custom_model(model)
            guarded = self.headers.get(REGISTERED_MODEL_HEADER, "").lower() == "true"
            if guarded and custom is None:
                meta.update(status=503, error_class="model_not_registered")
                return self._json(503, {"error": {"code": "model_not_registered",
                                                 "message": "required model is not registered"}})
            if (custom and (guarded or custom.get("fabric_model")) and custom.get("project")
                    and custom["project"] != key["project"]):
                meta.update(status=403, error_class="model_project_mismatch")
                return self._json(403, {"error": {"code": "model_project_mismatch"}})
            base, bearer, upstream_model, route_kind = (state.resolve_route(custom, model) if custom
                                                        else (state.upstream, state.upstream_key, model, "cpu"))
            policy = bool(custom and custom.get("fabric_model"))
            stream = bool(body and body.get("stream"))
            may_fall_back = bool(policy and route_kind == "fabric" and not stream)
            sent_headers = False
            conn = None
            try:
                while True:
                    meta.update(route=route_kind if custom else "upstream", upstream_model=upstream_model)
                    outbound = dict(body) if body is not None else None
                    if outbound is not None:
                        if upstream_model:
                            outbound["model"] = upstream_model
                        if stream:
                            opts = dict(outbound.get("stream_options") or {})
                            opts["include_usage"] = True
                            outbound["stream_options"] = opts
                        raw = json.dumps(outbound).encode()
                    if base is None:        # no gateway fallback and no private service: nothing to send to
                        meta.update(status=503, error_class="model_unavailable")
                        return self._json(503, {"error": {"code": "model_unavailable",
                                                         "message": "no route is available for this model"}})
                    parts = urlsplit(base + self.path)
                    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                                else http.client.HTTPConnection)
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in HOP_HEADERS and k.lower() not in _IDENTITY_HEADERS
                               and k.lower() != REGISTERED_MODEL_HEADER}
                    headers.update(self._identity_headers(key))
                    headers["Content-Length"] = str(len(raw))
                    if bearer:
                        headers["Authorization"] = f"Bearer {bearer}"
                    try:
                        conn = conn_cls(parts.hostname, parts.port, timeout=10 if may_fall_back else 600)
                        sent = time.monotonic()
                        conn.request(self.command, parts.path + (f"?{parts.query}" if parts.query else ""),
                                     body=raw or None, headers=headers)
                        resp = conn.getresponse()
                        if not may_fall_back or resp.status not in {429, 503}:
                            break
                        if state.degraded_route(custom, model)[0] is None:
                            break           # nowhere else to go: the caller gets the gateway's own answer
                    except (OSError, http.client.HTTPException):
                        if not may_fall_back or state.degraded_route(custom, model)[0] is None:
                            raise
                    conn.close()
                    may_fall_back = False
                    meta["fallback"] = True
                    base, bearer, upstream_model, route_kind = state.degraded_route(custom, model)
                meta.update(status=resp.status, error_class=_upstream_error_class(resp.status))
                try:
                    self.send_response(resp.status)
                    if policy:
                        self.send_header("X-Herdr-LLM-Route", route_kind)
                    ctype = resp.getheader("Content-Type", "application/json")
                    self.send_header("Content-Type", ctype)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                except OSError as exc:
                    raise _ClientGone() from exc
                sent_headers = True
                tail = b""
                collected = bytearray()
                first = last = None
                while True:
                    chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                    if not chunk:
                        # read1() can return EOF without enforcing Content-Length.
                        # Never mark a truncated upstream body as a complete reply.
                        if resp.length is not None and resp.length > 0:
                            raise http.client.IncompleteRead(b"", resp.length)
                        break
                    last = time.monotonic()
                    if first is None:
                        first = last
                    self._client_write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    if stream:
                        tail = (tail + chunk)[-65536:]
                    elif len(collected) < MAX_BODY:
                        collected.extend(chunk)
                if stream:  # TTFT and generation time are defined for streams only
                    meta.update(ttft_ms=_ms(sent, first), gen_ms=_ms(first, last))
                # The body stays in memory only long enough to read its usage.
                meta["usage"] = self._extract(stream, bytes(tail), bytes(collected))
                return True
            except _ClientGone:
                meta["error_class"] = "client_disconnected"
                self.close_connection = True
            except Exception as exc:
                log.warning("forward failed: %s", type(exc).__name__)
                if sent_headers:
                    meta["error_class"] = "stream_truncated"
                    self.close_connection = True
                else:
                    meta.update(status=502, error_class="upstream_unreachable")
                    try:
                        self._json(502, {"error": "upstream unavailable"})
                    except Exception:
                        pass
            finally:
                if conn:
                    conn.close()

        @staticmethod
        def _extract(stream: bool, tail: bytes, whole: bytes) -> dict | None:
            try:
                if stream:
                    for line in reversed(tail.decode("utf-8", "replace").splitlines()):
                        if line.startswith("data:") and "usage" in line and "[DONE]" not in line:
                            usage = _usage_of(json.loads(line[5:].strip()))
                            if usage is not None:
                                return usage
                    return None
                return _usage_of(json.loads(whole or b"{}"))
            except Exception:
                return None

        def _report(self, key: dict, meta: dict, started: float):
            """Send one schema-2 usage record: metadata only, never request or response text."""
            usage = meta["usage"]
            status, error_class = meta["status"], meta["error_class"]
            record = {"schema": 2, "source": "llm-proxy", "request_id": uuid.uuid4().hex, "project": key["project"],
                      "key_id": key["key_id"], "model": meta["model"] or "unknown",
                      **{k: meta[k] for k in ("upstream_model", "session_id", "agent_id", "route",
                                              "fallback", "stream", "status", "error_class",
                                              "queue_ms", "ttft_ms", "gen_ms", "engine")},
                      "success": status is not None and 200 <= status < 300 and error_class is None,
                      "duration_ms": _ms(started, time.monotonic()),
                      "usage_known": usage is not None,
                      **{k: (usage or {}).get(k) for k in _TOKEN_FIELDS}}
            try:
                if state.reports is not None:
                    state.reports.submit(record)
                else:
                    state.control.report(record)
            except Exception as exc:  # accounting must never fail the request
                log.warning("token report failed: %s", exc)

    Handler.state = state
    return Handler


def serve(host: str, port: int, control_url: str, upstream: str, upstream_key: str, *,
          max_inflight: int = DEFAULT_MAX_INFLIGHT, wait_seconds: float = DEFAULT_QUEUE_WAIT_SECONDS):
    state = ProxyState(ControlPlaneClient(control_url), upstream, upstream_key,
                       max_inflight=max_inflight, wait_seconds=wait_seconds)
    state.reports = ReportQueue(state.control)
    state.refresh(force=True)
    httpd = _TrackingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    return httpd


class _TrackingHTTPServer(ThreadingHTTPServer):
    """Threading server that remembers its live handler threads (daemon threads are not tracked by the stdlib)."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._handler_threads: set[threading.Thread] = set()
        self._handler_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        thread = threading.Thread(target=self._run_handler, args=(request, client_address), daemon=True)
        with self._handler_lock:
            self._handler_threads.add(thread)
        thread.start()

    def _run_handler(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            with self._handler_lock:
                self._handler_threads.discard(threading.current_thread())


def _drain_handler_threads(httpd, timeout: float = 3.0) -> None:
    """Wait (bounded) for in-flight request handlers so the reports they submit are flushed, not lost."""
    lock = getattr(httpd, "_handler_lock", None)
    if lock is None:
        return
    deadline = time.monotonic() + timeout
    with lock:
        pending = list(httpd._handler_threads)
    for t in pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(remaining)


def run_until_stopped(httpd, flush_seconds: float = 5.0) -> None:
    """Serve until Ctrl-C or SIGTERM, then flush queued usage reports (so a restart does not lose them) and close."""
    import signal

    def _terminate(_signum, _frame):
        # shutdown() waits for serve_forever to return, so it cannot run on the thread that is serving.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _terminate)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _drain_handler_threads(httpd)
        reports = getattr(getattr(getattr(httpd, "RequestHandlerClass", None), "state", None), "reports", None)
        if reports is not None:
            reports.flush(flush_seconds)
            reports.close()
        close = getattr(httpd, "server_close", None)
        if close:
            close()
