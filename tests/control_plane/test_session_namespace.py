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


def test_pi_session_with_raw_id_does_not_attach_to_herdr_mission():
    # Case 1: a herdr MSN-2 exists and a pi session uses the same raw id.
    # The pi session must not attach to the herdr mission, and the session
    # record must still exist.
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-2",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission MSN-2"},
                "source": "herdr"})
    svc.ingest({"entity_type": "session", "entity_id": "sess-pi2",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "mission_id": "MSN-2"},
                "source": "pi"})
    herdr = repo.get_mission("MSN-2")
    assert herdr["source"] == "herdr"
    assert "sess-pi2" not in herdr["sessions"]
    assert repo.get_session("sess-pi2") is not None


def test_set_mission_stage_pi_raises_when_no_pi_mission():
    # Case 2: stage transition for a pi source whose raw id only matches a
    # herdr mission fails closed with KeyError and leaves the herdr mission
    # unchanged.
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-2",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission MSN-2"},
                "source": "herdr"})
    try:
        repo.set_mission_stage("MSN-2", "review", source="pi")
        assert False, "expected KeyError"
    except KeyError as exc:
        assert exc.args[0] == "MSN-2"
    herdr = repo.get_mission("MSN-2")
    assert herdr["stage"] == "active"


def test_pi_session_attaches_to_pi_mission():
    # Case 3 (positive): a pi:MSN-1 mission exists and a pi session with raw
    # mission_id MSN-1 attaches to pi:MSN-1.
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "pi mission"},
                "source": "pi"})
    svc.ingest({"entity_type": "session", "entity_id": "sess-pi1",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "mission_id": "MSN-1"},
                "source": "pi"})
    pi = repo.get_mission("pi:MSN-1")
    assert "sess-pi1" in pi["sessions"]


def test_pi_session_attaches_by_full_namespaced_key():
    # Case 4: a pi session whose mission_id already carries the source prefix
    # (pi:MSN-1) attaches to pi:MSN-1 even when a raw MSN-1 also exists.
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission"},
                "source": "herdr"})
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "pi mission"},
                "source": "pi"})
    svc.ingest({"entity_type": "session", "entity_id": "sess-pi-key",
                "event_type": "session.started",
                "payload": {"host_id": "bender",
                             "mission_id": "pi:MSN-1"},
                "source": "pi"})
    pi = repo.get_mission("pi:MSN-1")
    herdr = repo.get_mission("MSN-1")
    assert "sess-pi-key" in pi["sessions"]
    assert "sess-pi-key" not in herdr["sessions"]


def test_herdr_session_unstored_mission_is_recorded_without_attach():
    # Case 5 (legacy): a herdr session for a mission that is not yet present
    # records the session without raising and does not attach it anywhere.
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "session", "entity_id": "sess-herdr9",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "mission_id": "MSN-9"},
                "source": "herdr"})
    assert repo.get_session("sess-herdr9") is not None
    assert repo.get_mission("MSN-9") is None


def test_pi_session_unstored_mission_is_recorded_without_attach():
    # Case 6: a pi session whose raw id only matches a herdr mission (and no
    # pi mission exists) records the session without attaching it to the
    # herdr mission.
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "herdr-only-id",
                "event_type": "mission.started",
                "payload": {"title": "herdr mission"},
                "source": "herdr"})
    svc.ingest({"entity_type": "session", "entity_id": "sess-pi-orphan",
                "event_type": "session.started",
                "payload": {"host_id": "bender",
                             "mission_id": "herdr-only-id"},
                "source": "pi"})
    assert repo.get_session("sess-pi-orphan") is not None
    herdr = repo.get_mission("herdr-only-id")
    assert herdr["source"] == "herdr"
    assert "sess-pi-orphan" not in herdr["sessions"]
