"""LLM page statistics (docs/superpowers/specs/2026-10-23-llm-page-design.md, slice 2).

Pure functions over token_usage event payloads: requests, tokens, TTFT, generation
speed, end-to-end throughput, queue wait and error classes, per group and per time
bucket. Percentiles come from a fixed log-spaced histogram (no sample list is kept,
so a group costs a few hundred bytes), and a group needs ``MIN_SAMPLES`` samples
before one is shown. A field the record does not carry is unknown, never 0, and
numbers from different ``source`` values are never added together.
"""
from __future__ import annotations

import bisect
import datetime as _dt

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
STEPS = {"5m": 300, "1h": 3600, "1d": 86400}
MAX_POINTS = 500
MIN_SAMPLES = 5
MIN_GEN_MS = 50          # a shorter generation phase makes tokens/s meaningless
GROUPS = ("model", "project", "session", "route")
METRICS = ("requests", "tokens_out", "errors", "ttft_p95", "speed_p50")
# Log-spaced bucket upper bounds, 10 ms .. 600 s for latencies (about 12% apart), and
# 0.1 .. 10 000 for tokens/s. A percentile is the upper bound of the bucket holding
# that rank, so it is exact to within one bucket.
_LAT_BOUNDS = [10 * (60000 / 10) ** (i / 63) for i in range(64)]
_SPEED_BOUNDS = [0.1 * (100000 ** (i / 63)) for i in range(64)]


class Dist:
    """A fixed-bucket histogram with count, observed min and max."""

    __slots__ = ("bounds", "counts", "n", "lo", "hi")

    def __init__(self, bounds):
        self.bounds = bounds
        self.counts = [0] * (len(bounds) + 1)
        self.n = 0
        self.lo = self.hi = None

    def add(self, value):
        self.counts[bisect.bisect_left(self.bounds, value)] += 1
        self.n += 1
        self.lo = value if self.lo is None else min(self.lo, value)
        self.hi = value if self.hi is None else max(self.hi, value)

    def quantile(self, q):
        rank = max(1, -(-round(q * 100) * self.n // 100))  # ceil(q * n), q in whole percent
        seen = 0
        for i, c in enumerate(self.counts):
            seen += c
            if seen >= rank:
                upper = self.bounds[i] if i < len(self.bounds) else self.hi
                return round(min(max(upper, self.lo), self.hi), 1)
        return None

    def summary(self):
        if self.n < MIN_SAMPLES:
            return {"n": self.n, "p50": None, "p95": None, "insufficient": True}
        return {"n": self.n, "p50": self.quantile(0.50), "p95": self.quantile(0.95),
                "insufficient": False}


def _parse(ts):
    try:
        return _dt.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def is_error(p):
    """A v2 record is an error when it has an error class or an HTTP status >= 400; an
    older record only has ``success``."""
    if p.get("schema") == 2:
        status = p.get("status")
        return p.get("error_class") is not None or (isinstance(status, int) and status >= 400)
    return not p.get("success", True)


def speed(p):
    """Generation tokens/s: streams with a known usage and a long enough generation."""
    out, gen = p.get("output_tokens"), p.get("gen_ms")
    if p.get("schema") != 2 or p.get("stream") is not True or p.get("usage_known") is not True:
        return None
    if not isinstance(out, int) or not isinstance(gen, int) or gen < MIN_GEN_MS:
        return None
    return out / (gen / 1000)


def e2e(p):
    """Throughput of a non-stream request, ``output_tokens / duration``; never "speed"."""
    out, dur = p.get("output_tokens"), p.get("duration_ms")
    if p.get("schema") != 2 or p.get("stream") is not False or p.get("usage_known") is not True:
        return None
    if not isinstance(out, int) or not isinstance(dur, int) or dur <= 0:
        return None
    return out / (dur / 1000)


class Group:
    def __init__(self, key, source):
        self.key, self.source = key, source
        self.requests = self.errors = 0
        self.tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
        self.lower_bound = False
        self.ttft, self.queue = Dist(_LAT_BOUNDS), Dist(_LAT_BOUNDS)
        self.speed, self.e2e = Dist(_SPEED_BOUNDS), Dist(_SPEED_BOUNDS)
        self.first = self.last = None

    def add(self, p, ts):
        self.requests += 1
        self.errors += is_error(p)
        if p.get("schema") == 2 and p.get("usage_known") is not True and not is_error(p):
            self.lower_bound = True   # a success whose upstream sent no usage object
        for name, field in (("input", "input_tokens"), ("output", "output_tokens"),
                            ("reasoning", "reasoning_tokens"), ("cache_read", "cache_read_tokens")):
            value = p.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                self.tokens[name] += value
        if p.get("schema") == 2:
            for dist, field in ((self.ttft, "ttft_ms"), (self.queue, "queue_ms")):
                value = p.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    dist.add(value)
            for dist, fn in ((self.speed, speed), (self.e2e, e2e)):
                value = fn(p)
                if value is not None:
                    dist.add(value)
        self.first = ts if self.first is None else min(self.first, ts)
        self.last = ts if self.last is None else max(self.last, ts)

    def out(self):
        return {"key": self.key, "source": self.source, "requests": self.requests,
                "errors": self.errors,
                "error_rate": round(self.errors / self.requests, 4) if self.requests else None,
                "tokens": dict(self.tokens, lower_bound=self.lower_bound),
                "ttft_ms": self.ttft.summary(), "queue_ms": self.queue.summary(),
                "speed_tps": self.speed.summary(), "e2e_tps": self.e2e.summary(),
                "first_seen": _iso(self.first), "last_seen": _iso(self.last)}


def _iso(ts):
    return None if ts is None else _dt.datetime.fromtimestamp(ts, _dt.UTC).isoformat().replace("+00:00", "Z")


def group_key(by, p, resolve):
    """The group a record belongs to, or None when it has none (a record without a
    session id is left out of the per-session grouping)."""
    if by == "model":
        return p.get("model") or "unknown"
    if by == "project":
        return p.get("project") or "unknown"
    if by == "route":
        return p.get("route") or "unknown"
    raw = p.get("session_id")
    if not raw:
        return None
    hit = resolve(raw)
    return hit["session_id"] or f"unmatched:{raw}"


def select(events, payload_of, *, since, until=None, project=None, model=None, session=None,
           mission=None, resolve=None):
    """``(ts, payload)`` for each event inside the window that passes the filters. An
    older record carries its session id only on the event envelope; it is copied into
    the payload so every grouping sees it."""
    resolve = resolve or (lambda raw: {"session_id": raw, "match": "direct"})
    for ev in events:
        ts = _parse(ev.get("source_timestamp"))
        if ts is None or ts < since or (until is not None and ts >= until):
            continue
        p = payload_of(ev)
        if not p.get("session_id") and ev.get("session_id"):
            p = {**p, "session_id": ev["session_id"]}
        if project and p.get("project") != project:
            continue
        if model and p.get("model") != model:
            continue
        if mission and ev.get("mission_id") != mission:
            continue
        if session:
            raw = p.get("session_id")
            if not raw or (resolve(raw)["session_id"] or f"unmatched:{raw}") != session:
                continue
        yield ts, p


def stats(records, by, resolve):
    groups = {}
    for ts, p in records:
        key = group_key(by, p, resolve)
        if key is None:
            continue
        source = p.get("source") or "llm-proxy"
        groups.setdefault((key, source), Group(key, source)).add(p, ts)
    return sorted((g.out() for g in groups.values()), key=lambda g: (-g["requests"], g["key"]))


def timeseries(records, metric, *, since, until, step):
    """One point per ``step`` from ``since`` to ``until``; an empty bucket is ``None`` for
    the latency metrics (unknown) and 0 for the counts."""
    n = max(1, -int(-(until - since) // step))
    if n > MAX_POINTS:
        raise ValueError(f"window/step would give {n} points (max {MAX_POINTS})")
    buckets = [Group(None, None) for _ in range(n)]
    for ts, p in records:
        buckets[min(int((ts - since) // step), n - 1)].add(p, ts)
    points = []
    for i, b in enumerate(buckets):
        if metric == "requests":
            value = b.requests
        elif metric == "errors":
            value = b.errors
        elif metric == "tokens_out":
            value = b.tokens["output"]
        elif metric == "ttft_p95":
            value = b.ttft.summary()["p95"]
        else:
            value = b.speed.summary()["p50"]
        points.append({"t": _iso(since + i * step), "v": value,
                       "n": b.requests})
    return points


def errors(records, resolve, *, error_class=None, session_limit=20):
    """Problems grouped by error class, then model; a record that failed without a class
    (an older v1 record, or an unclassified status) is ``unclassified``."""
    groups = {}
    for ts, p in records:
        if not is_error(p):
            continue
        cls = p.get("error_class") or "unclassified"
        if error_class and cls != error_class:
            continue
        g = groups.setdefault((cls, p.get("model") or "unknown"), {
            "class": cls, "model": p.get("model") or "unknown", "count": 0, "first_seen": ts,
            "last_seen": ts, "statuses": {}, "sessions": set()})
        g["count"] += 1
        g["first_seen"], g["last_seen"] = min(g["first_seen"], ts), max(g["last_seen"], ts)
        if isinstance(p.get("status"), int):
            g["statuses"][str(p["status"])] = g["statuses"].get(str(p["status"]), 0) + 1
        raw = p.get("session_id")
        if raw:
            hit = resolve(raw)
            g["sessions"].add(hit["session_id"] or f"unmatched:{raw}")
    out = []
    for g in groups.values():
        sessions = sorted(g.pop("sessions"))
        out.append(dict(g, first_seen=_iso(g["first_seen"]), last_seen=_iso(g["last_seen"]),
                        session_count=len(sessions), sessions=sessions[:session_limit]))
    return sorted(out, key=lambda g: (-g["count"], g["class"], g["model"]))
