"""Mission storage is namespaced by source so pi-engineering and herdr missions
with the same raw entity_id do not collide."""
import json
import subprocess
from pathlib import Path

import pytest

from herdr_engineering.control_plane import bridge
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


@pytest.mark.parametrize("events", [
    ["workspace.created", "pane.agent_detected", "workspace.focused"],
    ["pane.agent_detected", "workspace.focused"],
])
def test_feed_plugin_workspace_events_ingest_and_replay(events):
    """Exercise the real JS producer and Python ingest/repository boundary."""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", """
import { readFileSync } from 'node:fs';
import { mapEvent } from './deploy/herdr-cp-feed-plugin/emit.js';
const events = JSON.parse(readFileSync(0, 'utf8'));
console.log(JSON.stringify(events.map(event => mapEvent({
  HERDR_PLUGIN_EVENT: event, HERDR_WORKSPACE_ID: 'wK',
  HERDR_PANE_ID: 'wK:p1', HERDR_SESSION_NAME: 'eng',
}, { host: 'feed-host', info: { wK: {
  title: event === 'workspace.focused' ? 'Focused workspace' : 'Created workspace',
  cwd: '/work/project',
} } }))));
"""], input=json.dumps(events), text=True, capture_output=True, check=True,
        cwd=Path(__file__).resolve().parents[2], timeout=10,
    )
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    for source in ("herdr", "pi"):
        svc.ingest({"entity_type": "mission", "entity_id": "wK",
                    "event_type": "mission.started", "source": source,
                    "payload": {"title": source + " original"}})
    session_id = "herdr:feed-host:eng:wK%3Ap1"
    for event, envelopes in zip(events, json.loads(result.stdout), strict=True):
        for envelope in envelopes:
            svc.ingest(envelope)
        if event == "workspace.created":
            assert repo.get_mission("herdr-ws:wK")["title"] == "Created workspace"

    def assert_workspace(missions, sessions):
        workspace = missions["herdr-ws:wK"]
        assert workspace["source"] == "herdr"
        assert workspace["mission_id"] == workspace["mission_key"] == "herdr-ws:wK"
        assert workspace["title"] == "Focused workspace"
        assert workspace["cwd"] == "/work/project"
        assert workspace["sessions"] == [session_id]
        session = sessions[session_id]
        assert session["mission_id"] == session["mission_key"] == "herdr-ws:wK"
        assert session["mission_source"] == "herdr"
        for key, source in (("wK", "herdr"), ("pi:wK", "pi")):
            assert missions[key]["title"] == source + " original"
            assert missions[key]["sessions"] == []

    state = repo.export_state()
    assert_workspace(state["missions"], state["sessions"])
    missions, sessions, *_ = bridge._reconstruct_from_events(state["events"])
    assert_workspace(missions, sessions)


def test_v2_workspace_mission_accepts_create_and_duplicate():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    body = {"schema_version": 2, "event_id": "workspace-created", "source_sequence": 0,
            "source": "herdr", "entity_type": "mission", "entity_id": "herdr-ws:wK",
            "event_type": "mission.started", "payload": {"title": "workspace"}}
    principal = {"kind": "agent", "id": "feed", "authenticated_by": "api_key"}
    assert svc.ingest(body, principal)["envelope"]["duplicate"] is False
    assert svc.ingest(body, principal)["envelope"]["duplicate"] is True
    assert repo.get_mission("herdr-ws:wK")["source"] == "herdr"


@pytest.mark.parametrize("source,mission_id", [
    ("herdr", "pi:wK"), ("pi", "pi:wK"), ("pi", "herdr-ws:wK"),
    ("other", "herdr-ws:wK"), ("herdr", "herdr-ws:"),
    ("herdr", "herdr-ws:pi:wK"), ("herdr", "herdr-ws:wK:extra"),
    ("herdr-ws", "wK"),
])
@pytest.mark.parametrize("route", ["ingest", "dispatch", "repo"])
def test_workspace_exception_does_not_open_other_mission_namespaces(source, mission_id, route):
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    repo.upsert_mission("wK", title="original", source="pi")
    before = repo.export_state()
    with pytest.raises(ValueError):
        if route == "ingest":
            svc.ingest({"schema_version": 2, "event_id": "invalid-workspace",
                        "source_sequence": 0, "source": source, "entity_type": "mission",
                        "entity_id": mission_id, "event_type": "mission.started",
                        "payload": {"title": "spoofed"}},
                       {"kind": "agent", "id": "feed", "authenticated_by": "api_key"})
        elif route == "dispatch":
            svc._dispatch("mission", mission_id, "mission.started", {"title": "spoofed"}, source)
        else:
            repo.upsert_mission(mission_id, title="spoofed", source=source)
    assert repo.export_state() == before
    assert repo.ledger.source_states() == []


def test_pi_session_cannot_claim_workspace_namespace():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    with pytest.raises(ValueError, match="mission identity conflicts with its source"):
        svc.ingest({"entity_type": "session", "entity_id": "pi-session",
                    "event_type": "session.started", "source": "pi",
                    "payload": {"mission_id": "herdr-ws:wK"}})
    assert repo.get_session("pi-session") is None
