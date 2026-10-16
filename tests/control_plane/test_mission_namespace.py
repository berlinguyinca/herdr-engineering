"""Mission storage is namespaced by source so pi-engineering and herdr missions
with the same raw entity_id do not collide."""
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def make_svc(repo=None):
    return IngestService(repo or ControlPlaneRepo())


def test_mission_namespace_by_source():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    # herdr mission (source absent -> defaults to "herdr")
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission"},
                "source": "herdr"})
    # pi-engineering mission with the same raw entity_id
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "pi mission"},
                "source": "pi"})
    missions = repo.list_missions()
    assert len(missions) == 2
    by_key = {m["mission_key"]: m for m in missions}
    assert by_key["MSN-1"]["source"] == "herdr"
    assert by_key["MSN-1"]["mission_id"] == "MSN-1"
    assert by_key["pi:MSN-1"]["source"] == "pi"
    assert by_key["pi:MSN-1"]["mission_id"] == "MSN-1"
    # get_mission("MSN-1") resolves the herdr record
    assert repo.get_mission("MSN-1")["source"] == "herdr"
    # get_mission("pi:MSN-1") resolves the pi record by exact key
    assert repo.get_mission("pi:MSN-1")["source"] == "pi"


def test_mission_entity_id_with_colon_rejected_and_existing_untouched():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    # pre-existing pi:MSN-1 record
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "original title"},
                "source": "pi"})
    before = repo.get_mission("pi:MSN-1")
    # a mission with entity_id containing ':' must be rejected
    try:
        svc.ingest({"entity_type": "mission", "entity_id": "pi:MSN-1",
                    "event_type": "mission.started",
                    "payload": {"title": "spoofed"},
                    "source": "herdr"})
        raise AssertionError("expected ValueError for colon in entity_id")
    except ValueError as exc:
        assert str(exc) == "invalid mission id"
    # existing pi:MSN-1 record is untouched
    after = repo.get_mission("pi:MSN-1")
    assert after["title"] == "original title"
    assert after["source"] == "pi"
    assert after["updated_at"] == before["updated_at"]


def test_source_with_colon_rejected():
    svc = make_svc()
    try:
        svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                    "event_type": "mission.started",
                    "payload": {"title": "x"},
                    "source": "a:b"})
        raise AssertionError("expected ValueError for invalid source")
    except ValueError as exc:
        assert str(exc) == "invalid source"


def test_source_normalized_case_and_whitespace():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "pi mission"},
                "source": "PI "})
    by_key = {m["mission_key"]: m for m in repo.list_missions()}
    assert "pi:MSN-1" in by_key
    assert by_key["pi:MSN-1"]["source"] == "pi"
    assert repo.get_mission("pi:MSN-1")["source"] == "pi"


def test_non_string_source_rejected():
    svc = make_svc()
    try:
        svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                    "event_type": "mission.started",
                    "payload": {"title": "x"},
                    "source": 123})
        raise AssertionError("expected ValueError for invalid source")
    except ValueError as exc:
        assert str(exc) == "invalid source"
