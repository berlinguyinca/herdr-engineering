"""Host events from a non-agent source (the Ansible fleet update) set state only."""
import pytest

from herdr_engineering.control_plane.ingest import LIVENESS_SOURCES, IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _svc():
    clock = Clock()
    repo = ControlPlaneRepo(now=clock)
    return repo, IngestService(repo), clock


def _heartbeat(svc, event_type="host.heartbeat"):
    svc.ingest({"source": "fabric-agent", "entity_type": "host", "entity_id": "beast",
                "event_type": event_type,
                "payload": {"host_name": "beast-real", "tailnet_ip": "100.64.0.9",
                            "telemetry": {"cpu": 12.5, "mem": 40}}})


def _fleet(svc, host="beast", **payload):
    body = {"maintenance": False, "pins": {"herdr": "0.9.1"},
            "fleet_update": {"last_updated": None, "deferred_since": None,
                             "result": "deferred: agents running"}, **payload}
    return svc.ingest({"source": "fleet-update", "entity_type": "host", "entity_id": host,
                       "event_type": "host.fleet_update", "payload": body})


def test_fleet_update_source_is_not_a_liveness_source():
    assert "fleet-update" not in LIVENESS_SOURCES


@pytest.mark.parametrize("event_type", ["host.heartbeat", "host.unavailable"])
def test_state_event_never_touches_liveness_identity_or_metrics(event_type):
    repo, svc, clock = _svc()
    _heartbeat(svc, event_type)
    before = repo.get_host("beast")
    clock.t += 3600
    out = _fleet(svc)
    after = repo.get_host("beast")
    for key in ("last_seen", "status", "host_name", "tailnet_ip", "cpu", "mem", "latest"):
        assert after.get(key) == before.get(key), key
    assert after["pins"] == {"herdr": "0.9.1"}
    assert after["maintenance"] is False
    assert after["fleet_update"]["result"] == "deferred: agents running"
    assert out["host_id"] == "beast"
    # still stale: the main host's event is not proof the agent is alive
    assert "stale" in {p["code"] for p in after["problems"]}


def test_unknown_host_is_not_created():
    repo, svc, _ = _svc()
    out = _fleet(svc, host="ghost")
    assert out == {"entity_id": "ghost", "ignored": "unknown host"}
    assert repo.get_host("ghost") is None
    assert all(h["host_id"] != "ghost" for h in repo.list_hosts())


@pytest.mark.parametrize("key,value", [("telemetry", {"cpu": 1}), ("cpu", 1), ("mem", 2),
                                       ("disk", 3), ("inventory", {"os": "x"})])
def test_metrics_from_a_non_agent_source_are_rejected(key, value):
    repo, svc, _ = _svc()
    _heartbeat(svc)
    with pytest.raises(ValueError):
        _fleet(svc, **{key: value})
    assert "pins" not in repo.get_host("beast")  # nothing written


def test_bad_state_type_is_rejected_before_writing():
    repo, svc, _ = _svc()
    _heartbeat(svc)
    with pytest.raises(ValueError):
        _fleet(svc, maintenance="false")
    assert "maintenance" not in repo.get_host("beast")
