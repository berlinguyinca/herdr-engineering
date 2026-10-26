"""Pi snapshot adapter: golden contract, real HTTP ingestion, and safe failures.

Golden bytes copied from pi-engineering-web's
src/browser/fixtures/mission-snapshot-v3.golden.json (blob 08701b4).
"""
import copy
import json
import threading
import traceback
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering.control_plane import pi_snapshot as adapter
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

INGEST_KEY = "snapshot-ingest-secret"


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parents[1] / "fixtures/pi-snapshot-v3.json").read_text())


@pytest.fixture
def server():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, api_keys={INGEST_KEY: "ingest", "operator": "operator", "viewer": "viewer"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        yield f"http://127.0.0.1:{server.port}", repo
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_golden_mapping_is_pure(snapshot):
    before = copy.deepcopy(snapshot)
    envelopes = adapter.snapshot_to_envelopes(snapshot)
    assert envelopes == adapter.snapshot_to_envelopes(snapshot)
    assert snapshot == before
    assert len(envelopes) == 2
    nulls, full = envelopes
    assert nulls["source"] == "pi-engineering"
    assert nulls["entity_type"] == "mission"
    assert nulls["entity_id"] == "MSN-golden-nulls"
    assert nulls["event_type"] == "mission.updated"
    assert nulls["occurred_at"] == "2026-10-14T12:00:00.000Z"
    assert nulls["payload"]["status"] == "NEW"
    assert nulls["payload"]["title"] == "Nullable fields are null"
    assert nulls["payload"]["purpose"] == "Pin the null form of every nullable field"
    assert nulls["payload"]["progress"] == 40
    assert nulls["payload"]["last_progress_at"] is None
    assert "last_heartbeat_at" not in nulls["payload"]
    assert full["payload"]["last_progress_at"] == "2026-10-14T11:59:00.000Z"
    assert full["payload"]["next_action"] == "verify"
    assert full["payload"]["recovery_action"] == "run_task"
    assert full["payload"]["health"] == "active"


def test_heartbeat_never_fabricates_progress(snapshot):
    obs = snapshot["missions"][0]["observability"]
    obs["lastHeartbeatAt"] = "2026-10-14T12:01:00.000Z"
    obs["progress"]["approximatePercent"] = 0
    payload = adapter.snapshot_to_envelopes(snapshot)[0]["payload"]
    assert payload["last_heartbeat_at"] == "2026-10-14T12:01:00.000Z"
    assert payload["last_progress_at"] is None
    assert payload["progress"] == 0


@pytest.mark.parametrize("version", [1, 2, 3])
def test_supported_versions_with_absent_observability(version):
    doc = {"contractVersion": version, "missions": [{"id": "old", "status": "RUNNING"}]}
    envelope, = adapter.snapshot_to_envelopes(doc)
    assert envelope["entity_id"] == "old"
    assert envelope["payload"]["status"] == "RUNNING"
    for key in ("last_heartbeat_at", "last_progress_at", "progress", "recovery_action"):
        assert key not in envelope["payload"]
    assert adapter.snapshot_to_envelopes({"contractVersion": version, "missions": []}) == []


@pytest.mark.parametrize("version", [4, 0, -1, None, "3", True, 3.0, [], {}])
def test_unsupported_versions(version):
    with pytest.raises(adapter.UnsupportedSnapshotVersion):
        adapter.snapshot_to_envelopes({"contractVersion": version, "missions": []})


def test_posts_idempotently_with_ingest_role(snapshot, server):
    base, _ = server
    envelopes = adapter.snapshot_to_envelopes(snapshot)
    assert adapter.post_envelopes(base + "/", INGEST_KEY, envelopes) == 2
    assert adapter.post_envelopes(base, INGEST_KEY, envelopes) == 2
    request = urllib.request.Request(base + "/api/v1/missions", headers={"X-API-Key": "operator"})
    with urllib.request.urlopen(request, timeout=5) as response:
        missions = json.load(response)["missions"]
    assert {m["mission_key"] for m in missions} == {"pi:MSN-golden-nulls", "pi:MSN-golden-full"}
    assert len(missions) == 2
    assert {m["status"] for m in missions} == {"NEW"}


def test_environment_ingest_key(snapshot, server, monkeypatch):
    base, repo = server
    monkeypatch.setenv("HERDR_CP_INGEST_KEY", INGEST_KEY)
    assert adapter.post_envelopes(base, None, adapter.snapshot_to_envelopes(snapshot)) == 2
    assert len(repo.list_missions()) == 2


@pytest.mark.parametrize("key", ["wrong-secret", "viewer", "bad\nsecret", "unicode-\u2603-secret"])
def test_failed_posts_never_expose_keys(snapshot, server, key, capsys, caplog):
    base, repo = server
    with pytest.raises(RuntimeError) as error:
        adapter.post_envelopes(base, key, adapter.snapshot_to_envelopes(snapshot))
    output = capsys.readouterr()
    diagnostic = "".join(traceback.format_exception(error.value)) + output.out + output.err + caplog.text
    assert key not in diagnostic
    assert "HERDR_CP_INGEST_KEY" in str(error.value)
    assert repo.list_missions() == []


def test_missing_key_fails_closed(snapshot, server, monkeypatch):
    base, repo = server
    monkeypatch.delenv("HERDR_CP_INGEST_KEY", raising=False)
    with pytest.raises(ValueError, match="HERDR_CP_INGEST_KEY"):
        adapter.post_envelopes(base, None, adapter.snapshot_to_envelopes(snapshot))
    assert repo.list_missions() == []


def test_invalid_url_is_sanitized(snapshot, capsys, caplog):
    with pytest.raises(ValueError) as error:
        adapter.post_envelopes("invalid:" + INGEST_KEY, INGEST_KEY, adapter.snapshot_to_envelopes(snapshot))
    output = capsys.readouterr()
    assert INGEST_KEY not in str(error.value) + output.out + output.err + caplog.text
