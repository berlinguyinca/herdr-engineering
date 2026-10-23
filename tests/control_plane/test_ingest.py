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


def _session(svc, sid, event, **payload):
    svc.ingest({"entity_type": "session", "entity_id": sid, "event_type": event,
                "payload": {"host_id": "bender", **payload}})


def test_session_keeps_its_parent_and_name_and_ignores_bad_ones():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    _session(svc, "bender:s0", "session.started", name="lead")
    _session(svc, "bender:s1", "session.started", parent_session_id="bender:s0", name="helper-1")
    _session(svc, "bender:s1", "session.updated", status="working")        # a later event without them keeps them
    _session(svc, "bender:s2", "session.started", parent_session_id="bender:s2", name="x" * 500)
    _session(svc, "bender:s3", "session.started", parent_session_id=["not", "a", "string"], name="bad\x00name")
    got = {s["session_id"]: s for s in repo.list_sessions()}
    assert (got["bender:s1"]["parent_session_id"], got["bender:s1"]["name"]) == ("bender:s0", "helper-1")
    assert got["bender:s0"]["parent_session_id"] is None and got["bender:s0"]["name"] == "lead"
    for sid in ("bender:s2", "bender:s3"):                                  # self-parent, oversize, wrong type, control chars
        assert got[sid]["parent_session_id"] is None and got[sid]["name"] is None


def test_parent_and_name_survive_rebuilding_from_the_event_log():
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    _session(svc, "bender:s1", "session.started", parent_session_id="bender:s0", name="helper-1")
    _session(svc, "bender:s1", "session.updated", status="working")
    events = repo.stream("session", "bender:s1")
    rebuilt = _reconstruct_from_events({"session:bender:s1": events})
    sessions = next(x for x in rebuilt if isinstance(x, dict) and "bender:s1" in x)
    assert sessions["bender:s1"]["parent_session_id"] == "bender:s0" and sessions["bender:s1"]["name"] == "helper-1"


def test_session_recording_status_and_explicit_parent_survive_updates_and_restore():
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events

    svc = make_svc()
    parent = "herdr:bender:default:w1Z%3ApF"
    def emit(payload):
        return svc.ingest({"entity_type": "session", "entity_id": "helper",
                           "event_type": "session.updated", "payload": payload})

    emit({"host_id": "bender", "parent_session_id": parent,
          "transcript": {"status": "recording", "truncated": True, "text": "must not be stored"}})
    emit({"model": "new-model"})
    current = svc.repo.get_session("helper")
    assert current["transcript"] == {"status": "recording", "truncated": True}
    assert current["parent_session_id"] == parent
    emit({"transcript": {"status": "unreadable", "truncated": False}})
    events = svc.repo.stream("session", "helper")
    assert all("must not be stored" not in str(event) for event in events)
    _, sessions, _, _, _ = _reconstruct_from_events({"session|helper": events})
    assert sessions["helper"]["transcript"] == {"status": "unreadable", "truncated": False}
    assert sessions["helper"]["parent_session_id"] == parent


@pytest.mark.parametrize("metadata", [
    {"transcript": "recording"}, {"transcript": {"status": "invented"}},
    {"transcript": {"status": "recording", "truncated": "false"}},
])
def test_invalid_session_transcript_metadata_does_not_mutate_session(metadata):
    svc = make_svc()
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "session", "entity_id": "helper",
                    "event_type": "session.started", "payload": metadata})
    assert svc.repo.get_session("helper") is None


def test_session_retains_validated_identity_repair_status():
    svc = make_svc()
    svc.ingest({"entity_type": "session", "entity_id": "helper", "event_type": "session.updated",
                "payload": {"transcript": {"status": "recording", "identity_repair": "report_unverified"}}})
    assert svc.repo.get_session("helper")["transcript"]["identity_repair"] == "report_unverified"
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "session", "entity_id": "bad", "event_type": "session.updated",
                    "payload": {"transcript": {"status": "recording", "identity_repair": "invented"}}})
    assert svc.repo.get_session("bad") is None


@pytest.mark.parametrize("entity_type", [
    "pr_gate", "pr_gate_author", "review_job", "pr_gate_future", "review_jobs", "PR_GATE", " pr_gate"])
def test_generic_ingest_rejects_review_gate_streams(entity_type):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match="reserved"):
        make_svc(repo).ingest({
            "entity_type": entity_type, "entity_id": "github.com/acme/widgets#7",
            "event_type": "verdict", "payload": {"verdict": "PASS", "sha": "a" * 40}})
    assert repo.stream(entity_type, "github.com/acme/widgets#7") == []


def test_reserved_prefixes_cover_every_gate_stream():
    from herdr_engineering.review_gate import queue, registry, store
    from herdr_engineering.review_gate.entities import RESERVED_ENTITY_PREFIXES

    for entity in (store.ENTITY, queue.ENTITY, registry.ENTITY):
        assert entity.startswith(RESERVED_ENTITY_PREFIXES)
