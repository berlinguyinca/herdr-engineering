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


def test_assistant_config_kwarg_builds_enabled_assistant():
    # Regression: the server must honour the assistant_config kwarg (the fleet
    # config's personal_assistant section). A prior shadowing bug always built it
    # disabled regardless of this kwarg.
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, assistant_config={"personal_assistant": {"enabled": True}})
    assert svr.assistant.config["enabled"] is True


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


def test_rate_limit_returns_429():
    # a low cap so a handful of calls trips the limiter -> HTTP 429
    cfg = assistant_config({"personal_assistant": {"enabled": True,
                                                   "max_requests_per_minute": 2}})
    assistant = AssistantService(config=cfg)
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, assistant_service=assistant)
    t = threading.Thread(target=svr.serve, daemon=True); t.start(); svr.wait_ready() if hasattr(svr, 'wait_ready') else svr.wait_until_ready()
    c = Client(svr.port)
    try:
        for _ in range(2):
            assert c.get("/api/v1/assistants", headers=_uid())[0] == 200
        status, body = c.get("/api/v1/assistants", headers=_uid())
        assert status == 429
        assert json.loads(body)["code"] == "rate_limited"
    finally:
        svr.shutdown()


def test_diagnostics(server):
    _, body = server.post("/api/v1/assistants", {"name": "Helper"}, headers=_uid())
    aid = json.loads(body)["assistant_id"]
    status, body = server.get(f"/api/v1/assistants/{aid}/diagnostics", headers=_uid())
    assert status == 200
    d = json.loads(body)
    assert d["runner"] == "LocalRunnerAdapter"
    assert d["enabled"] is True


class _FakeRepo:
    def list_hosts(self):
        return [{"host_name": "bender", "status": "online", "problem_count": 2},
                {"host_name": "beast", "status": "online"}]

    def list_missions(self):
        return [{"title": "Add PA", "status": "running", "stage": "implement"}]

    def list_sessions(self):
        return [{"display_name": "Clever Iris Amber", "host_id": "beast", "status": "active"}]

    def list_services(self, *, include_archived=True):
        return [{"service_name": "cp-web", "status": "running"}]

    def recent_events(self, limit=50):
        return [{"type": "session.report"}, {"type": "session.report"}, {"type": "deploy"}]


def test_fleet_context_provider_is_bounded_redacted_and_framed():
    from herdr_engineering.control_plane.assistant import FleetContextProvider
    ctx = FleetContextProvider(_FakeRepo(), max_chars=6000, per_section=10).render()
    assert "INFORMATIONAL ONLY" in ctx          # prompt-injection framing
    assert "host bender" in ctx and "2 problems" in ctx
    assert "mission Add PA" in ctx and "agent Clever Iris Amber" in ctx
    assert "session.report: 2" in ctx           # activity summarized, not raw payloads

def test_fleet_context_redacts_secrets_and_truncates():
    from herdr_engineering.control_plane.assistant import FleetContextProvider

    class _LeakyRepo(_FakeRepo):
        def list_services(self, *, include_archived=True):
            return [{"service_name": "svc password=hunter2supersecret", "status": "up"}]

    ctx = FleetContextProvider(_LeakyRepo(), max_chars=6000).render()
    assert "hunter2supersecret" not in ctx and "REDACTED" in ctx
    small = FleetContextProvider(_FakeRepo(), max_chars=500).render()
    assert len(small) <= 500 + len(" ...(truncated)")

def test_http_runner_injects_fleet_context_as_system_message():
    from herdr_engineering.control_plane.assistant import (
        AssistantService, FleetContextProvider, HttpModelRunnerAdapter)
    seen = {}

    def fake_urlopen(url, payload, timeout):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "PA-OK"}}]}

    service = AssistantService(
        config=assistant_config({"personal_assistant": {
            "enabled": True,
            "inference": {"endpoint": "http://proxy.invalid/v1/chat/completions",
                          "model": "qwen-flash"},
            "fleet_context": {"enabled": True}}}))
    service.fleet_context = FleetContextProvider(_FakeRepo())
    service.runner = HttpModelRunnerAdapter(
        service.store, endpoint="http://proxy.invalid/v1/chat/completions",
        model="qwen-flash", context_provider=service.fleet_context, urlopen=fake_urlopen)
    a = service.create_assistant(tenant_id="t1", user_id="u1", name="A")
    th = service.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    service.send_message(tenant_id="t1", user_id="u1",
                         assistant_id=a.assistant_id, thread_id=th.thread_id,
                         content="what is down?")
    msgs = seen["payload"]["messages"]
    assert msgs[0]["role"] == "system" and "host bender" in msgs[0]["content"]
    assert msgs[-1]["content"] == "what is down?"

def test_read_fleet_tool_requires_enabled_fleet_context():
    service = AssistantService(
        config=assistant_config({"personal_assistant": {
            "enabled": True, "inference": {"endpoint": "http://x.invalid"}}}))
    a = service.create_assistant(tenant_id="t1", user_id="u1", name="A")
    from herdr_engineering.control_plane.assistant import AssistantError
    try:
        service.execute_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                             tool_id="read_fleet", payload={}); assert False
    except AssistantError as exc:
        assert "not enabled" in str(exc)

def test_read_fleet_tool_returns_context_when_enabled():
    from herdr_engineering.control_plane.assistant import (
        AssistantService, FleetContextProvider)
    service = AssistantService(
        config=assistant_config({"personal_assistant": {
            "enabled": True, "inference": {"endpoint": "http://x.invalid"},
            "fleet_context": {"enabled": True}}}))
    service.fleet_context = FleetContextProvider(_FakeRepo())
    a = service.create_assistant(tenant_id="t1", user_id="u1", name="A")
    out = service.execute_tool(tenant_id="t1", user_id="u1",
                               assistant_id=a.assistant_id,
                               tool_id="read_fleet", payload={"sections": ["hosts"]})
    assert "host bender" in out["context"] and "[missions]" not in out["context"]


class _FakeStepRunner:
    """Scripted tool-calling runner: yields canned step() responses and records
    the messages it was shown at each step (to assert tool results are fed back)."""
    supports_tools = True

    def __init__(self, script):
        self.script = script
        self.i = 0
        self.seen = []

    def execute(self, run, messages):
        return "exec-fallback"

    def step(self, run, messages, tools):
        self.seen.append({"messages": list(messages), "tools": list(tools)})
        resp = self.script[self.i] if self.i < len(self.script) else self.script[-1]
        self.i += 1
        return resp


def _tool_service(script, *, tools_enabled=True):
    from herdr_engineering.control_plane.assistant import (
        AssistantService, FleetContextProvider)
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "inference": {"endpoint": "http://x.invalid"},
        "fleet_context": {"enabled": True},
        "tools": {"enabled": tools_enabled, "max_iterations": 3}}}))
    svc.fleet_context = FleetContextProvider(_FakeRepo())
    runner = _FakeStepRunner(script)
    svc.runner = runner
    return svc, runner


def test_tool_loop_executes_granted_tool_then_replies():
    svc, runner = _tool_service([
        {"content": "", "tool_calls": [{"id": "c1", "name": "read_fleet", "arguments": "{}"}]},
        {"content": "bender is up", "tool_calls": []},
    ])
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="A")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="read_fleet", scope="read_fleet")
    th = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=th.thread_id, content="status?")
    assert run.state == "completed"
    reply = svc.store.list_messages(th.thread_id, tenant_id="t1", user_id="u1")[-1].content
    assert reply == "bender is up"
    # the fleet snapshot (tool result) was fed back into the second model step
    tool_msgs = [m for m in runner.seen[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "host bender" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "c1"

def test_tool_loop_denies_tools_without_grant():
    # no grant -> read_fleet is never offered to the model; falls back to single call
    svc, runner = _tool_service([{"content": "no tools", "tool_calls": []}])
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="A")
    th = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     thread_id=th.thread_id, content="status?")
    assert runner.i == 0            # step() never called (no grants -> no tools)
    reply = svc.store.list_messages(th.thread_id, tenant_id="t1", user_id="u1")[-1].content
    assert reply == "exec-fallback"

def test_tool_loop_sensitive_tool_requires_approval_not_executed():
    svc, runner = _tool_service([
        {"content": "", "tool_calls": [{"id": "c1", "name": "delegate",
                                        "arguments": json.dumps({"task": "ship it"})}]},
        {"content": "I need your approval to delegate", "tool_calls": []},
    ])
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="A")
    # grant delegate but NOT preauthorized -> sensitive -> approval required
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="delegate", scope="delegate")
    th = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     thread_id=th.thread_id, content="ship it")
    tool_msg = [m for m in runner.seen[1]["messages"] if m.get("role") == "tool"][0]
    assert "operator approval required" in tool_msg["content"]
    # a pending approval was created; the delegation did NOT auto-run
    pend = [ap for ap in svc.store.list_approvals(tenant_id="t1", user_id="u1")
            if ap.decision == "pending"]
    assert pend

def test_tool_loop_stops_at_iteration_cap():
    svc, runner = _tool_service([
        {"content": "", "tool_calls": [{"id": "c", "name": "read_fleet", "arguments": "{}"}]},
    ])
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="A")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                   tool_id="read_fleet", scope="read_fleet")
    th = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,
                     thread_id=th.thread_id, content="loop")
    assert runner.i == 3            # capped at max_iterations
    reply = svc.store.list_messages(th.thread_id, tenant_id="t1", user_id="u1")[-1].content
    assert "iteration limit" in reply
