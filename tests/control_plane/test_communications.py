"""Real ingestion, repository and authenticated HTTP boundaries."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.communications import Outbox, sync
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.communications import communication_page, record_observation
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def observation(tmp_path, operation="native_one", status="delivered"):
    box = Outbox(tmp_path / "outbox.sqlite")
    box.put(
        operation,
        status,
        {
            "host_id": "mac",
            "text": "please clean Bearer hiddenvalue",
            "sender": {"pane_id": "w1:p1"},
            "recipient": {"pane_id": "w1:p2"},
            "route": [],
            "channel": "herdr.agent.prompt",
            "evidence_kind": "host_observation",
            "sender_origin": "caller_environment",
        },
    )
    return box.pending()[0]


def envelope(payload):
    return {
        "entity_type": "communication",
        "entity_id": payload["operation_id"],
        "event_type": "communication." + payload["status"],
        "payload": payload,
    }


def test_ingestion_replay_is_idempotent_and_does_not_mutate_sessions(tmp_path):
    repo = ControlPlaneRepo()
    sid = repo.upsert_session("existing", host_id="mac")["session_id"]
    before = repo.get_session(sid)
    payload = observation(tmp_path)
    service = IngestService(repo)
    service.ingest(envelope(payload))
    service.ingest(envelope(payload))
    assert len(repo.stream("communication", payload["operation_id"])) == 1
    assert repo.get_session(sid) == before
    assert "hiddenvalue" not in json.dumps(communication_page(repo))


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_kind", "server_receipt"),
        ("status", "completed"),
        ("text", "a" * 17000),
        ("reported_at", float("inf")),
        ("reported_at", 1e300),
        ("sender", {"unexpected": "payload"}),
    ],
)
def test_invalid_observation_is_rejected_before_recording(tmp_path, field, value):
    repo = ControlPlaneRepo()
    payload = observation(tmp_path)
    payload[field] = value
    with pytest.raises(ValueError):
        IngestService(repo).ingest(envelope(payload))
    assert not repo.entity_ids("communication")


def test_native_identity_links_both_sessions_and_never_focused_pane(tmp_path):
    repo = ControlPlaneRepo()
    first = repo.upsert_session("one", host_id="mac", pane_id="w1:p1", source="herdr")
    second = repo.upsert_session("two", host_id="mac", pane_id="w1:p2", source="herdr")
    record_observation(repo, observation(tmp_path))
    row = communication_page(repo)["communications"][0]
    assert row["sender"]["session_id"] == first["session_id"]
    assert row["recipient"]["session_id"] == second["session_id"]
    assert row["evidence_kind"] == "host_observation"


def test_conversation_identity_wins_when_a_pane_has_been_reused(tmp_path):
    repo = ControlPlaneRepo()
    old = {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "old"}
    new = {**old, "value": "new"}
    repo.upsert_session("old", host_id="mac", pane_id="w1:p2", agent_session=old)
    repo.upsert_session("new", host_id="mac", pane_id="w1:p2", agent_session=new)
    payload = observation(tmp_path)
    record_observation(repo, {**payload, "recipient": {"pane_id": "w1:p2", "agent_session": new}})
    assert communication_page(repo)["communications"][0]["recipient"]["session_id"] == "new"


def test_server_mirror_uses_actual_receipt_recipient_after_replacement():
    from herdr_engineering.control_plane.communications import record_command

    repo = ControlPlaneRepo()
    old = {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "old"}
    new = {**old, "value": "new"}
    previous = repo.upsert_session("old", host_id="mac", pane_id="w1:p2", agent_session=old)
    repo.upsert_session("new", host_id="mac", pane_id="w1:p2", agent_session=new)
    record_command(
        repo,
        previous,
        {
            "operation_id": "actual-recipient",
            "status": "delivered",
            "text": "hello",
            "delivery": {
                "receipt": {"result": {"agent": {"pane_id": "w1:p2", "agent_session": new}}}
            },
        },
    )
    assert communication_page(repo)["communications"][0]["recipient"]["session_id"] == "new"


def test_server_receipt_takes_precedence_and_operations_stay_distinct(tmp_path):
    repo = ControlPlaneRepo()
    payload = observation(tmp_path)
    record_observation(repo, payload)
    record_observation(
        repo, {**payload, "evidence_kind": "server_receipt", "status": "failed"}, trusted=True
    )
    record_observation(repo, {**payload, "operation_id": "native_two"})
    rows = communication_page(repo)["communications"]
    assert len(rows) == 2
    assert next(row for row in rows if row["operation_id"] == "native_one")["status"] == "failed"


def test_dashboard_submission_is_in_the_same_history_without_delivery():
    repo = ControlPlaneRepo()
    sid = repo.create_session()
    repo.send_message(sid, "Please inspect cleanup", operation_id="dashboard-one")
    row = communication_page(repo)["communications"][0]
    assert row["operation_id"] == "dashboard-one"
    assert row["status"] == "queued"
    assert row["evidence_kind"] == "server_receipt"
    assert row["text"] == "Please inspect cleanup"


def test_history_pagination_does_not_repeat_phases(tmp_path):
    repo = ControlPlaneRepo()
    payload = observation(tmp_path)
    for n in range(5):
        record_observation(repo, {**payload, "operation_id": f"op{n}", "status": "queued"})
        record_observation(repo, {**payload, "operation_id": f"op{n}"})
    first = communication_page(repo, limit=2)
    second = communication_page(repo, limit=2, before=first["next_cursor"])
    assert len(first["communications"]) == len(second["communications"]) == 2
    assert {row["operation_id"] for row in first["communications"]}.isdisjoint(
        row["operation_id"] for row in second["communications"]
    )


def test_authenticated_real_http_offline_queue_replay(tmp_path):
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, api_key="test-ingestion-key")
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    key = tmp_path / "key"
    key.write_text("test-ingestion-key")
    payload = observation(tmp_path)
    box = Outbox(tmp_path / "outbox.sqlite")
    try:
        url = f"http://127.0.0.1:{server.port}"
        with pytest.raises(urllib.error.HTTPError) as refusal:
            urllib.request.urlopen(url + "/api/v1/communications")
        assert refusal.value.code == 401
        assert sync(box, url, key, max_seconds=0) == 0
        assert len(box.pending()) == 1
        assert sync(box, url, key) == 1
        assert box.pending() == []
        request = urllib.request.Request(
            url + "/api/v1/communications", headers={"X-API-Key": key.read_text()}
        )
        with urllib.request.urlopen(request) as response:
            body = json.load(response)
        assert body["communications"][0]["operation_id"] == payload["operation_id"]
        assert body["communications"][0]["status"] == "delivered"
        from herdr_engineering.communications import main

        config = tmp_path / "config.json"
        config.write_text(
            json.dumps(
                {"native": "/not/used", "outbox": str(box.path), "url": url, "key_file": str(key)}
            )
        )
        box.put("second", "queued", {**payload, "text": "another observation"})
        assert main(["sync", "--config", str(config)]) == 0
        assert not box.pending()
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", ""),
        ("record_id", 5),
        ("truncated", "true"),
        ("route", "--machine mac"),
        ("recipient", {"agent_session": "not an object"}),
        ("recipient", {"pane_id": 5}),
        ("recipient", {"name": "x" * 4100}),
    ],
)
def test_invalid_identity_and_bounds_are_refused(tmp_path, field, value):
    payload = observation(tmp_path)
    payload[field] = value
    with pytest.raises(ValueError):
        IngestService(ControlPlaneRepo()).ingest(envelope(payload))


def test_remote_or_ambiguous_identity_never_links_to_local_session(tmp_path):
    repo = ControlPlaneRepo()
    repo.upsert_session("local1", host_id="mac", pane_id="w1:p2", source="herdr")
    repo.upsert_session("local2", host_id="mac", pane_id="w1:p2", source="herdr")
    payload = observation(tmp_path)
    record_observation(repo, payload)
    assert "session_id" not in communication_page(repo)["communications"][0]["recipient"]
    record_observation(repo, {**payload, "operation_id": "remote", "route": ["--machine=other"]})
    assert "session_id" not in communication_page(repo)["communications"][0]["recipient"]


def test_invalid_cursor_and_limit_are_refused():
    from herdr_engineering.control_plane.communications import decode_before

    for cursor in ("bad!", "a" * 513, "W10", "WyJub3QtYS10aW1lIiwib25lIl0"):
        with pytest.raises(ValueError):
            decode_before(cursor)
    with pytest.raises(ValueError):
        communication_page(ControlPlaneRepo(), limit=101)
