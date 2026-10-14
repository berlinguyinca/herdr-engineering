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
    # a subsequent update without a title must NOT clobber the human label
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.updated",
                "payload": {"cwd": "/work/msn1"}})
    m = repo.get_mission("MSN-1")
    assert m["title"] == "Control plane"
    assert m["cwd"] == "/work/msn1"


def test_mission_create_without_title_defaults_to_id():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-9",
                "event_type": "mission.started",
                "payload": {"purpose": "no label"}})
    assert repo.get_mission("MSN-9")["title"] == "MSN-9"


def test_service_upsert_is_idempotent_across_ingests():
    ingest = IngestService(ControlPlaneRepo())
    first = ingest.ingest({
        "entity_type": "service", "entity_id": "herdr-control-plane",
        "event_type": "service.started",
        "payload": {"host_id": "bender", "port": 8080,
                    "route": "http://bender:8080", "status": "healthy"},
    })
    second = ingest.ingest({
        "entity_type": "service", "entity_id": "herdr-control-plane",
        "event_type": "service.heartbeat",
        "payload": {"host_id": "bender", "port": 8080, "status": "healthy"},
    })
    assert first["entity_id"] == second["entity_id"]
    assert len(ingest.repo.list_services()) == 1
    svc = ingest.repo.get_service(first["entity_id"])
    assert svc["service_name"] == "herdr-control-plane"
    assert svc["host_id"] == "bender" and svc["port"] == 8080
    assert svc["route"] == "http://bender:8080"


def test_service_name_comes_from_payload_not_entity_id():
    # The fabric agent uses entity_id as a routing key (lease id) and carries
    # the display name in payload.service_name; the repo must key by the label.
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "lease_abc123",
        "event_type": "service.registered",
        "payload": {"service_name": "web on bender (port 8181)",
                    "host_id": "bender", "port": 18000,
                    "route": "http://dev.lan:18000", "status": "healthy"},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["service_name"] == "web on bender (port 8181)"
    assert svc["host_id"] == "bender"
    assert svc["port"] == 18000
    assert svc["route"] == "http://dev.lan:18000"


def test_service_description_and_owner_from_payload():
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "lease_abc",
        "event_type": "service.registered",
        "payload": {"service_name": "python on beast (port 3011)",
                    "host_id": "beast", "port": 18142,
                    "route": "http://dev.lan:18142", "status": "healthy",
                    "owner": "python",
                    "description": "Augur Console, a Python web app"},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["description"] == "Augur Console, a Python web app"
    assert svc["owner"] == "python"


def test_service_fallback_description_when_absent():
    # No explicit description in the payload: the repo still guarantees one.
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "herdr-control-plane",
        "event_type": "service.started",
        "payload": {"host_id": "bender", "port": 8080,
                    "route": "http://bender:8080", "status": "healthy"},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["description"]
    assert "bender" in svc["description"]


def test_service_unavailable_event_marks_status():
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "postgres",
        "event_type": "service.started",
        "payload": {"host_id": "bender", "port": 5432, "status": "healthy"},
    })
    ingest.ingest({
        "entity_type": "service", "entity_id": "postgres",
        "event_type": "service.unavailable",
        "payload": {"host_id": "bender", "port": 5432},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["status"] == "unavailable"


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
