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
