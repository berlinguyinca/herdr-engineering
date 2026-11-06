"""``/api/v1/build-cache`` routes (spec 09) and the flag-gated service behind them.

Config (YAML only, never environment variables)::

    control_plane:
      build_cache:
        enabled: false              # default; when off the routes answer 404 and nothing is constructed
        ingest: {enabled: false, max_hosts: 64, per_host_builds: 200}   # other hosts' reports (default off)
        state_dir: /path            # optional; default is the build-gate state dir of THIS host
        fabric_build_cache: {...}   # optional; the host's ``fabric_build_cache`` section (default: the host config)

Scope, stated honestly:

* Read-only data comes from THIS control-plane host's local state files (``builds.jsonl``, host probe). Build hosts do
  not push their records to the control plane (no ingest, no table, no migration), so other hosts are not visible.
* ``POST cleanup/preview`` runs the existing dry-run planner; there is no apply endpoint (apply stays CLI-only).
* Lifecycle changes are published with ``repo.record_event`` (redacted, allowlisted). The shared SSE stream has no
  event ids; ``GET /build-cache/events/stream`` is the resumable one (``id:`` + ``Last-Event-ID``, 500-event ring
  kept in memory, so a control-plane restart replays the ring on reconnect instead of resuming exactly).
* Events are published when a read request notices new ``builds.jsonl`` lines (no background thread). A quiet API
  therefore delays events; the first read after start only sets the baseline and never replays history.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .redact import redact_payload

CAPABILITY = "build_cache_v1"
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
_TAIL_BYTES = 2 * 1024 * 1024
_SYNC_MAX_BYTES = 1024 * 1024
_SYNC_MAX_EVENTS = 200
_HOSTS_TTL_S = 30.0
_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_FILTERS = ("mission", "session", "host", "cmd_kind")
_FIELDS = ("event", "ts", "build_id", "host", "slot", "cmd_kind", "exit_code", "elapsed_s", "user_cpu_s", "sys_cpu_s",
           "max_rss_kb", "rusage_oublock", "proc_io_write_bytes", "host_disk_write_bytes_delta", "host_disk_scope",
           "unavailable")
_EVENT_FIELDS = ("ts", "build_id", "host", "slot", "cmd_kind", "exit_code", "elapsed_s")
_SIMPLE = (str, int, float, bool, type(None))
# Contract events written by the build side to contract-events.jsonl (build_metrics.emit_event); allowlisted fields.
_CONTRACT_FIELDS = {
    "cache_stats_updated": ("ts", "build_id", "host", "cache_applied"),
    "storage_degraded": ("ts", "build_id", "host", "tier", "reason"),
    "lease_changed": ("ts", "host", "change", "scope_id", "attempt_id", "epoch"),
    "cleanup_completed": ("ts", "host", "kind", "status", "removed_count", "removed_bytes"),
}
INGEST_MAX_RECORDS = 100
STREAM_RING = 500            # events a reconnecting client can replay
_STREAM_MAX_S = 3600
_STREAM_TICK_S = 1.0
_LOGS = ("builds.jsonl", "contract-events.jsonl")
_BUILD_EVENTS = ("build_started", "build_finished")


class IngestFull(Exception):
    """The reporting-host bound is reached."""


def build_cache_config(cfg) -> dict:
    cfg = {} if cfg is None else cfg
    if not isinstance(cfg, dict):
        raise ValueError("control_plane.build_cache must be a mapping")
    unknown = set(cfg) - {"enabled", "state_dir", "fabric_build_cache", "ingest"}
    if unknown:
        raise ValueError(f"control_plane.build_cache: unknown keys {sorted(unknown)}")
    if cfg.get("enabled", False) not in (True, False):
        raise ValueError("control_plane.build_cache.enabled must be true or false")
    state_dir = cfg.get("state_dir")
    if state_dir is not None and (not isinstance(state_dir, str) or not os.path.isabs(state_dir)):
        raise ValueError("control_plane.build_cache.state_dir must be an absolute path")
    section = cfg.get("fabric_build_cache")
    if section is not None and not isinstance(section, dict):
        raise ValueError("control_plane.build_cache.fabric_build_cache must be a mapping")
    return {"enabled": cfg.get("enabled", False) is True, "state_dir": state_dir, "fabric_build_cache": section,
            "ingest": _ingest_config(cfg.get("ingest"))}


def _ingest_config(cfg) -> dict:
    cfg = {} if cfg is None else cfg
    if not isinstance(cfg, dict):
        raise ValueError("control_plane.build_cache.ingest must be a mapping")
    unknown = set(cfg) - {"enabled", "max_hosts", "per_host_builds"}
    if unknown:
        raise ValueError(f"control_plane.build_cache.ingest: unknown keys {sorted(unknown)}")
    if cfg.get("enabled", False) not in (True, False):
        raise ValueError("control_plane.build_cache.ingest.enabled must be true or false")
    out = {"enabled": cfg.get("enabled", False) is True}
    for name, default, hi in (("max_hosts", 64, 1024), ("per_host_builds", 200, 5000)):
        value = cfg.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= hi:
            raise ValueError(f"control_plane.build_cache.ingest.{name} must be an integer in 1..{hi}")
        out[name] = value
    return out


def build_build_cache(cfg, *, record_event):
    """Return the service, or None when the flag is off (nothing is constructed or read)."""
    config = build_cache_config(cfg)
    if not config["enabled"]:
        return None
    from ..build_cache_config import validate
    section = config["fabric_build_cache"]
    if section is None:
        from ..config import load_config
        section = load_config().get("fabric_build_cache")
    state = Path(config["state_dir"]) if config["state_dir"] else None
    if state is None:
        from ..build_coord import _default_state_dir
        state = _default_state_dir()
    return BuildCacheService(validate(section), state, record_event=record_event, ingest=config["ingest"])


def _trace(value):
    if not isinstance(value, dict):
        return None
    from ..build_metrics import sanitize_trace_id
    out = {k: v for k, v in value.items() if k in ("session", "mission") and sanitize_trace_id(v)}
    return out or None


def _slim(rec: dict) -> dict:
    out = {k: rec[k] for k in _FIELDS if k in rec and (isinstance(rec[k], _SIMPLE) or k == "unavailable")}
    if isinstance(out.get("unavailable"), list):
        out["unavailable"] = [str(x)[:64] for x in out["unavailable"][:32]]
    cache = rec.get("cache")
    if isinstance(cache, dict):
        out["cache"] = {"applied": cache.get("applied") is True}
        if isinstance(cache.get("reasons"), list):
            out["cache"]["reasons"] = [str(x)[:64] for x in cache["reasons"][:16]]
    stats = rec.get("sccache_stats")
    if isinstance(stats, dict):
        out["sccache_stats"] = {str(k)[:64]: v for k, v in list(stats.items())[:64]
                                if isinstance(v, (int, float)) and not isinstance(v, bool)}
    trace = _trace(rec.get("trace"))
    if trace:
        out["trace"] = trace
    return redact_payload(out)


class BuildCacheService:
    def __init__(self, cfg, state_dir: Path, *, record_event=None, now=time.monotonic, ingest=None):
        self.cfg = cfg
        self.ingest_cfg = ingest or _ingest_config(None)
        # host_id -> build_id -> {event: slim record}; in memory only (see ingest()).
        self._remote: dict[str, dict[str, dict[str, dict]]] = {}
        self._remote_ts: dict[str, str] = {}
        self.state_dir = Path(state_dir)
        self.record_event = record_event
        self._now = now
        self._lock = threading.RLock()
        self._boot = uuid.uuid4().hex[:8]                    # ids from another boot cannot be ordered against ours
        self._seq = 0
        self._ring: deque = deque(maxlen=STREAM_RING)         # (n, frame)
        self._subs: set = set()
        self._cursors: dict[str, list[int | None]] = {}      # log name -> [offset, inode]
        self._hosts: tuple[float, list] | None = None

    @property
    def log_path(self) -> Path:
        return self.state_dir / "builds.jsonl"

    # ---------------------------------------------------------------- log reads
    def _tail_lines(self) -> list[dict]:
        try:
            with open(self.log_path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - _TAIL_BYTES))
                data = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        lines = data.splitlines()
        if size > _TAIL_BYTES:
            lines = lines[1:]                       # first line is probably cut
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("event") in ("build_started", "build_finished") \
                    and isinstance(rec.get("build_id"), str):
                out.append(rec)
        return out

    def _builds(self) -> list[dict]:
        """Merged started/finished per build id, newest activity first (local log plus reported hosts)."""
        rows = self._merge(self._tail_lines())
        if self.ingest_cfg["enabled"] and self._remote:
            with self._lock:
                remote = [r for h in self._remote.values() for slot in h.values() for r in slot.values()]
            rows = sorted(rows + self._merge(remote), key=lambda r: r.get("ts") or "", reverse=True)
        return rows

    @staticmethod
    def _merge(records) -> list[dict]:
        merged: dict[str, dict] = {}
        for rec in records:
            slot = merged.pop(rec["build_id"], {})          # pop + reinsert keeps recency order
            slot["finished" if rec["event"] == "build_finished" else "started"] = rec
            merged[rec["build_id"]] = slot
        out = []
        for bid, slot in reversed(list(merged.items())):
            fin, start = slot.get("finished"), slot.get("started")
            if fin is not None:
                status = "failed" if fin.get("exit_code") else "completed"
            else:
                status = "running_or_unknown"
            row = _slim(fin or start)
            row["build_id"] = bid
            row["status"] = status
            if start is not None and fin is not None:
                row["started"] = _slim(start)
            out.append(row)
        return out

    def list_builds(self, query: dict, limit: int, cursor: int) -> dict:
        rows = self._builds()
        for name in _FILTERS:
            want = query.get(name)
            if want is None:
                continue
            if name in ("mission", "session"):
                rows = [r for r in rows if (r.get("trace") or {}).get(name) == want]
            else:
                rows = [r for r in rows if r.get(name) == want]
        if query.get("status") is not None:
            rows = [r for r in rows if r["status"] == query["status"]]
        page = rows[cursor:cursor + limit]
        more = cursor + limit < len(rows)
        return {"builds": page, "limit": limit, "next_cursor": str(cursor + limit) if more else None,
                "source": "local_builds_jsonl+reported" if self.ingest_cfg["enabled"] else "local_builds_jsonl",
                "window_bytes": _TAIL_BYTES}

    def get_build(self, build_id: str) -> dict | None:
        for row in self._builds():
            if row["build_id"] == build_id:
                return row
        return None

    # ---------------------------------------------------------------- events
    def sync_events(self) -> int:
        """Publish log lines appended since the last call (builds.jsonl and contract-events.jsonl).

        The first call sets the baseline only.
        """
        if self.record_event is None:
            return 0
        with self._lock:
            return sum(self._sync_log(name) for name in _LOGS)

    def _sync_log(self, name: str) -> int:
        path = self.state_dir / name
        cur = self._cursors.setdefault(name, [None, None])
        try:
            st = os.stat(path)
        except OSError:
            if cur[0] is None:
                cur[0] = 0                                        # no log yet: a file created later is read from its start
            return 0
        if cur[0] is None:
            cur[0], cur[1] = st.st_size, st.st_ino
            return 0
        if st.st_ino != cur[1] or st.st_size < cur[0]:
            cur[0], cur[1] = 0, st.st_ino                          # rotated or truncated: read the new file
        if st.st_size == cur[0]:
            return 0
        try:
            with open(path, "rb") as f:
                f.seek(cur[0])
                data = f.read(_SYNC_MAX_BYTES)
        except OSError:
            return 0
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= _SYNC_MAX_BYTES:
                cur[0] += len(data)                                # one absurd line: skip it
            return 0
        published = 0
        consumed = 0
        for raw in data[:end + 1].splitlines(keepends=True):
            if published >= _SYNC_MAX_EVENTS:
                break
            consumed += len(raw)
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("event") not in (*_BUILD_EVENTS, *_CONTRACT_FIELDS):
                continue
            self._publish(rec)
            published += 1
        cur[0] += consumed
        return published

    def _publish(self, rec: dict) -> None:
        kind = rec["event"]
        if kind in _CONTRACT_FIELDS:
            payload = {k: rec[k] for k in _CONTRACT_FIELDS[kind] if k in rec and isinstance(rec[k], _SIMPLE)}
            if kind == "cache_stats_updated" and isinstance(rec.get("sccache_stats"), dict):
                payload["sccache_stats"] = {str(k)[:64]: v for k, v in list(rec["sccache_stats"].items())[:64]
                                            if isinstance(v, (int, float)) and not isinstance(v, bool)}
            trace = None
        else:
            payload = {k: rec[k] for k in _EVENT_FIELDS if k in rec and isinstance(rec[k], _SIMPLE)}
            trace = _trace(rec.get("trace"))
            if trace:
                payload["trace"] = trace
            if isinstance(rec.get("cache"), dict):
                payload["cache_applied"] = rec["cache"].get("applied") is True
        host = str(rec.get("host") or "unknown")[:64]
        try:
            ev = self.record_event("build_cache", host, f"build_cache.{kind}", redact_payload(payload),
                                   session_id=(trace or {}).get("session"))
        except Exception:  # noqa: BLE001 - publishing must never break a read
            return
        self._fan_out(ev, host, f"build_cache.{kind}", redact_payload(payload))

    # ---------------------------------------------------------------- resumable stream
    def _fan_out(self, ev, host: str, event_type: str, payload: dict) -> None:
        """Give the published event a stream id and hand it to the ring and to connected clients.

        The id is ``<boot>-<n>`` with a service-wide counter: the repo's per-host ``sequence`` (carried in the frame)
        is only ordered within one host, so it cannot resume a stream that mixes hosts.
        """
        seq = ev.get("sequence") if isinstance(ev, dict) else None
        with self._lock:
            self._seq += 1
            frame = {"id": f"{self._boot}-{self._seq}", "event_type": event_type, "entity_type": "build_cache",
                     "entity_id": host, "sequence": seq, "payload": payload}
            self._ring.append((self._seq, frame))
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                with self._lock:
                    self._subs.discard(q)                      # slow client: drop it, it reconnects with Last-Event-ID

    def subscribe(self, last_event_id: str | None) -> tuple[queue.Queue, list[dict]]:
        """Register a client; returns its live queue and the frames to replay first.

        No id: no replay. An id from this boot: the retained events after it. Any other id (another boot, garbage,
        or older than the ring): the whole retained ring, so a reconnect never silently loses events (at-least-once;
        clients de-duplicate on the id).
        """
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            replay: list[dict] = []
            if last_event_id:
                m = re.fullmatch(r"([0-9a-f]{8})-([0-9]{1,12})", last_event_id)
                after = int(m.group(2)) if m and m.group(1) == self._boot else -1
                replay = [f for n, f in self._ring if n > after]
            self._subs.add(q)
        return q, replay

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    # ---------------------------------------------------------------- remote hosts
    def ingest(self, host_id: str, body) -> dict:
        """Store another host's ``build-coordinate --record`` summary (spec 09 follow-up; in memory, no migration).

        Records are re-sliced through the same allowlist as local rows (no argv, env, cwd or paths) and their host is
        forced to the authenticated ``host_id``. Retention: ``per_host_builds`` builds per host, ``max_hosts`` hosts.
        State is lost on restart; hosts re-send their recent window (replays are idempotent).
        """
        if not isinstance(body, dict) or set(body) - {"host_id", "builds"}:
            raise ValueError("JSON object with host_id and builds required")
        builds = body.get("builds")
        if not isinstance(builds, list) or len(builds) > INGEST_MAX_RECORDS:
            raise ValueError(f"builds must be a list of at most {INGEST_MAX_RECORDS} records")
        records = []
        for raw in builds:
            if not isinstance(raw, dict) or raw.get("event") not in _BUILD_EVENTS \
                    or not isinstance(raw.get("build_id"), str) or not _ID_RE.match(raw["build_id"]):
                raise ValueError("each record needs event (build_started|build_finished) and a valid build_id")
            rec = {k: (v[:256] if isinstance(v, str) else v) for k, v in _slim(raw).items()}
            rec["host"] = host_id
            records.append(rec)
        new = []
        with self._lock:
            if host_id not in self._remote and len(self._remote) >= self.ingest_cfg["max_hosts"]:
                raise IngestFull("too many reporting hosts")
            store = self._remote.setdefault(host_id, {})
            for rec in records:
                slot = store.pop(rec["build_id"], {})            # pop + reinsert keeps recency order
                if rec["event"] not in slot:
                    new.append(rec)
                slot[rec["event"]] = rec
                store[rec["build_id"]] = slot
                while len(store) > self.ingest_cfg["per_host_builds"]:
                    del store[next(iter(store))]
            self._remote_ts[host_id] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if self.record_event is not None:
            for rec in new:
                self._publish(rec)
        return {"accepted": len(records), "new": len(new), "host_id": host_id}

    # ---------------------------------------------------------------- reads
    def status(self) -> dict:
        from ..build_metrics import summarize
        out = {
            "enabled": True, "policy_enabled": self.cfg.enabled, "apply": "cli_only",
            "ingest": ({"remote_hosts": True, "source": "local_host_state_files+reports", "hosts": len(self._remote),
                        "retention": "in_memory"} if self.ingest_cfg["enabled"]
                       else {"remote_hosts": False, "source": "local_host_state_files"}),
            "sse": {"resumable": True, "path": "/api/v1/build-cache/events/stream", "replay_events": STREAM_RING},
            "log_present": self.log_path.exists(),
            "summary": summarize(self.log_path) if self.cfg.enabled else None,
            "policy": self.cfg.public_view(),
        }
        return out

    def hosts(self) -> dict:
        now = self._now()
        if self._hosts is None or now - self._hosts[0] > _HOSTS_TTL_S:
            from ..build_cache_inventory import collect
            cap = collect(self.cfg, state_dir=self.state_dir)
            self._hosts = (now, [cap.public_view()] if cap else [])
        rows = list(self._hosts[1])
        source = "local_host_only"
        if self.ingest_cfg["enabled"]:
            source = "local_plus_reported"
            with self._lock:
                rows += [{"host_id": h, "source": "reported", "last_report_ts": self._remote_ts.get(h),
                          "builds": len(b)} for h, b in sorted(self._remote.items())]
        return {"hosts": rows, "total": len(rows), "source": source, "cached_s": _HOSTS_TTL_S}

    def policies(self) -> dict:
        return {"policy": self.cfg.public_view(), "source": "control_plane_host_config", "scope": "this_host"}

    def preview(self, request_key: str | None) -> dict:
        out = {"mode": "dry_run", "apply": "cli_only", "request_key": request_key}
        if not self.cfg.enabled:
            return {**out, "plan": None, "reason": "build_cache_disabled", "plan_id": None}
        from ..build_cache_policy import run_eviction
        raw = run_eviction(self.cfg, self.state_dir, apply=False)
        plan = raw.get("plan")
        cands = []
        if plan:
            for v in plan.get("victims", []):
                digest = hashlib.sha256(str(v["path"]).encode()).hexdigest()[:16]
                cands.append({"id": digest, "bytes": v["bytes"], "reason": v["reason"]})
            plan = {k: plan[k] for k in ("reason", "used_bytes", "capacity_bytes", "hard_cap_bytes",
                                         "planned_bytes", "planned_count", "truncated")}
            plan["candidates"] = cands
            plan["skipped_count"] = len(raw["plan"].get("skipped", []))
        plan_id = hashlib.sha256(json.dumps([plan, raw.get("blocked_by")], sort_keys=True).encode()).hexdigest()[:16]
        reason = raw.get("reason")
        if raw.get("status") == "error":
            reason = "scan_failed"
        return {**out, "status": raw.get("status"), "reason": reason, "admission": raw.get("admission"),
                "blocked": raw.get("blocked_by"), "plan": plan, "plan_id": plan_id}


def _parse_int(query, name, default, lo, hi):
    raw = (query.get(name) or [None])[0]
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise ValueError(f"{name} must be a non-negative integer")
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"{name} out of range")
    return value


def _frame(frame: dict) -> bytes:
    data = json.dumps({k: v for k, v in frame.items() if k != "id"}, separators=(",", ":"))
    return f"id: {frame['id']}\nevent: {frame['event_type']}\ndata: {data}\n\n".encode()


def _stream(handler, service) -> None:
    """``GET /build-cache/events/stream``: SSE with ``id:``; ``Last-Event-ID`` (or ``?last_event_id=``) resumes."""
    query = parse_qs(urlsplit(handler.path).query)
    last = handler.headers.get("Last-Event-ID") or (query.get("last_event_id") or [None])[0]
    if last is not None and len(last) > 64:
        last = None
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    try:
        handler.connection.settimeout(30)
    except OSError:
        pass
    q, replay = service.subscribe(last)
    try:
        handler.wfile.write(b"retry: 3000\n\n")
        for frame in replay:
            handler.wfile.write(_frame(frame))
        handler.wfile.flush()
        started = last_write = time.monotonic()
        while time.monotonic() - started < _STREAM_MAX_S:
            try:
                frame = q.get(timeout=_STREAM_TICK_S)
            except queue.Empty:
                service.sync_events()                           # no background thread: poll the logs while connected
                if q not in service._subs:
                    break                                       # dropped as a slow client
                if time.monotonic() - last_write >= 15:
                    handler.wfile.write(b": keep-alive\n\n")
                    handler.wfile.flush()
                    last_write = time.monotonic()
                continue
            handler.wfile.write(_frame(frame))
            handler.wfile.flush()
            last_write = time.monotonic()
    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
        pass
    finally:
        service.unsubscribe(q)


def _route_report(handler, server) -> bool:
    """``POST /api/v1/host/build-cache/report`` {host_id, builds: [...]}: a host reports its recorded builds.

    Same conventions as the other host-report routes: ingest-role key (gated in ``_require`` via ``_HOST_REPORT``),
    ``host_id`` in the body checked with ``_host_key_allowed``. Off unless ``control_plane.build_cache.ingest.enabled``
    (then indistinguishable from an unknown route, returning False so normal routing answers 404).
    """
    service = getattr(server, "build_cache", None)
    if service is None or not service.ingest_cfg["enabled"]:
        return False
    try:
        body = handler._read_json(512 * 1024)
        host_id = body.get("host_id") if isinstance(body, dict) else None
        if not isinstance(host_id, str) or not _HOST_RE.match(host_id):
            raise ValueError("host_id is required")
        if not handler._host_key_allowed(host_id):
            handler._json({"error": "this API key may not act for that host"}, 403)
            return True
        handler._json(service.ingest(host_id, body))
    except IngestFull as exc:
        handler._json({"error": f"{exc}: raise control_plane.build_cache.ingest.max_hosts"}, 429)
    except ValueError as exc:
        handler._json({"error": str(exc)}, 400)
    return True


def route_build_cache(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/build-cache*``. Returns True when the request was answered.

    Authentication and the operator requirement for POST are enforced by the handler before routing
    (``_require``): 401 without a valid key, 403 for a non-operator POST. The control plane has no per-tenant keys,
    so there is no tenant dimension to check here; the data is this host's own.
    """
    if resource == "host" and parts[4:] == ["build-cache", "report"] and handler.command == "POST":
        return _route_report(handler, server)
    if resource != "build-cache":
        return False
    service = getattr(server, "build_cache", None)
    if service is None:                      # flag off: indistinguishable from an unknown route
        handler._json({"error": "not found"}, 404)
        return True
    tail = parts[4:]
    method = handler.command
    query = parse_qs(urlsplit(handler.path).query)
    try:
        if method == "GET":
            service.sync_events()
            if tail == ["events", "stream"]:
                _stream(handler, service)
            elif tail == ["status"]:
                handler._json(service.status())
            elif tail == ["hosts"]:
                handler._json(service.hosts())
            elif tail == ["policies"]:
                handler._json(service.policies())
            elif tail == ["builds"]:
                limit = _parse_int(query, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
                cursor = _parse_int(query, "cursor", 0, 0, 10_000_000)
                flt = {k: (query.get(k) or [None])[0] for k in (*_FILTERS, "status")}
                for k, v in flt.items():
                    if v is not None and (not _ID_RE.match(v) or (k == "status" and v not in (
                            "completed", "failed", "running_or_unknown"))):
                        raise ValueError(f"invalid {k} filter")
                handler._json(service.list_builds(flt, limit, cursor))
            elif len(tail) == 2 and tail[0] == "builds" and _ID_RE.match(tail[1]):
                row = service.get_build(tail[1])
                if row is None:
                    handler._json({"error": "not found"}, 404)
                else:
                    handler._json(row)
            else:
                handler._json({"error": "not found"}, 404)
            return True
        if method == "POST" and tail in (["cleanup", "preview"], ["cleanup", "execute"]):
            origin = handler.headers.get("Origin")
            if origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                           or urlsplit(origin).scheme not in {"http", "https"}):
                handler._deny(403, "cross-origin build-cache mutation refused")
                return True
            if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                handler._json({"error": "build-cache requests require application/json"}, 415)
                return True
            if tail[1] == "execute":
                handler._json({"error": "cleanup apply is CLI-only (herdr-eng build-cache-evict --apply)"}, 501)
                return True
            body = handler._read_json(4096)
            body = {} if body is None else body
            if not isinstance(body, dict):
                raise ValueError("JSON object body required")
            if set(body) - {"request_key"}:
                raise ValueError("unknown fields; preview takes only request_key")
            key = body.get("request_key")
            if key is not None and (not isinstance(key, str) or not _KEY_RE.match(key)):
                raise ValueError("invalid request_key")
            handler._json(service.preview(key))
            return True
    except ValueError as exc:
        handler._json({"error": str(exc)}, 400)
        return True
    handler._json({"error": "not found"}, 404)
    return True
