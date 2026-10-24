"""HTTP surface of host telemetry history."""
import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_api import Client

MAX_SAMPLE_BYTES = 16 * 1024  # the documented cap, stated here so this file runs before the store exists


@pytest.fixture()
def server(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield Client(svr.port), repo
    svr.shutdown()


def _beat(c, telemetry, host="bender", **extra):
    return c.post("/api/v1/ingest", {
        "source": "fabric-agent", "entity_type": "host", "entity_id": host,
        "event_type": "host.heartbeat",
        "payload": {"host_name": host, "tailnet_ip": "100.64.0.9",
                    "telemetry": telemetry, **extra}})


def test_range_returns_history(server):
    c, _ = server
    for cpu in (10.0, 20.0, 30.0):
        assert _beat(c, {"cpu": cpu, "mem": 5.0})[0] in (200, 201)
    for rng in ("1h", "24h", "7d"):
        status, _, body = c.get(f"/api/v1/hosts/bender/telemetry?range={rng}")
        assert status == 200
        data = json.loads(body)
        assert data["range"] == rng and data["samples"]
        assert len(data["samples"]) <= 300
    data = json.loads(c.get("/api/v1/hosts/bender/telemetry?range=1h")[2])
    assert [p["cpu"] for p in data["samples"]] == [10.0, 20.0, 30.0]


def test_unknown_range_is_400(server):
    c, _ = server
    _beat(c, {"cpu": 1.0})
    status, _, _ = c.get("/api/v1/hosts/bender/telemetry?range=1y")
    assert status == 400


def test_legacy_limit_param_unchanged(server):
    c, repo = server
    hid = repo.register_host("beast", "100.64.0.2")
    for i in range(150):
        repo.record_telemetry(hid, {"cpu_pct": i, "mem_pct": 1})
    status, _, body = c.get(f"/api/v1/hosts/{hid}/telemetry")
    assert status == 200
    samples = json.loads(body)["samples"]
    assert len(samples) == 100 and samples[-1]["cpu_pct"] == 149
    # the page asks for ?limit=1 and reads the newest sample
    status, _, body = c.get(f"/api/v1/hosts/{hid}/telemetry?limit=1")
    samples = json.loads(body)["samples"]
    assert status == 200 and samples[-1]["cpu_pct"] == 149
    # legacy POST body keeps working
    status, _ = c.post(f"/api/v1/hosts/{hid}/telemetry",
                       {"cpu_pct": 7, "mem_pct": 8, "disk_pct": 9})
    assert status == 200
    last = json.loads(c.get(f"/api/v1/hosts/{hid}/telemetry?limit=1")[2])["samples"][-1]
    assert (last["cpu_pct"], last["mem_pct"], last["disk_pct"]) == (7, 8, 9)


def test_oversized_payload_is_400_not_500(server):
    c, repo = server
    assert _beat(c, {"cpu": 12.0, "mem": 30.0})[0] in (200, 201)
    good = repo.get_host("bender")
    status, _ = _beat(c, {"cpu": 1.0, "pad": "x" * (MAX_SAMPLE_BYTES + 10)})
    assert status == 400
    h = repo.get_host("bender")
    assert h["last_seen"] == good["last_seen"] and h["latest"] == good["latest"]
    # the host list still serialises as strict JSON
    status, _, body = c.get("/api/v1/hosts")
    assert status == 200 and json.loads(body)["hosts"][0]["latest"]["cpu"] == 12.0


def test_host_record_carries_latest_and_slow_fields(server):
    c, _ = server
    _beat(c, {"cpu": 12.0}, updates={"security": 1}, versions={"pi": "0.9"},
          pins={"pi": "0.9"}, fleet_update={"result": "ok"}, maintenance=False)
    h = json.loads(c.get("/api/v1/hosts/bender")[2])
    assert h["latest"]["cpu"] == 12.0 and h["updates"] == {"security": 1}
    assert h["versions"] == {"pi": "0.9"} and h["pins"] == {"pi": "0.9"}
    assert h["fleet_update"] == {"result": "ok"} and h["maintenance"] is False
