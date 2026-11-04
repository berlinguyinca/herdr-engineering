"""Host telemetry storage: cleaning, retention, rollups and durable history."""
import json
import math

import pytest

from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.host_metrics_store import (
    MAX_POINTS,
    MAX_SAMPLE_BYTES,
    HostMetricsStore,
    clean_metrics,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

T0 = 1_800_000_000.0  # a 5-minute bucket boundary (divisible by 300)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeSource:
    def __init__(self, events=()):
        self._e = list(events)

    def missions(self):
        return []

    def sessions(self):
        return []

    def services(self):
        return []

    def hosts(self):
        return []

    def events(self):
        return self._e


def _sample(cpu=10.0, **kw):
    return {"cpu": cpu, "mem": 40.0, "disk": 50.0, "load1": 0.5, **kw}


def _beat(ingest, host="bender", **payload):
    return ingest.ingest({"source": "fabric-agent", "entity_type": "host",
                          "entity_id": host, "event_type": "host.heartbeat",
                          "payload": {"host_name": host, **payload}})


def test_clean_metrics_clamps_and_drops_nan():
    raw = {
        "cpu": 140.0, "mem": -5, "disk": float("nan"), "swap": float("inf"),
        "load1": True, "load5": "1.2", "load15": 0.7, "uptime_s": 100,
        "mem_used_b": 10, "net_rx_bps": float("-inf"),
        "mounts": [{"path": f"/m{i}", "pct": 150, "used_b": 1, "total_b": 2}
                   for i in range(12)],
        "gpus": [{"name": "g" * 500, "util_pct": 101, "temp_c": float("nan"),
                  "vram_used_b": 1, "vram_total_b": 2} for _ in range(10)],
        "top": {"cpu": [{"pid": i, "command": "x" * 300, "user": "u",
                         "cpu_pct": 350.0, "mem_pct": 1, "owner": None}
                        for i in range(9)],
                "mem": [{"pid": 1, "command": "y", "user": "u", "cpu_pct": 1,
                         "mem_pct": float("nan"), "owner": "pi-1"}]},
        "agents_running": 3, "unknown_field": 7,
    }
    out = clean_metrics(raw)
    assert out["cpu"] == 100.0 and out["mem"] == 0
    assert "disk" not in out and "swap" not in out and "net_rx_bps" not in out
    assert "load1" not in out and "load5" not in out  # bool / str are not numbers
    assert out["load15"] == 0.7 and out["uptime_s"] == 100 and out["agents_running"] == 3
    assert "unknown_field" not in out
    assert len(out["mounts"]) == 8 and all(m["pct"] == 100 for m in out["mounts"])
    assert len(out["gpus"]) == 8
    g = out["gpus"][0]
    assert g["util_pct"] == 100 and "temp_c" not in g and len(g["name"]) <= 128
    assert len(out["top"]["cpu"]) == 5
    assert len(out["top"]["cpu"][0]["command"]) <= 64
    assert "mem_pct" not in out["top"]["mem"][0]
    assert out["top"]["mem"][0]["owner"] == "pi-1"
    json.dumps(out, allow_nan=False)  # always serialisable as strict JSON


def test_clean_metrics_rejects_oversized_and_non_dict():
    with pytest.raises(ValueError):
        clean_metrics({"cpu": 1, "pad": "x" * (MAX_SAMPLE_BYTES + 1)})
    with pytest.raises(ValueError):
        clean_metrics(["not", "a", "dict"])


def test_history_range_returns_at_most_300_points():
    clock = Clock()
    store = HostMetricsStore(now=clock)
    for i in range(int(7 * 86400 / 30)):  # a full week of 30 s samples
        store.record("bender", _sample(cpu=float(i % 100)))
        clock.advance(30)
    for rng in ("1h", "24h", "7d"):
        pts = store.history("bender", rng)
        assert 0 < len(pts) <= MAX_POINTS, rng
        assert all(math.isfinite(p["cpu"]) for p in pts)
        assert pts == sorted(pts, key=lambda p: p["ts"])
    assert len(store.history("bender", "1h")) == 120  # raw 30 s points
    # downsampling averages: a 7 d point spans many samples, so it is not a raw value
    week = store.history("bender", "7d")
    assert week[0]["ts"] >= clock() - 7 * 86400 - 300


def test_raw_samples_expire_after_48h_and_rollups_after_7d():
    clock = Clock()
    store = HostMetricsStore(now=clock)
    store.record("bender", _sample())
    clock.advance(300)
    store.record("bender", _sample())  # closes the first 5-minute bucket
    assert store.raw_count("bender") == 2 and store.rollup_count("bender") == 1
    clock.advance(47 * 3600)
    assert store.raw_count("bender") == 2
    clock.advance(2 * 3600)  # beyond 48 h: raw gone even without new writes
    assert store.raw_count("bender") == 0
    assert store.rollup_count("bender") >= 1
    assert store.history("bender", "1h") == []
    clock.advance(6 * 86400)  # beyond 7 d
    assert store.rollup_count("bender") == 0
    assert store.history("bender", "7d") == []


def test_unknown_range_raises_value_error():
    store = HostMetricsStore(now=Clock())
    with pytest.raises(ValueError):
        store.history("bender", "1y")


def test_history_survives_new_repo_over_same_sink(acknowledged_sink):
    clock = Clock()
    repo = ControlPlaneRepo(sink=acknowledged_sink, now=clock)
    ingest = IngestService(repo)
    samples = 0
    for i in range(60):  # 30 minutes at 30 s
        _beat(ingest, tailnet_ip="100.64.0.9",
              telemetry=_sample(cpu=float(i)),
              updates={"manager": "apt", "security": 2},
              versions={"herdr": "1.2.3"}, pins={"herdr": "1.2.3"},
              fleet_update={"last_updated": "2026-10-23", "deferred_since": None,
                            "result": "ok"},
              maintenance=True)
        samples += 1
        clock.advance(30)
    before = repo.host_history("bender", "24h")
    assert before

    metric_events = [e for e in acknowledged_sink.events
                     if e["entity_type"] == "host_metrics"]
    # rollups (one per 5 min) plus a state change, never one per sample
    assert 0 < len(metric_events) <= samples // 10 + 2

    fresh = ControlPlaneRepo(now=clock)
    load_into(fresh, FakeSource(events=acknowledged_sink.events))
    after = fresh.host_history("bender", "24h")
    # every closed bucket survives; only the still-open bucket (<= 5 min) is lost
    closed = before[:-1]
    assert [p["ts"] for p in after] == [p["ts"] for p in closed]
    assert after[0]["cpu"] == pytest.approx(before[0]["cpu"])
    h = fresh.get_host("bender")
    assert h["latest"]["cpu"] is not None
    assert h["updates"] == {"manager": "apt", "security": 2}
    assert h["versions"] == {"herdr": "1.2.3"} and h["pins"] == {"herdr": "1.2.3"}
    assert h["fleet_update"]["result"] == "ok" and h["maintenance"] is True
    assert h["tailnet_ip"] == "100.64.0.9"
    # the private metric stream is not on the shared activity feed
    assert not [e for e in fresh.recent_events(limit=500)
                if e.get("entity_type") == "host_metrics"]


def test_record_host_metrics_sets_record_and_legacy_keys():
    clock = Clock()
    repo = ControlPlaneRepo(now=clock)
    ingest = IngestService(repo)
    _beat(ingest, tailnet_ip="100.64.0.9", telemetry=_sample(cpu=12.0))
    _beat(ingest, telemetry=_sample(cpu=13.0))  # no tailnet_ip this time
    h = repo.get_host("bender")
    assert h["tailnet_ip"] == "100.64.0.9"
    assert h["latest"]["cpu"] == 13.0 and h["cpu"] == 13.0
    last = repo.list_telemetry("bender", limit=1)[0]
    assert last["cpu_pct"] == 13.0 and last["mem_pct"] == 40.0 and last["disk_pct"] == 50.0


def test_bad_telemetry_does_not_touch_host_record():
    clock = Clock()
    repo = ControlPlaneRepo(now=clock)
    ingest = IngestService(repo)
    _beat(ingest, telemetry=_sample(cpu=12.0))
    good = repo.get_host("bender")
    clock.advance(30)
    with pytest.raises(ValueError):
        _beat(ingest, telemetry={"cpu": 1, "pad": "x" * (MAX_SAMPLE_BYTES + 1)})
    with pytest.raises(ValueError):
        _beat(ingest, telemetry=_sample(), updates="not a dict")
    h = repo.get_host("bender")
    assert h["last_seen"] == good["last_seen"] and h["latest"] == good["latest"]


def _realistic_sample(cpu=20.0):
    top = [{"pid": 1000 + i, "command": "python3", "user": "wohlgemuth",
            "cpu_pct": 12.5, "mem_pct": 3.1, "owner": "pi-worker-1"} for i in range(5)]
    return {
        **_sample(cpu=cpu), "load5": 0.4, "load15": 0.3, "swap": 2.0,
        "mem_used_b": 12_000_000_000, "mem_total_b": 64_000_000_000,
        "uptime_s": 123456, "net_rx_bps": 120000, "net_tx_bps": 80000, "agents_running": 4,
        "mounts": [{"path": f"/mnt/data{i}", "pct": 40.0, "used_b": 4 * 10**11,
                    "total_b": 10**12} for i in range(8)],
        "gpus": [{"name": "NVIDIA GeForce RTX 4090", "util_pct": 55.0,
                  "vram_used_b": 8 * 10**9, "vram_total_b": 24 * 10**9, "temp_c": 61.0}
                 for _ in range(2)],
        "top": {"cpu": top, "mem": top},
    }


def test_rollup_events_carry_aggregates_only_and_stay_small(acknowledged_sink):
    clock = Clock()
    repo = ControlPlaneRepo(sink=acknowledged_sink, now=clock)
    ingest = IngestService(repo)
    for i in range(40):  # 20 minutes -> several closed buckets
        _beat(ingest, telemetry=_realistic_sample(cpu=float(i)))
        clock.advance(30)
    rollups = [e for e in acknowledged_sink.events if e["event_type"] == "HostMetricsRollup"]
    assert len(rollups) >= 3
    for ev in rollups:
        assert set(ev["payload"]) == {"rollup"}
        assert len(json.dumps(ev["payload"])) < 1024


def test_latest_timestamp_survives_new_repo_over_same_sink(acknowledged_sink):
    clock = Clock()
    repo = ControlPlaneRepo(sink=acknowledged_sink, now=clock)
    ingest = IngestService(repo)
    for i in range(30):  # 15 minutes: rollups are written after the state event
        _beat(ingest, telemetry=_realistic_sample(cpu=1.0), updates={"security": 0})
        clock.advance(30)
    live = repo.get_host("bender")["latest"]
    assert live["at"]
    fresh = ControlPlaneRepo(now=clock)
    load_into(fresh, FakeSource(events=acknowledged_sink.events))
    latest = fresh.get_host("bender")["latest"]
    assert latest["at"] and latest["cpu"] == 1.0


def test_legacy_rollup_with_sample_never_overwrites_latest():
    store = HostMetricsStore(now=Clock())
    events = [
        {"entity_id": "bender", "event_type": "HostStateObserved", "sequence": 1,
         "payload": {"state": {"updates": None}, "latest": {"cpu": 5.0, "at": "t1"}}},
        {"entity_id": "bender", "event_type": "HostMetricsRollup", "sequence": 2,
         "payload": {"rollup": {"ts": T0, "cpu": 9.0, "n": 10}, "latest": {"cpu": 9.0}}},
    ]
    assert store.hydrate(events)["bender"]["latest"] == {"cpu": 5.0, "at": "t1"}


def test_hydrate_replays_many_rollups_in_linear_time_and_the_later_duplicate_wins():
    """Boot replays every stored rollup; filtering the list per event made 1M telemetry events take minutes."""
    import time
    now = 1_000_000.0
    store = HostMetricsStore(now=lambda: now)
    events = [{"entity_id": "bender", "event_type": "HostMetricsRollup", "sequence": i,
               "payload": {"rollup": {"ts": now - 3600 + (i % 3000), "marker": i}}} for i in range(60_000)]
    started = time.monotonic()
    store.hydrate(events)
    assert time.monotonic() - started < 3.0
    rows = store._rollups["bender"]
    assert [r["ts"] for r in rows] == sorted({r["ts"] for r in rows}) and len(rows) == 3000
    assert max(r["marker"] for r in rows) > 57_000        # each ts kept its LATEST event
