# ruff: noqa: F811 (pytest fixtures are imported by name)
"""Local attachments coexist with upstream mentions and durable retry identity."""
import json

import pytest

from herdr_engineering.control_plane.collab import CollabService, _digest
from tests.control_plane.test_collab_tasks import code, svc  # noqa: F401
from tests.test_collab_mentions import w  # noqa: F401


def test_attachments_preserve_server_mentions_and_replay(w):
    body = {"text": "@bob review this", "idempotency_key": "attachment",
            "artifact_ids": ["artifact-one"], "mentions": [{"id": "mallory"}]}
    status, out = w.s.post_message(w.alice, w.room, body)
    assert status == 201
    assert out["event"]["mentions"] == [{"kind": "person", "id": "bob", "name": "bob"}]
    assert out["event"]["artifact_ids"] == ["artifact-one"]
    restored = CollabService(w.s.auth, sink=w.s._sink, now=w.s.clock)
    restored.hydrate(w.s._sink.load())
    status, replay = restored.post_message(w.alice, w.room, body)
    assert status == 200
    assert replay["replayed"] is True
    assert replay["event"]["event_id"] == out["event"]["event_id"]
    assert replay["event"]["mentions"] == out["event"]["mentions"]
    assert replay["event"]["artifact_ids"] == ["artifact-one"]
    assert code(restored.post_message, w.alice, w.room,
                {**body, "artifact_ids": ["artifact-two"]}) == 409


@pytest.mark.parametrize("artifact_ids", [None, "artifact-one", [""], [" "], [1], ["a"] * 21])
def test_invalid_attachment_ids_fail_without_posting(w, artifact_ids):
    assert code(w.s.post_message, w.alice, w.room,
                {"text": "review", "idempotency_key": "bad", "artifact_ids": artifact_ids}) == 400
    assert not [event for event in w.s.read_events(w.alice, w.room, 0, 200)["events"]
                if event["event_type"] == "message"]


@pytest.mark.parametrize("legacy_local", [False, True])
def test_attachment_free_retries_survive_both_historical_fingerprints(w, legacy_local):
    body = {"text": "review", "idempotency_key": "historical"}
    _, out = w.s.post_message(w.alice, w.room, body)
    events = w.s._sink.load()
    message = next(event for event in events if event["event_type"] == "message")
    values = [w.room, "review", None] + ([[]] if legacy_local else [])
    message["payload"]["fingerprint"] = _digest(json.dumps(values))
    restored = CollabService(w.s.auth, now=w.s.clock)
    restored.hydrate(events)
    status, replay = restored.post_message(w.alice, w.room, body)
    assert status == 200
    assert replay["replayed"] is True
    assert replay["event"]["event_id"] == out["event"]["event_id"]
