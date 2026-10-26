import http.client
import json
import threading

from herdr_engineering.control_plane.api import ControlPlaneServer
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
    repo = ControlPlaneRepo()
    from herdr_engineering.control_plane.handoffs import HandoffRegistry

    registry = HandoffRegistry(repo)
    row, _ = registry.create({"handoff_id": "handoff_restore", "goal": "restore",
                              "source_harness": "pi", "target_harness": "codex"}, "agent-a")
    registry.transition(row["handoff_id"], "claimed", "agent-b")
    restored = HandoffRegistry(repo)
    assert restored.get(row["handoff_id"])["state"] == "claimed"
