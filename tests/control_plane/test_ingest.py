"""Ingestion service: map runtime lifecycle events to idempotent upserts."""
import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def make_svc(repo=None):
    return IngestService(repo or ControlPlaneRepo())


def test_host_upsert_is_idempotent_across_heartbeats():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "host", "entity_id": "bender",
                "event_type": "host.registered",
                "payload": {"host_name": "bender", "tailnet_ip": "100.104.39.6"}})
    svc.ingest({"entity_type": "host", "entity_id": "bender",
                "event_type": "host.heartbeat",
                "payload": {"cpu": 12, "mem": 40}})
    svc.ingest({"entity_type": "host", "entity_id": "bender",
                "event_type": "host.heartbeat",
                "payload": {"cpu": 15}})
    hosts = repo.list_hosts()
    assert len(hosts) == 1  # one stable record, not three
    h = hosts[0]
    assert h["host_id"] == "bender"
    assert h["host_name"] == "bender"
    assert h["tailnet_ip"] == "100.104.39.6"
    assert h["cpu"] == 15
    # two heartbeats + one registered event recorded
    types = [e["event_type"] for e in repo.stream("host", "bender")]
    assert types == ["HostRegistered", "HostHeartbeat", "HostHeartbeat"]


def test_session_upsert_start_then_stop():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "session", "entity_id": "w1H:t1",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "agent_role": "pi",
                            "model": "claude"}})
    svc.ingest({"entity_type": "session", "entity_id": "w1H:t1",
                "event_type": "session.started",
                "payload": {"host_id": "bender"}})
    svc.ingest({"entity_type": "session", "entity_id": "w1H:t1",
                "event_type": "session.stopped"})
    sessions = repo.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["status"] == "stopped"
    assert s["agent_role"] == "pi"
    # one stream, 0-indexed sequences 0,1,2
    assert [e["sequence"] for e in repo.stream("session", "w1H:t1")] == [0, 1, 2]


def test_mission_upsert_links_sessions():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "Control plane", "purpose": "ingest"}})
    svc.ingest({"entity_type": "session", "entity_id": "sess-a",
                "event_type": "session.started",
                "payload": {"host_id": "beast", "mission_id": "MSN-1"}})
    m = repo.get_mission("MSN-1")
    assert m is not None
    assert m["title"] == "Control plane"
    assert "sess-a" in m["sessions"]
    # mission update moves stage
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.updated",
                "payload": {"stage": "testing"}})
    assert repo.get_mission("MSN-1")["stage"] == "testing"


def test_ingest_generic_records_event_for_unknown_types():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "workspace", "entity_id": "w1H",
                "event_type": "workspace.focus", "payload": {"tab": "t1"}})
    evs = repo.stream("workspace", "w1H")
    assert len(evs) == 1
    assert evs[0]["event_type"] == "workspace.focus"


def test_ingest_requires_entity_and_event():
    svc = make_svc()
    with pytest.raises(ValueError):
        svc.ingest({})
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "host", "entity_id": "x"})
