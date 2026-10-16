"""Sessions attach to missions by the mission's source-namespaced key.

A session for a pi-sourced mission (raw mission_id ``MSN-1``) must link to the
``pi:MSN-1`` mission, not the herdr ``MSN-1`` record that happens to share the
same raw id. herdr sessions are unchanged, and stage transitions resolve the
pi mission by its namespaced key."""
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def make_svc(repo=None):
    return IngestService(repo or ControlPlaneRepo())


def test_pi_session_attaches_to_pi_mission_not_herdr():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    # herdr mission with raw id MSN-1
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission"},
                "source": "herdr"})
    # pi-engineering mission with the same raw id
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "pi mission"},
                "source": "pi"})
    # a session sourced from pi with raw mission_id MSN-1
    svc.ingest({"entity_type": "session", "entity_id": "sess-pi",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "mission_id": "MSN-1"},
                "source": "pi"})
    herdr = repo.get_mission("MSN-1")
    pi = repo.get_mission("pi:MSN-1")
    assert herdr["source"] == "herdr"
    assert pi["source"] == "pi"
    assert "sess-pi" in pi["sessions"]
    assert "sess-pi" not in herdr["sessions"]


def test_herdr_session_still_attaches_to_herdr_mission():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission"},
                "source": "herdr"})
    svc.ingest({"entity_type": "session", "entity_id": "sess-herdr",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "mission_id": "MSN-1"},
                "source": "herdr"})
    m = repo.get_mission("MSN-1")
    assert m["source"] == "herdr"
    assert "sess-herdr" in m["sessions"]


def test_set_mission_stage_works_on_pi_mission():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "pi mission"},
                "source": "pi"})
    # resolve by the raw id under source pi
    repo.set_mission_stage("MSN-1", "testing", source="pi")
    assert repo.get_mission("pi:MSN-1")["stage"] == "testing"
    # and by the full namespaced key with the default source
    repo.set_mission_stage("pi:MSN-1", "review")
    assert repo.get_mission("pi:MSN-1")["stage"] == "review"


def test_herdr_set_mission_stage_unchanged():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission"},
                "source": "herdr"})
    repo.set_mission_stage("MSN-1", "review")
    assert repo.get_mission("MSN-1")["stage"] == "review"
