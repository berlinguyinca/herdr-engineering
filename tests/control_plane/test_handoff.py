import copy
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.handoffs import HandoffRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"X-API-Key": "operator", "Content-Type": "application/json"})
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    return response.status, payload


def server():
    instance = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0,
                                  api_keys={"operator": "operator", "viewer": "viewer"})
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    instance.wait_until_ready()
    return instance, thread


def test_handoff_queue_claim_and_tree_are_idempotent():
    instance, thread = server()
    try:
        body = {"handoff_id": "handoff_123", "goal": "Continue work", "source_harness": "pi",
                "target_harness": "claude", "model_hint": "sonnet", "remaining": ["test"],
                "artifact_refs": [{"artifact_id": "art_1", "version_id": "ver_1"}],
                "source_agent_id": "agent-a", "target_agent_id": "agent-b"}
        status, created = call(instance.port, "POST", "/api/v1/handoffs", body)
        assert status == 201
        assert created["state"] == "queued"
        status, replay = call(instance.port, "POST", "/api/v1/handoffs", body)
        assert status == 200
        assert replay["handoff_id"] == created["handoff_id"]
        status, conflict = call(instance.port, "POST", "/api/v1/handoffs",
                                {**body, "goal": "different"})
        assert status == 400
        assert "different content" in conflict["error"]

        status, claimed = call(instance.port, "POST", "/api/v1/handoffs/handoff_123/claim",
                               {"actor": "spoofed-agent"})
        assert status == 400
        status, claimed = call(instance.port, "POST", "/api/v1/handoffs/handoff_123/claim", {})
        assert status == 200
        assert claimed["state"] == "claimed"
        actor = claimed["history"][-1]["actor"]
        assert actor != "spoofed-agent"
        assert isinstance(actor, dict) and actor.get("authenticated_by") == "api_key"
        status, tree = call(instance.port, "GET", "/api/v1/handoffs/tree")
        assert status == 200
        assert tree["nodes"]
        assert any(edge["handoff_id"] == "handoff_123" for edge in tree["edges"])
    finally:
        instance.shutdown()
        thread.join(timeout=5)


def test_handoff_projection_rehydrates_from_event_history():
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)

    registry = HandoffRegistry(repo)
    row, _ = registry.create({"handoff_id": "handoff_restore", "goal": "restore",
                              "source_harness": "pi", "target_harness": "codex"}, "agent-a")
    registry.transition(row["handoff_id"], "claimed", "agent-b")
    restored = HandoffRegistry(repo)
    assert restored.get(row["handoff_id"]) == registry.get(row["handoff_id"])
    assert [item["state"] for item in restored.get(row["handoff_id"])["history"]] == ["queued", "claimed"]
    # Older in-memory exports could contain a queued payload aliased to the
    # already-transitioned live row. Its history must not be counted twice.
    repo._events[("handoff", row["handoff_id"])][0]["payload"] = registry.get(row["handoff_id"])
    assert HandoffRegistry(repo).get(row["handoff_id"]) == registry.get(row["handoff_id"])


def test_old_handoff_survives_more_than_100000_unrelated_events():
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
    registry = HandoffRegistry(repo)
    actor = {"id": "api-key:1234567890abcdef:operator", "kind": "operator",
             "authenticated_by": "api_key"}
    registry.create({"handoff_id": "old-handoff", "goal": "Keep history",
                     "source_harness": "pi", "target_harness": "codex"}, actor)
    expected = registry.transition("old-handoff", "claimed", actor)
    # Populate the same projection shape produced by durable replay, without
    # record_event's per-write sequence scan making the fixture quadratic.
    with repo._event_lock:
        repo._events[("service", "busy-service")] = [
            {"entity_type": "service", "entity_id": "busy-service", "sequence": i,
             "event_type": "ServiceUpdated", "payload": {"port": 8080}}
            for i in range(100001)]
        repo._event_order.extend(("service", "busy-service", i) for i in range(100001))
    restored = HandoffRegistry(repo)
    assert restored.get("old-handoff") == expected
    assert restored.list(state="claimed") == [expected]
    assert restored.tree()["edges"][0]["handoff_id"] == "old-handoff"


def test_restore_uses_typed_stream_sequences_without_global_history_copy(monkeypatch):
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
    registry = HandoffRegistry(repo)
    actor = {"id": "operator", "kind": "operator", "authenticated_by": "api_key"}
    registry.create({"handoff_id": "retry-handoff", "goal": "Retry",
                     "source_harness": "codex", "target_harness": "pi"}, actor)
    for state in ("claimed", "queued", "claimed"):
        expected = registry.transition("retry-handoff", state, actor)
    # Replay can supply typed streams independently of the activity-feed index.
    repo._events[("handoff", "retry-handoff")].reverse()
    repo._event_order = []

    def forbidden(*args, **kwargs):
        raise AssertionError("handoff restoration must not copy global history")

    monkeypatch.setattr(repo, "recent_events", forbidden)
    monkeypatch.setattr(repo, "export_state", forbidden)
    for _ in range(2):
        restored = HandoffRegistry(repo)
        assert restored.get("retry-handoff") == expected
        assert [item["state"] for item in restored.get("retry-handoff")["history"]] == [
            "queued", "claimed", "queued", "claimed"]
        restored._restore()
        assert restored.get("retry-handoff") == expected


def test_queued_event_payload_is_immutable_after_transition():
    repo = ControlPlaneRepo()
    registry = HandoffRegistry(repo)
    queued, _ = registry.create({"handoff_id": "immutable-handoff", "goal": "Persist",
                                 "source_harness": "pi", "target_harness": "codex"}, "agent-a")
    registry.transition("immutable-handoff", "claimed", "agent-b")
    assert repo.stream("handoff", "immutable-handoff")[0]["payload"] == queued


class HandoffSink:
    def __init__(self):
        self.events = []
        self.fail = False

    def record(self, event):
        self.record_required(event)

    def record_required(self, event):
        if self.fail:
            raise OSError("durable store unavailable")
        self.events.append(copy.deepcopy(event))


def test_failed_durable_create_does_not_publish_and_can_retry():
    sink = HandoffSink()
    sink.fail = True
    repo = ControlPlaneRepo(sink=sink)
    registry = HandoffRegistry(repo)
    body = {"handoff_id": "durable-create", "goal": "Persist before acknowledging",
            "source_harness": "pi", "target_harness": "codex"}
    actor = {"id": "operator", "kind": "operator", "authenticated_by": "api_key"}
    with pytest.raises(OSError, match="durable store unavailable"):
        registry.create(body, actor)
    assert registry.get("durable-create") is None
    assert registry.list() == []
    assert repo.stream("handoff", "durable-create") == []
    assert sink.events == []
    sink.fail = False
    row, duplicate = registry.create(body, actor)
    assert not duplicate
    assert row["created_by"] == actor
    assert len(sink.events) == 1
    assert HandoffRegistry(repo).get("durable-create") == row


def test_failed_durable_transition_leaves_projection_and_history_unchanged():
    sink = HandoffSink()
    repo = ControlPlaneRepo(sink=sink, now=lambda: 1_800_000_000)
    registry = HandoffRegistry(repo)
    actor = {"id": "operator", "kind": "operator", "authenticated_by": "api_key"}
    queued, _ = registry.create({"handoff_id": "durable-transition", "goal": "Keep state",
                                 "source_harness": "pi", "target_harness": "codex"}, actor)
    sink.fail = True
    with pytest.raises(OSError, match="durable store unavailable"):
        registry.transition("durable-transition", "claimed", actor)
    assert registry.get("durable-transition") == queued
    assert len(sink.events) == len(repo.stream("handoff", "durable-transition")) == 1
    sink.fail = False
    claimed = registry.transition("durable-transition", "claimed", actor)
    restored_repo = ControlPlaneRepo()
    restored_repo.import_state({"events": {"handoff|durable-transition": sink.events}})
    assert HandoffRegistry(restored_repo).get("durable-transition") == claimed
    assert [entry["actor"] for entry in claimed["history"]] == [actor, actor]
    assert [event["sequence"] for event in sink.events] == [0, 1]


def test_concurrent_creates_wait_for_ack_and_share_one_durable_event():
    entered = threading.Event()
    acknowledge = threading.Event()
    completed = threading.Event()
    start = threading.Barrier(8)

    class BlockingSink(HandoffSink):
        def record_required(self, event):
            entered.set()
            assert acknowledge.wait(5), "test did not release durable acknowledgment"
            super().record_required(event)

    sink = BlockingSink()
    repo = ControlPlaneRepo(sink=sink)
    registry = HandoffRegistry(repo)
    body = {"handoff_id": "concurrent-create", "goal": "Create once",
            "source_harness": "codex", "target_harness": "pi"}
    actor = {"id": "operator", "kind": "operator", "authenticated_by": "api_key"}

    def create():
        start.wait(timeout=5)
        try:
            return registry.create(body, actor)
        finally:
            completed.set()

    with ThreadPoolExecutor(max_workers=8) as workers:
        futures = [workers.submit(create) for _ in range(8)]
        try:
            assert entered.wait(5)
            assert not completed.wait(0.1), "a create returned before persistence acknowledgment"
        finally:
            acknowledge.set()
        results = [future.result(timeout=5) for future in futures]
    assert sum(not duplicate for _, duplicate in results) == 1
    assert all(row == results[0][0] for row, _ in results)
    assert len(sink.events) == len(repo.stream("handoff", "concurrent-create")) == 1
    assert len(HandoffRegistry(repo).get("concurrent-create")["history"]) == 1


def test_best_effort_only_sink_cannot_acknowledge_handoff_creation():
    class BestEffortOnly:
        def record(self, event):
            raise AssertionError("best-effort persistence cannot acknowledge a handoff")

    repo = ControlPlaneRepo(sink=BestEffortOnly())
    registry = HandoffRegistry(repo)
    with pytest.raises(AttributeError, match="record_required"):
        registry.create({"handoff_id": "no-ack", "goal": "Require acknowledgment",
                         "source_harness": "pi", "target_harness": "codex"}, "actor")
    assert registry.list() == []
