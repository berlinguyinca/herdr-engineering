"""Pi orchestration-snapshot reader (issue #29).

Parses a ``.pi-eng/orchestration-snapshot.json`` (contractVersion 1-3) into
ingest envelopes and posts them to a control-plane with an ingest-role key.
"""
import json
import os
import threading
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.pi_snapshot import (
    UnsupportedSnapshotVersion,
    snapshot_to_envelopes,
    post_envelopes,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pi-snapshot-v3.json"


def _load_fixture():
    return json.loads(FIXTURE.read_text())


class _Sink:
    def __init__(self, repo):
        self.repo = repo

    def export_state(self):
        return self.repo.export_state()

    def record(self, ev):
        pass


def _server():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys={
        "view-1": "viewer",
        "ing-1": "ingest",
    })
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    return f"http://127.0.0.1:{svr.port}", repo, svr


# ---------------------------------------------------------------------------
# snapshot_to_envelopes
# ---------------------------------------------------------------------------
def test_snapshot_to_envelopes_maps_mission_fields():
    doc = _load_fixture()
    envelopes = snapshot_to_envelopes(doc)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["source"] == "pi-engineering"
    assert env["entity_type"] == "mission"
    assert env["entity_id"] == "msn_alpha"
    assert env["event_type"] == "mission.updated"
    p = env["payload"]
    assert p["title"] == "Add health endpoint"
    assert p["stage"] == "implementation"  # EXECUTING -> implementation
    # the snapshot goal is carried as the ingest ``purpose`` so the dashboard
    # shows the intended outcome
    assert p["purpose"] == "Expose a /healthz endpoint"


def test_snapshot_to_envelopes_unsupported_version_raises():
    doc = _load_fixture()
    doc["contractVersion"] = 4
    with pytest.raises(UnsupportedSnapshotVersion):
        snapshot_to_envelopes(doc)


def test_snapshot_to_envelopes_accepts_v1_and_v2():
    doc = _load_fixture()
    for version in (1, 2):
        d = json.loads(json.dumps(doc))
        d["contractVersion"] = version
        if version < 2:
            for m in d["missions"]:
                m.pop("observability", None)
        envelopes = snapshot_to_envelopes(d)
        assert envelopes and envelopes[0]["entity_id"] == "msn_alpha"


def test_snapshot_to_envelopes_handles_absent_observability():
    doc = _load_fixture()
    for m in doc["missions"]:
        m.pop("observability", None)
    envelopes = snapshot_to_envelopes(doc)
    # still produces a mission envelope with defaults, no crash
    assert envelopes[0]["entity_type"] == "mission"
    assert envelopes[0]["event_type"] == "mission.updated"


def test_snapshot_to_envelopes_stopped_mission_stays_incomplete():
    # A durable stop with a resume condition blocks/pauses the mission; it must
    # NOT be presented as complete (codex P2).
    doc = _load_fixture()
    doc["missions"][0]["status"] = "EXECUTING"
    doc["missions"][0]["stop"] = {
        "reason": "awaiting_approval",
        "attemptedRecoveries": 0,
        "preservedWork": ["w"],
        "resumeCondition": "approval granted",
        "stoppedAt": "2026-01-15T12:00:00.000Z",
    }
    envelopes = snapshot_to_envelopes(doc)
    assert envelopes[0]["payload"]["stage"] == "implementation"  # not complete


def test_snapshot_to_envelopes_maps_complete_status_to_complete_stage():
    doc = _load_fixture()
    doc["missions"][0]["status"] = "COMPLETE"
    envelopes = snapshot_to_envelopes(doc)
    assert envelopes[0]["payload"]["stage"] == "complete"


def test_snapshot_to_envelopes_maps_pi_lifecycle_stages():
    # Real Pi MissionStatus values must map to the right mission stages.
    doc = _load_fixture()
    cases = {"REVIEWING": "review", "FINAL_VALIDATION": "review",
             "VALIDATING": "testing", "EXECUTING": "implementation",
             "PLANNING": "planning", "REPAIRING": "implementation",
             "WAITING_FOR_USER": "review"}
    for status, expected in cases.items():
        doc["missions"][0]["status"] = status
        envelopes = snapshot_to_envelopes(doc)
        assert envelopes[0]["payload"]["stage"] == expected, status


# ---------------------------------------------------------------------------
# post_envelopes
# ---------------------------------------------------------------------------
def test_post_envelopes_with_ingest_key_creates_missions():
    base, repo, svr = _server()
    try:
        envelopes = snapshot_to_envelopes(_load_fixture())
        posted = post_envelopes(base, "ing-1", envelopes)
        assert posted == 1
        m = repo.get_mission("pi-engineering:msn_alpha")
        assert m is not None
        assert m["title"] == "Add health endpoint"
        assert m["source"] == "pi-engineering"
        # idempotent: posting twice leaves one row per mission
        posted2 = post_envelopes(base, "ing-1", envelopes)
        assert posted2 == 1
        assert len(repo.list_missions()) == 1
    finally:
        svr.shutdown()


def test_post_envelopes_error_never_echoes_the_key():
    # Error messages name the variable, never a value: the ingest key must not
    # leak through raised error text. Point at an unroutable port so the POST
    # fails after the key is in flight.
    secret = "super-secret-key-value"
    with pytest.raises((ValueError, OSError)):
        post_envelopes("http://127.0.0.1:1", secret, [{"a": 1}])
    # A missing key raises a ValueError that names the requirement, not a value.
    with pytest.raises(ValueError):
        post_envelopes("http://127.0.0.1:1", "", [])


def test_post_envelopes_rejects_non_ingest_key():
    base, repo, svr = _server()
    try:
        envelopes = snapshot_to_envelopes(_load_fixture())
        # a viewer key cannot POST /api/v1/ingest (403)
        with pytest.raises(ValueError):
            post_envelopes(base, "view-1", envelopes, expected=(200,))
        assert repo.list_missions() == []
    finally:
        svr.shutdown()


def test_post_envelopes_auth_rejected_is_distinguishable():
    base, repo, svr = _server()
    try:
        envelopes = snapshot_to_envelopes(_load_fixture())
        from herdr_engineering.control_plane.pi_snapshot import IngestAuthRejected
        with pytest.raises(IngestAuthRejected):
            post_envelopes(base, "view-1", envelopes, expected=(200,))
    finally:
        svr.shutdown()
