"""Fleet version inventory: hosts report tool/plugin versions, the repo diffs them."""
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def report(svc, host, tools, plugins=None):
    svc.ingest({"entity_type": "host", "entity_id": host,
                "event_type": "host.inventory",
                "payload": {"host_name": host,
                            "inventory": {"tools": tools, "plugins": plugins or {}}}})


def test_matrix_marks_drift_and_missing_against_majority():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    report(svc, "bender", {"claude": "2.1.0", "pi": "1.3.0"}, {"superpowers": "7.0.0"})
    report(svc, "beast", {"claude": "2.1.0", "pi": "1.2.0"}, {"superpowers": "7.0.0"})
    report(svc, "mac", {"claude": "2.0.0"}, {})
    m = repo.fleet_inventory()
    comps = {(c["group"], c["name"]): c for c in m["components"]}
    claude = comps[("tools", "claude")]
    assert claude["reference"] == "2.1.0"
    assert claude["hosts"]["mac"] == {"version": "2.0.0", "state": "drifted"}
    pi = comps[("tools", "pi")]
    assert pi["hosts"]["mac"]["state"] == "missing"
    assert pi["drifted"] == 1 and pi["missing"] == 1
    assert comps[("plugins", "superpowers")]["hosts"]["mac"]["state"] == "missing"
    assert m["summary"]["hosts"] == 3


def test_hosts_without_inventory_are_left_out_and_junk_is_dropped():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    svc.ingest({"entity_type": "host", "entity_id": "old", "event_type": "host.heartbeat",
                "payload": {"host_name": "old"}})
    svc.ingest({"entity_type": "host", "entity_id": "bad", "event_type": "host.inventory",
                "payload": {"inventory": "nonsense"}})
    assert repo.fleet_inventory()["hosts"] == []


def test_inventory_survives_event_replay():
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    report(svc, "bender", {"claude": "2.1.0"})
    events = [{"entity_type": e["entity_type"], "entity_id": e["entity_id"],
               "event_type": e["event_type"], "payload": e["payload"],
               "source_timestamp": e.get("source_timestamp")}
              for e in repo.stream("host", "bender")]
    hosts = _reconstruct_from_events({"host:bender": events})[2]
    assert hosts["bender"]["inventory"]["tools"] == {"claude": "2.1.0"}


def test_heartbeat_load_becomes_a_telemetry_sample_and_list_stays_bounded():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    for i in range(600):
        svc.ingest({"entity_type": "host", "entity_id": "bender", "event_type": "host.heartbeat",
                    "payload": {"host_name": "bender", "cpu": i % 100, "mem": 50, "disk": 61}})
    svc.ingest({"entity_type": "host", "entity_id": "bender", "event_type": "host.heartbeat",
                "payload": {"cpu": True}})
    latest = repo.list_telemetry("bender", limit=1)[0]
    assert latest["mem_pct"] == 50 and latest["disk_pct"] == 61
    assert len(repo.list_telemetry("bender", limit=10_000)) == 500
