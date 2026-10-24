"""An unchanged agent heartbeat refreshes liveness without a durable host event."""
import copy

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Sink:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(copy.deepcopy(event))


def _setup():
    clock, sink = Clock(), Sink()
    repo = ControlPlaneRepo(now=clock, sink=sink)
    return repo, IngestService(repo), clock, sink


def _beat(svc, event_type="host.heartbeat", **payload):
    body = {"host_name": "beast", "tailnet_ip": "100.64.0.9",
            "telemetry": {"cpu": 12.5, "mem": 40, "disk": 10}, **payload}
    return svc.ingest({"source": "fabric-agent", "entity_type": "host", "entity_id": "beast",
                       "event_type": event_type, "payload": body})


def _host_events(sink):
    return [e["event_type"] for e in sink.events if e.get("entity_type") == "host"]


def test_twenty_heartbeats_write_at_most_one_host_event_and_advance_last_seen():
    repo, svc, clock, sink = _setup()
    _beat(svc)
    first_seen = repo.get_host("beast")["last_seen"]
    for _ in range(19):
        clock.t += 30
        _beat(svc)
    assert _host_events(sink) == ["HostRegistered"]
    assert len(repo.stream("host", "beast")) == 1
    h = repo.get_host("beast")
    assert h["last_seen"] > first_seen
    assert h["status"] == "healthy"
    assert h["latest"]["cpu"] == 12.5
    # the telemetry still reaches the metrics stream
    assert any(e.get("entity_type") == "host_metrics" for e in sink.events)


def test_identity_change_still_records_an_event():
    repo, svc, clock, sink = _setup()
    _beat(svc)
    clock.t += 30
    _beat(svc, tailnet_ip="100.64.0.10")
    clock.t += 30
    _beat(svc, tailnet_ip="100.64.0.10", host_name="beast-renamed")
    clock.t += 30
    _beat(svc, tailnet_ip="100.64.0.10", host_name="beast-renamed",
          inventory={"tools": {"git": "2.43"}})
    assert _host_events(sink) == ["HostRegistered", "HostHeartbeat", "HostHeartbeat",
                                  "HostHeartbeat"]
    h = repo.get_host("beast")
    assert h["tailnet_ip"] == "100.64.0.10" and h["host_name"] == "beast-renamed"


def test_missing_tailnet_ip_never_overwrites_and_is_not_a_change():
    repo, svc, clock, sink = _setup()
    _beat(svc)
    clock.t += 30
    _beat(svc, tailnet_ip=None)
    assert _host_events(sink) == ["HostRegistered"]
    assert repo.get_host("beast")["tailnet_ip"] == "100.64.0.9"


def test_unavailable_and_recovery_transitions_are_recorded():
    repo, svc, clock, sink = _setup()
    _beat(svc)
    clock.t += 30
    _beat(svc, event_type="host.unavailable")
    assert repo.get_host("beast")["status"] == "unavailable"
    clock.t += 30
    _beat(svc)
    assert repo.get_host("beast")["status"] == "healthy"
    assert _host_events(sink) == ["HostRegistered", "HostHeartbeat", "HostHeartbeat"]


def test_legacy_cpu_mem_heartbeat_updates_in_memory_without_event():
    repo, svc, clock, sink = _setup()
    svc.ingest({"entity_type": "host", "entity_id": "bender", "event_type": "host.registered",
                "payload": {"host_name": "bender", "tailnet_ip": "100.104.39.6"}})
    clock.t += 30
    svc.ingest({"entity_type": "host", "entity_id": "bender", "event_type": "host.heartbeat",
                "payload": {"cpu": 12, "mem": 40}})
    h = repo.get_host("bender")
    assert h["cpu"] == 12 and h["mem"] == 40
    assert _host_events(sink) == ["HostRegistered"]
