"""Host telemetry history: cleaning, bounded retention and 5-minute rollups.

Raw 30 s samples live in memory only, for 48 h. Every closed 5-minute bucket
becomes one rollup point (numeric fields averaged), kept 7 days; rollups and
the latest host state are the only things persisted, through ``persist`` (the
repo's durable sink), as a private ``host_metrics`` stream that
``hydrate(events)`` replays after a restart. The bucket still open when the
process stops is lost (at most five minutes).
"""
from __future__ import annotations

import json
import math
import threading
import time

MAX_SAMPLE_BYTES = 16 * 1024
MAX_STATE_BYTES = 16 * 1024
MAX_POINTS = 300
RAW_RETENTION_S = 48 * 3600
ROLLUP_RETENTION_S = 7 * 86400
BUCKET_S = 300
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
MAX_LIST = 8
TOP_N = 5
MAX_STR = 128

ENTITY_TYPE = "host_metrics"
ROLLUP_EVENT = "HostMetricsRollup"
STATE_EVENT = "HostStateObserved"
STATE_KEYS = ("updates", "versions", "pins", "fleet_update", "maintenance")

_PCT = {"cpu", "mem", "disk", "swap"}
_SCALARS = ("cpu", "load1", "load5", "load15", "mem", "mem_used_b", "mem_total_b",
            "swap", "disk", "uptime_s", "net_rx_bps", "net_tx_bps", "agents_running")
_MOUNT = {"path": 256, "pct": "p", "used_b": "n", "total_b": "n"}
_GPU = {"name": MAX_STR, "util_pct": "p", "vram_used_b": "n", "vram_total_b": "n",
        "temp_c": "n", "cores": "n"}
_TOP = {"pid": "n", "command": 64, "user": 64, "cpu_pct": "n", "mem_pct": "n",
        "owner": MAX_STR}


def _number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


def _clamp(v):
    return min(100, max(0, v))


def _clean_item(raw, schema):
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, kind in schema.items():
        v = raw.get(key)
        if isinstance(kind, int):  # a string field truncated to ``kind`` chars
            if isinstance(v, str):
                out[key] = v[:kind]
            elif v is None and key in raw:
                out[key] = None
            continue
        v = _number(v)
        if v is not None:
            out[key] = _clamp(v) if kind == "p" else v
    return out


def _clean_list(raw, schema, cap):
    if not isinstance(raw, list):
        return None
    items = [i for i in (_clean_item(r, schema) for r in raw[:cap]) if i is not None]
    return items


def _size(value, limit, what):
    try:
        size = len(json.dumps(value, default=str))
    except (TypeError, ValueError, RecursionError):
        raise ValueError(f"{what} is not serialisable") from None
    if size > limit:
        raise ValueError(f"{what} exceeds {limit} bytes")


def clean_metrics(raw: dict) -> dict:
    """A bounded, strict-JSON copy of one host sample; ``ValueError`` if unusable.

    Numeric fields that are not finite numbers (bool is not a number) are dropped,
    percentages are clamped to 0-100, lists are capped and strings truncated.
    Unknown fields are dropped."""
    if not isinstance(raw, dict):
        raise ValueError("telemetry must be a JSON object")
    _size(raw, MAX_SAMPLE_BYTES, "telemetry")
    out = {}
    for key in _SCALARS:
        v = _number(raw.get(key))
        if v is not None:
            out[key] = _clamp(v) if key in _PCT else v
    mounts = _clean_list(raw.get("mounts"), _MOUNT, MAX_LIST)
    if mounts:
        out["mounts"] = mounts
    gpus = _clean_list(raw.get("gpus"), _GPU, MAX_LIST)
    if gpus:
        out["gpus"] = gpus
    top = raw.get("top")
    if isinstance(top, dict):
        cleaned = {k: _clean_list(top.get(k), _TOP, TOP_N) for k in ("cpu", "mem")}
        cleaned = {k: v for k, v in cleaned.items() if v}
        if cleaned:
            out["top"] = cleaned
    return out


def clean_state(name: str, value):
    """Validate one host-state field (``updates``/``versions``/``pins``/``fleet_update``/``maintenance``)."""
    if value is None:
        return None
    if name == "maintenance":
        if not isinstance(value, bool):
            raise ValueError("maintenance must be a boolean")
        return value
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    _size(value, MAX_STATE_BYTES, name)
    try:  # strict JSON round trip: no NaN, no foreign objects
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be strict JSON") from None


def _point(sample: dict) -> dict:
    """The scalar series point for one sample (history never carries lists)."""
    p = {k: sample[k] for k in _SCALARS if k in sample}
    gpus = sample.get("gpus") or []
    util = [g["util_pct"] for g in gpus if "util_pct" in g]
    temp = [g["temp_c"] for g in gpus if "temp_c" in g]
    if util:
        p["gpu_util_pct"] = max(util)
    if temp:
        p["gpu_temp_c"] = max(temp)
    return p


def _average(points: list[dict], ts: float) -> dict:
    sums: dict = {}
    counts: dict = {}
    for p in points:
        for k, v in p.items():
            if k in ("ts", "at", "n"):
                continue
            sums[k] = sums.get(k, 0.0) + v
            counts[k] = counts.get(k, 0) + 1
    out = {k: round(sums[k] / counts[k], 3) for k in sums}
    out["ts"] = ts
    return out


def _downsample(points: list[dict]) -> list[dict]:
    if len(points) <= MAX_POINTS:
        return points
    step = math.ceil(len(points) / MAX_POINTS)
    return [_average(points[i:i + step], points[i]["ts"])
            for i in range(0, len(points), step)]


def _iso(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat()


def _payload(ev: dict) -> dict:
    p = ev.get("payload") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


class HostMetricsStore:
    """Per-host raw ring (48 h), closed 5-minute rollups (7 d) and the open bucket."""

    def __init__(self, *, now=None, persist=None):
        self._now = now or time.time
        self._persist = persist
        self._lock = threading.RLock()
        self._raw: dict[str, list[dict]] = {}
        self._rollups: dict[str, list[dict]] = {}
        self._open: dict[str, dict] = {}  # host -> {"start", "points"}
        self._state: dict[str, dict] = {}  # host -> last persisted state
        self._seq = 0

    # ---------------------------------------------------------------- write
    def record(self, host_id: str, sample: dict) -> None:
        """Add one cleaned sample; close (and persist) the previous bucket if due."""
        with self._lock:
            ts = self._now()
            point = {**_point(sample), "ts": ts}
            raw = self._raw.setdefault(host_id, [])
            raw.append(point)
            self._prune(host_id, ts)
            start = ts - ts % BUCKET_S
            bucket = self._open.get(host_id)
            if bucket is not None and bucket["start"] != start:
                self._close(host_id, bucket)
                bucket = None
            if bucket is None:
                bucket = self._open[host_id] = {"start": start, "points": []}
            bucket["points"].append(point)

    def observe_state(self, host_id: str, state: dict, latest: dict | None) -> None:
        """Persist host state when it changes (not once per heartbeat)."""
        with self._lock:
            durable = {k: state.get(k) for k in STATE_KEYS}
            if self._state.get(host_id, {}).get("fields") == durable:
                return
            self._state[host_id] = {"fields": durable}
            self._emit(host_id, STATE_EVENT, {"state": durable, "latest": latest})

    def _close(self, host_id, bucket):
        rollup = _average(bucket["points"], bucket["start"])
        rollup["n"] = len(bucket["points"])
        self._rollups.setdefault(host_id, []).append(rollup)
        # Aggregates only: ``latest`` rides the (change-driven) state event.
        self._emit(host_id, ROLLUP_EVENT, {"rollup": rollup})

    def _emit(self, host_id, event_type, payload):
        if self._persist is None:
            return
        self._seq = max(self._seq + 1, int(self._now() * 1000))
        try:
            self._persist({"entity_type": ENTITY_TYPE, "entity_id": host_id,
                           "event_type": event_type, "sequence": self._seq,
                           "payload": payload})
        except Exception:  # noqa: BLE001 - history persistence is best-effort
            pass

    def _prune(self, host_id, now):
        raw = self._raw.get(host_id)
        if raw:
            cut = now - RAW_RETENTION_S
            if raw[0]["ts"] < cut:
                raw[:] = [p for p in raw if p["ts"] >= cut]
        rollups = self._rollups.get(host_id)
        if rollups:
            cut = now - ROLLUP_RETENTION_S
            if rollups[0]["ts"] < cut:
                rollups[:] = [p for p in rollups if p["ts"] >= cut]
        bucket = self._open.get(host_id)
        if bucket and bucket["start"] < now - ROLLUP_RETENTION_S:
            self._open.pop(host_id, None)

    # ----------------------------------------------------------------- read
    def history(self, host_id: str, range: str) -> list[dict]:
        if range not in RANGES:
            raise ValueError(f"range must be one of {', '.join(RANGES)}")
        with self._lock:
            now = self._now()
            self._prune(host_id, now)
            cut = now - RANGES[range]
            if range == "1h":
                points = [p for p in self._raw.get(host_id, []) if p["ts"] >= cut]
            else:
                points = [p for p in self._rollups.get(host_id, []) if p["ts"] >= cut - BUCKET_S]
                bucket = self._open.get(host_id)
                if bucket and bucket["points"]:
                    partial = _average(bucket["points"], bucket["start"])
                    partial["n"] = len(bucket["points"])
                    points.append(partial)
            points = _downsample([dict(p) for p in points])
        for p in points:
            p["at"] = _iso(p["ts"])
        return points

    def has(self, host_id: str) -> bool:
        with self._lock:
            return bool(self._raw.get(host_id) or self._rollups.get(host_id)
                        or self._open.get(host_id))

    def raw_count(self, host_id: str) -> int:
        with self._lock:
            self._prune(host_id, self._now())
            return len(self._raw.get(host_id, []))

    def rollup_count(self, host_id: str) -> int:
        with self._lock:
            self._prune(host_id, self._now())
            return len(self._rollups.get(host_id, []))

    # -------------------------------------------------------------- restore
    def hydrate(self, events: list[dict]) -> dict:
        """Replay persisted rollups/state; returns ``{host_id: {"latest", **state}}``."""
        hosts: dict[str, dict] = {}
        replay: dict[str, dict] = {}
        with self._lock:
            ordered = sorted(events, key=lambda e: int(e.get("sequence") or 0))
            for ev in ordered:
                hid = ev.get("entity_id")
                if not hid:
                    continue
                p = _payload(ev)
                self._seq = max(self._seq, int(ev.get("sequence") or 0))
                rec = hosts.setdefault(hid, {})
                # Only the state event carries ``latest`` (with its ``at``); a
                # rollup, including an older one that carried a sample, never
                # overwrites it.
                if ev.get("event_type") == STATE_EVENT and isinstance(p.get("latest"), dict):
                    rec["latest"] = p["latest"]
                if isinstance(p.get("state"), dict):
                    for k in STATE_KEYS:
                        rec[k] = p["state"].get(k)
                    self._state[hid] = {"fields": {k: p["state"].get(k) for k in STATE_KEYS}}
                rollup = p.get("rollup")
                if ev.get("event_type") == ROLLUP_EVENT and isinstance(rollup, dict) \
                        and isinstance(rollup.get("ts"), (int, float)):
                    # Keyed by ts: a later rollup for the same ts replaces the earlier one. Filtering the whole
                    # list per event was quadratic and made a boot over ~1M telemetry events take minutes.
                    by_ts = replay.get(hid)
                    if by_ts is None:
                        by_ts = replay[hid] = {r["ts"]: r for r in self._rollups.get(hid, [])}
                    by_ts[rollup["ts"]] = dict(rollup)
            for hid, by_ts in replay.items():
                self._rollups[hid] = list(by_ts.values())
            for hid, rows in self._rollups.items():
                rows.sort(key=lambda r: r["ts"])
                self._prune(hid, self._now())
        return hosts
