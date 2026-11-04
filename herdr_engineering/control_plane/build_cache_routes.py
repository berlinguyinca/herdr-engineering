"""``/api/v1/build-cache`` routes (spec 09) and the flag-gated service behind them.

Config (YAML only, never environment variables)::

    control_plane:
      build_cache:
        enabled: false              # default; when off the routes answer 404 and nothing is constructed
        state_dir: /path            # optional; default is the build-gate state dir of THIS host
        fabric_build_cache: {...}   # optional; the host's ``fabric_build_cache`` section (default: the host config)

Scope, stated honestly:

* Read-only data comes from THIS control-plane host's local state files (``builds.jsonl``, host probe). Build hosts do
  not push their records to the control plane (no ingest, no table, no migration), so other hosts are not visible.
* ``POST cleanup/preview`` runs the existing dry-run planner; there is no apply endpoint (apply stays CLI-only).
* Lifecycle changes are published with ``repo.record_event`` (redacted, allowlisted). The shared SSE stream has no
  event ids, so there is no ``Last-Event-ID`` resume; a new client only gets the shared stream's 100-event replay.
* Events are published when a read request notices new ``builds.jsonl`` lines (no background thread). A quiet API
  therefore delays events; the first read after start only sets the baseline and never replays history.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
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
_FILTERS = ("mission", "session", "host", "cmd_kind")
_FIELDS = ("event", "ts", "build_id", "host", "slot", "cmd_kind", "exit_code", "elapsed_s", "user_cpu_s", "sys_cpu_s",
           "max_rss_kb", "rusage_oublock", "proc_io_write_bytes", "host_disk_write_bytes_delta", "host_disk_scope",
           "unavailable")
_EVENT_FIELDS = ("ts", "build_id", "host", "slot", "cmd_kind", "exit_code", "elapsed_s")
_SIMPLE = (str, int, float, bool, type(None))


def build_cache_config(cfg) -> dict:
    cfg = {} if cfg is None else cfg
    if not isinstance(cfg, dict):
        raise ValueError("control_plane.build_cache must be a mapping")
    unknown = set(cfg) - {"enabled", "state_dir", "fabric_build_cache"}
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
    return {"enabled": cfg.get("enabled", False) is True, "state_dir": state_dir, "fabric_build_cache": section}


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
    return BuildCacheService(validate(section), state, record_event=record_event)


def _trace(value):
    if not isinstance(value, dict):
        return None
    out = {k: v for k, v in value.items() if k in ("session", "mission") and isinstance(v, str) and len(v) <= 64}
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
    def __init__(self, cfg, state_dir: Path, *, record_event=None, now=time.monotonic):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.record_event = record_event
        self._now = now
        self._lock = threading.Lock()
        self._offset: int | None = None
        self._inode: int | None = None
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
        """Merged started/finished per build id, newest activity first."""
        merged: dict[str, dict] = {}
        for rec in self._tail_lines():
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
                "source": "local_builds_jsonl", "window_bytes": _TAIL_BYTES}

    def get_build(self, build_id: str) -> dict | None:
        for row in self._builds():
            if row["build_id"] == build_id:
                return row
        return None

    # ---------------------------------------------------------------- events
    def sync_events(self) -> int:
        """Publish builds.jsonl lines appended since the last call. The first call sets the baseline only."""
        if self.record_event is None:
            return 0
        with self._lock:
            try:
                st = os.stat(self.log_path)
            except OSError:
                if self._offset is None:
                    self._offset = 0                              # no log yet: a file created later is read from its start
                return 0
            if self._offset is None:
                self._offset, self._inode = st.st_size, st.st_ino
                return 0
            if st.st_ino != self._inode or st.st_size < self._offset:
                self._offset, self._inode = 0, st.st_ino           # rotated or truncated: read the new file
            if st.st_size == self._offset:
                return 0
            try:
                with open(self.log_path, "rb") as f:
                    f.seek(self._offset)
                    data = f.read(_SYNC_MAX_BYTES)
            except OSError:
                return 0
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) >= _SYNC_MAX_BYTES:
                    self._offset += len(data)                      # one absurd line: skip it
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
                if not isinstance(rec, dict) or rec.get("event") not in ("build_started", "build_finished"):
                    continue
                self._publish(rec)
                published += 1
            self._offset += consumed
            return published

    def _publish(self, rec: dict) -> None:
        payload = {k: rec[k] for k in _EVENT_FIELDS if k in rec and isinstance(rec[k], _SIMPLE)}
        trace = _trace(rec.get("trace"))
        if trace:
            payload["trace"] = trace
        if isinstance(rec.get("cache"), dict):
            payload["cache_applied"] = rec["cache"].get("applied") is True
        host = str(rec.get("host") or "unknown")[:64]
        try:
            self.record_event("build_cache", host, f"build_cache.{rec['event']}", redact_payload(payload),
                              session_id=(trace or {}).get("session"))
        except Exception:  # noqa: BLE001 - publishing must never break a read
            pass

    # ---------------------------------------------------------------- reads
    def status(self) -> dict:
        from ..build_metrics import summarize
        out = {
            "enabled": True, "policy_enabled": self.cfg.enabled, "apply": "cli_only",
            "ingest": {"remote_hosts": False, "source": "local_host_state_files"},
            "sse": {"resumable": False, "note": "shared stream has no event ids; 100-event replay only"},
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
        rows = self._hosts[1]
        return {"hosts": rows, "total": len(rows), "source": "local_host_only", "cached_s": _HOSTS_TTL_S}

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


def route_build_cache(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/build-cache*``. Returns True when the request was answered.

    Authentication and the operator requirement for POST are enforced by the handler before routing
    (``_require``): 401 without a valid key, 403 for a non-operator POST. The control plane has no per-tenant keys,
    so there is no tenant dimension to check here; the data is this host's own.
    """
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
            if tail == ["status"]:
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
