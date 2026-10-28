"""Contract tests for the Herdr Personal Assistant API under /api/v1.

Uses the same live-server Client pattern as the rest of tests/control_plane.
The assistant feature is enabled in these tests via an injected service so the
routes are exercised; the default (flag off) path is covered by the 503 test.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def request(self, path, body=None, method=None, headers=None):
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=h,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path, **kw):
        return self.request(path, method="GET", **kw)

    def post(self, path, body, **kw):
        return self.request(path, body=body, method="POST", **kw)

    def delete(self, path, **kw):
        return self.request(path, method="DELETE", **kw)


@pytest.fixture()
def server(acknowledged_sink):
    cfg = assistant_config({"personal_assistant": {"enabled": True}})
    assistant = AssistantService(config=cfg)
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, assistant_service=assistant)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield Client(svr.port)
    svr.shutdown()


def _uid(user="u1", tenant="t1"):
    return {"X-Herdr-User": user, "X-Herdr-Tenant": tenant}


def test_disabled_feature_returns_503():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)  # default: assistant disabled
    t = threading.Thread(target=svr.serve, daemon=True); t.start(); svr.wait_until_ready()
    c = Client(svr.port)
    try:
        status, _ = c.get("/api/v1/assistants")
        assert status == 503
        assert "assistant_v1" not in json.loads(c.get("/api/v1/health")[1]).get("capabilities", [])
    finally:
        svr.shutdown()


def test_enabled_capability_advertised(server):
    status, body = server.get("/api/v1/health")
    assert status == 200
    caps = json.loads(body).get("capabilities", [])
    assert "assistant_v1" in caps


def test_create_and_list_assistants(server):
    status, body = server.post("/api/v1/assistants", {"name": "Helper", "persona": "helpful"},
                               headers=_uid())
    assert status == 201
    created = json.loads(body)
    aid = created["assistant_id"]
    status, body = server.get("/api/v1/assistants", headers=_uid())
    assert status == 200
    data = json.loads(body)
    assert [a["assistant_id"] for a in data["assistants"]] == [aid]


def test_get_single_assistant(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.get(f"/api/v1/assistants/{aid}", headers=_uid())
    assert status == 200
    assert json.loads(body)["name"] == "Helper"


def test_cross_user_assistant_404(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid("u1"))
    aid = json.loads(body)["assistant_id"]
    status, _ = server.get(f"/api/v1/assistants/{aid}", headers=_uid("u2"))
    assert status == 404


def test_update_and_archive_assistant(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.request(f"/api/v1/assistants/{aid}", {"name": "Renamed"},
                                  method="PATCH", headers=_uid())
    assert status == 200
    assert json.loads(body)["name"] == "Renamed"
    status, _ = server.delete(f"/api/v1/assistants/{aid}", headers=_uid())
    assert status == 200
    status, body = server.get(f"/api/v1/assistants/{aid}", headers=_uid())
    assert json.loads(body)["enabled"] is False


def test_thread_and_chat_flow(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    _, body = server.post(f"/api/v1/assistants/{aid}/threads", {"title": "Hello"},
                          headers=_uid())
    tid = json.loads(body)["thread_id"]
    status, body = server.post(f"/api/v1/assistants/{aid}/threads/{tid}/messages",
                               {"content": "hello"}, headers=_uid())
    assert status == 202
    run = json.loads(body)["run"]
    assert run["state"] == "completed"
    # thread reload shows messages only for the owning user (A3)
    status, body = server.get(f"/api/v1/assistants/{aid}/threads/{tid}", headers=_uid())
    assert status == 200
    data = json.loads(body)
    assert len(data["messages"]) == 2
    assert data["messages"][-1]["role"] == "assistant"
    # a different user cannot read this thread
    status, _ = server.get(f"/api/v1/assistants/{aid}/threads/{tid}", headers=_uid("u2"))
    assert status == 404


def test_permissions_grant_and_tools(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.post(f"/api/v1/assistants/{aid}/permissions",
                               {"tool_id": "browse", "scope": "*"}, headers=_uid())
    assert status == 201
    status, body = server.get(f"/api/v1/assistants/{aid}/permissions", headers=_uid())
    assert status == 200
    data = json.loads(body)
    assert any(t["tool_id"] == "browse" and t["granted"] for t in data["tools"])


def test_approval_flow_via_api(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    server.post(f"/api/v1/assistants/{aid}/permissions",
                {"tool_id": "send_message", "scope": "*"}, headers=_uid())
    _, body = server.post(f"/api/v1/assistants/{aid}/threads", {"title": "T"}, headers=_uid())
    tid = json.loads(body)["thread_id"]
    _, body = server.post(f"/api/v1/assistants/{aid}/threads/{tid}/messages",
                          {"content": "hi"}, headers=_uid())
    run_id = json.loads(body)["run"]["run_id"]
    # sensitive tool -> approval needed
    status, body = server.post(f"/api/v1/assistant-runs/{run_id}/tool",
                               {"tool_id": "send_message", "scope": "*",
                                "idempotency_key": "k1", "payload": {}}, headers=_uid())
    assert status == 403
    resp = json.loads(body)
    assert resp["code"] == "approval_needed"
    approval_id = resp["approval_id"]
    # approve
    status, _ = server.post(f"/api/v1/assistant-approvals/{approval_id}/decision",
                            {"decision": "approved"}, headers=_uid())
    assert status == 200
    status, body = server.get("/api/v1/assistant-approvals", headers=_uid())
    data = json.loads(body)
    assert data["approvals"][0]["decision"] == "approved"


def test_memory_store_search_export_forget(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.post(f"/api/v1/assistants/{aid}/memory",
                               {"content": "User likes concise answers", "source_ref": "chat"},
                               headers=_uid())
    assert status == 201
    mid = json.loads(body)["memory_id"]
    status, body = server.get(f"/api/v1/assistants/{aid}/memory?q=concise", headers=_uid())
    assert json.loads(body)["memory"][0]["memory_id"] == mid
    status, body = server.post(f"/api/v1/assistants/{aid}/memory/export", {}, headers=_uid())
    assert json.loads(body)["count"] == 1
    status, _ = server.delete(f"/api/v1/assistant-memory/{mid}", headers=_uid())
    assert status == 200
    status, body = server.post(f"/api/v1/assistants/{aid}/memory/export", {}, headers=_uid())
    assert json.loads(body)["count"] == 0


def test_patch_delete_restricted_to_assistant_resources(server):
    # PATCH/DELETE are enabled only for the assistant resource family; other
    # /api/v1 resources must keep their prior 501 behavior so an unintended
    # verb cannot reach their POST-only _api_post branches.
    status, body = server.request("/api/v1/handoffs", {"kind": "task", "title": "x", "owner_id": "u1"},
                                  method="DELETE", headers=_uid())
    assert status == 501
    status, body = server.request("/api/v1/scheduler", {"resource_profile": {"class": "cpu"}},
                                  method="PATCH", headers=_uid())
    assert status == 501
    # assistant routes still accept PATCH/DELETE
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, _ = server.request(f"/api/v1/assistants/{aid}", {"name": "Renamed"},
                               method="PATCH", headers=_uid())
    assert status == 200


def test_routines_crud_and_trigger(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.post("/api/v1/assistant-routines",
                               {"assistant_id": aid, "name": "Briefing",
                                "trigger_type": "interval",
                                "trigger_spec": {"every": "hours", "amount": 1},
                                "timezone": "UTC"}, headers=_uid())
    assert status == 201
    rid = json.loads(body)["routine_id"]
    status, body = server.get("/api/v1/assistant-routines", headers=_uid())
    assert any(r["routine_id"] == rid for r in json.loads(body)["routines"])
    # trigger it (force next_at into the past is not needed; trigger is idempotent)
    status, body = server.post(f"/api/v1/assistant-routines/{rid}/trigger", {}, headers=_uid())
    assert status == 200
    # archive (DELETE)
    status, _ = server.delete(f"/api/v1/assistant-routines/{rid}", headers=_uid())
    assert status == 200
    status, body = server.get(f"/api/v1/assistant-routines/{rid}", headers=_uid())
    assert json.loads(body)["enabled"] is False


def test_memory_correction_via_api(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    _, body = server.post(f"/api/v1/assistants/{aid}/memory",
                          {"content": "User likes coffee"}, headers=_uid())
    mid = json.loads(body)["memory_id"]
    status, body = server.request(f"/api/v1/assistant-memory/{mid}",
                                  {"content": "User prefers tea"}, method="PATCH", headers=_uid())
    assert status == 200
    assert json.loads(body)["content"] == "User prefers tea"


def test_connector_crud_and_request(server):
    status, body = server.post("/api/v1/assistant-connectors",
                               {"name": "Weather", "kind": "http",
                                "base_url": "https://w.example.com",
                                "auth": {"_allowed_origins": ["w.example.com"], "token": "s3cr3t"}},
                               headers=_uid())
    assert status == 201
    cid = json.loads(body)["connector_id"]
    # auth is never serialized
    assert json.loads(body)["auth_present"] is True
    status, body = server.get("/api/v1/assistant-connectors", headers=_uid())
    assert any(c["connector_id"] == cid for c in json.loads(body)["connectors"])
    # SSRF-guarded request to a non-allowlisted host
    status, body = server.post(f"/api/v1/assistant-connectors/{cid}/request",
                               {"path": "https://evil.example.net/x"}, headers=_uid())
    assert json.loads(body)["ok"] is False
    # archive (DELETE)
    status, _ = server.delete(f"/api/v1/assistant-connectors/{cid}", headers=_uid())
    assert status == 200


def test_delegation_create_list_cancel(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.post("/api/v1/assistant-delegations",
                               {"assistant_id": aid, "task": "Add a health endpoint"},
                               headers=_uid())
    assert status == 201
    dlg = json.loads(body)
    assert dlg["status"] == "queued"
    did = dlg["delegation_id"]
    status, body = server.post(f"/api/v1/assistant-delegations/{did}/artifact",
                               {"artifact_ref": "artifact://mission/MSN-1"}, headers=_uid())
    assert json.loads(body)["status"] == "running"
    status, body = server.post(f"/api/v1/assistant-delegations/{did}/cancel", {}, headers=_uid())
    assert json.loads(body)["status"] == "cancelled"


def test_diagnostics(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.get(f"/api/v1/assistants/{aid}/diagnostics", headers=_uid())
    assert status == 200
    d = json.loads(body)
    assert d["runner"] == "LocalRunnerAdapter"
    assert d["enabled"] is True
