"""S0 hardening of the personal assistant: connector-route bypass, request binding, TTL, dry-run,
distinct approver, preauthorized grants, fleet scoping and the unreachable routines/due route.

Service tests use the service directly; HTTP tests use a live server with one key per role."""
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from unittest.mock import Mock

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    ApprovalRequired,
    AssistantAction,
    AssistantError,
    AssistantForbidden,
    AssistantNotFound,
    AssistantService,
    AssistantStore,
    FleetContextProvider,
    HttpModelRunnerAdapter,
    assistant_config,
    describe_action,
    request_digest,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}


def make(section=None, **cfg_over):
    base = {"enabled": True}
    base.update(section or {})
    svc = AssistantService(config=assistant_config({"personal_assistant": base}))
    a = svc.create_assistant(tenant_id="t", user_id="u1", name="A")
    th = svc.create_thread(tenant_id="t", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=th.thread_id, content="hi")
    return svc, a, run


def req(svc, a, run, *, tool="edit_shared", scope="rec", payload=None, key="k1", actor="u1", user="u1"):
    return svc.request_tool_action(
        tenant_id="t", user_id=user, assistant_id=a.assistant_id, tool_id=tool, scope=scope,
        run_id=run.run_id, idempotency_key=key,
        request_payload=payload if payload is not None else {"record": "rec", "field": "f", "value": "v"},
        actor=actor)


def grant(svc, a, tool="edit_shared", scope="rec", **kw):
    return svc.grant_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                          tool_id=tool, scope=scope, **kw)


# ---- request hash, TTL, replay -------------------------------------------------------------------------

def test_hash_binds_principal_assistant_tool_and_payload_but_not_key_order():
    base = dict(tenant_id="t", assistant_id="a", tool_id="edit_shared", scope="s")
    one = request_digest(user_id="u1", payload={"a": 1, "b": 2}, **base)
    assert one == request_digest(user_id="u1", payload={"b": 2, "a": 1}, **base)
    assert one != request_digest(user_id="u2", payload={"a": 1, "b": 2}, **base)
    assert one != request_digest(user_id="u1", payload={"a": 1, "b": 3}, **base)
    assert one != request_digest(**{**base, "assistant_id": "b"}, user_id="u1", payload={"a": 1, "b": 2})


def test_same_key_with_a_different_request_is_refused_not_reused():
    svc, a, run = make()
    grant(svc, a, preauthorized=True, principal=OPERATOR)
    first = req(svc, a, run)
    with pytest.raises(AssistantError, match="different request"):
        req(svc, a, run, payload={"record": "rec", "field": "f", "value": "OTHER"})
    assert req(svc, a, run).action_id == first.action_id


def test_action_expires_and_an_expired_action_never_executes():
    from datetime import UTC, datetime
    clock = [datetime(2026, 10, 29, 12, 0, tzinfo=UTC)]
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "actions": {"action_ttl_seconds": 60}}}), now=lambda: clock[0])
    a = svc.create_assistant(tenant_id="t", user_id="u1", name="A")
    th = svc.create_thread(tenant_id="t", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=th.thread_id, content="hi")
    grant(svc, a)
    svc.execute_tool = Mock(return_value={"context": "ran"})
    args = {"record": "rec", "field": "f", "value": "v"}
    assert "approval required" in svc._execute_gated(run, "edit_shared", args, 500)
    appr = next(iter(svc.store.approvals.values()))
    svc.decide_approval(appr.approval_id, tenant_id="t", user_id="u1", decision="approved", actor="u1")
    action = next(iter(svc.store.actions.values()))
    assert action.expires_at is not None and svc.action_executable(action)
    clock[0] += timedelta(seconds=61)
    assert not svc.action_executable(action)
    assert "expired" in svc._execute_gated(run, "edit_shared", args, 500)
    svc.execute_tool.assert_not_called()
    assert action.status == "expired"


def test_snapshot_without_expiry_is_treated_as_expired():
    action = AssistantAction.__new__(AssistantAction)
    snap = {"action_id": "act_x", "run_id": "r", "tool_id": "browse", "scope": "s", "request_hash": "h",
            "idempotency_key": "k", "status": "authorized"}          # an older snapshot: no new fields
    action = AssistantStore._e_from_dict(AssistantAction, snap)
    svc, _, _ = make()
    assert action.expires_at is None and not svc.action_executable(action)


def test_replay_of_a_completed_action_does_not_execute_twice():
    svc, a, run = make()
    grant(svc, a, preauthorized=True, principal=OPERATOR)
    svc.execute_tool = Mock(return_value={"context": "ran"})
    args = {"record": "rec", "field": "f", "value": "v"}
    assert svc._execute_gated(run, "edit_shared", args, 500) == "ran"
    assert "already ran" in svc._execute_gated(run, "edit_shared", args, 500)
    assert svc.execute_tool.call_count == 1
    assert next(iter(svc.store.actions.values())).status == "completed"


# ---- dry-run --------------------------------------------------------------------------------------------

def test_sensitive_action_carries_a_redacted_bounded_human_readable_dry_run():
    svc, a, run = make()
    grant(svc, a, tool="run_shell", scope="*")
    cmd = "deploy --password=hunter2hunter2 " + "x" * 5000
    with pytest.raises(ApprovalRequired) as ei:
        req(svc, a, run, tool="run_shell", scope="*", payload={"command": cmd})
    approval = svc.store.get_approval(ei.value.approval_id)
    assert approval.dry_run.startswith("Run this shell command")
    assert "hunter2" not in approval.dry_run and "REDACTED" in approval.dry_run
    assert len(approval.dry_run) <= 1200
    assert ei.value.dry_run == approval.dry_run
    assert approval.as_dict()["dry_run"] == approval.dry_run


def test_dry_run_does_not_create_actions_or_approvals():
    svc, a, run = make()
    grant(svc, a, tool="send_message", scope="*")
    out = svc.dry_run_tool_action(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                                  tool_id="send_message", scope="*",
                                  request_payload={"to": "x@example.com", "body": "hello"})
    assert out["approval_required"] is True and out["granted"] is True
    assert "SEND an external message to x@example.com" in out["description"]
    assert not svc.store.actions and not svc.store.approvals


def test_describe_action_never_leaks_a_secret_in_any_field():
    text = describe_action("connector_request", "c1", {"connector_id": "c1", "method": "POST",
                                                       "path": "/x?token=abcd1234efgh", "body": {"k": "v"}})
    assert "abcd1234efgh" not in text and "fields ['k']" in text


# ---- distinct approver ----------------------------------------------------------------------------------

def test_requester_cannot_approve_own_sensitive_action_by_default():
    svc, a, run = make()
    grant(svc, a)
    with pytest.raises(ApprovalRequired) as ei:
        req(svc, a, run, actor="u1")                       # a person asked through the API
    with pytest.raises(AssistantForbidden, match="differ"):
        svc.decide_approval(ei.value.approval_id, tenant_id="t", user_id="u1", decision="approved", actor="u1")
    assert svc.store.get_approval(ei.value.approval_id).decision == "pending"
    # denying one's own request is harmless
    svc.decide_approval(ei.value.approval_id, tenant_id="t", user_id="u1", decision="denied", actor="u1")


def test_operator_yaml_can_opt_in_to_self_approval():
    svc, a, run = make({"actions": {"allow_self_approval": True}})
    grant(svc, a)
    with pytest.raises(ApprovalRequired) as ei:
        req(svc, a, run, actor="u1")
    svc.decide_approval(ei.value.approval_id, tenant_id="t", user_id="u1", decision="approved", actor="u1")
    assert req(svc, a, run).status == "approved"


def test_the_user_may_approve_what_their_assistant_requested():
    svc, a, run = make()
    grant(svc, a)
    svc.execute_tool = Mock(return_value={"context": "ran"})
    args = {"record": "rec", "field": "f", "value": "v"}
    assert "approval required" in svc._execute_gated(run, "edit_shared", args, 500)
    appr = next(iter(svc.store.approvals.values()))
    assert appr.requested_by == f"assistant:{a.assistant_id}"
    svc.decide_approval(appr.approval_id, tenant_id="t", user_id="u1", decision="approved", actor="u1")
    assert svc._execute_gated(run, "edit_shared", args, 500) == "ran"


def test_configured_approver_decides_other_users_actions_and_a_stranger_gets_not_found():
    svc, a, run = make({"actions": {"approvers": ["key:boss"]}})
    grant(svc, a)
    with pytest.raises(ApprovalRequired) as ei:
        req(svc, a, run, actor="u1")
    aid = ei.value.approval_id
    with pytest.raises(AssistantNotFound):
        svc.decide_approval(aid, tenant_id="t", user_id="u9", decision="approved", actor="u9")
    assert [x.approval_id for x in svc.list_approvals(tenant_id="t", user_id="key:boss")] == [aid]
    assert svc.list_approvals(tenant_id="t", user_id="u9") == []
    svc.decide_approval(aid, tenant_id="t", user_id="key:boss", decision="approved", actor="key:boss")
    assert req(svc, a, run).status == "approved"


def test_approval_is_not_decidable_after_the_action_expired():
    svc, a, run = make({"actions": {"allow_self_approval": True}})
    grant(svc, a)
    with pytest.raises(ApprovalRequired) as ei:
        req(svc, a, run)
    svc.store.get_action(svc.store.get_approval(ei.value.approval_id).action_id).expires_at = \
        svc.now() - timedelta(seconds=1)
    with pytest.raises(AssistantError, match="expired"):
        svc.decide_approval(ei.value.approval_id, tenant_id="t", user_id="u1", decision="approved", actor="u1")


# ---- preauthorized grants ------------------------------------------------------------------------------

@pytest.mark.parametrize("principal", [
    None,
    {"kind": "viewer", "id": "key:v", "authenticated_by": "api_key"},
    {"kind": "ingest", "id": "key:i", "authenticated_by": "api_key"},
    {"kind": "agent", "id": "key:a", "authenticated_by": "api_key"},
    {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"},
])
def test_preauthorized_grant_needs_an_authenticated_operator(principal):
    svc, a, _ = make()
    with pytest.raises(AssistantForbidden):
        grant(svc, a, preauthorized=True, principal=principal)
    assert not svc.store.grants


@pytest.mark.parametrize("tool", ["send_message", "run_shell"])
def test_tools_that_must_always_be_approved_cannot_be_preauthorized_even_by_an_operator(tool):
    svc, a, _ = make()
    with pytest.raises(AssistantForbidden, match="explicit approval"):
        grant(svc, a, tool=tool, scope="*", preauthorized=True, principal=OPERATOR)


def test_send_message_has_no_executor_even_with_grant_and_approval():
    svc, a, run = make({"actions": {"allow_self_approval": True}})
    grant(svc, a, tool="send_message", scope="to@example.com")
    payload = {"to": "to@example.com", "body": "hi"}
    with pytest.raises(ApprovalRequired) as ei:
        req(svc, a, run, tool="send_message", scope="to@example.com", payload=payload)
    svc.decide_approval(ei.value.approval_id, tenant_id="t", user_id="u1", decision="approved", actor="u1")
    action = req(svc, a, run, tool="send_message", scope="to@example.com", payload=payload)
    assert svc.action_executable(action)
    with pytest.raises(AssistantError, match="no executor"):
        svc.execute_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                         tool_id="send_message", payload=payload)


def test_no_merge_or_github_write_tool_exists_and_connector_calls_need_approval():
    from herdr_engineering.control_plane.assistant import TOOL_CATALOG
    assert not [t for t in TOOL_CATALOG if "merge" in t or "github" in t]
    assert TOOL_CATALOG["connector_request"]["sensitive"] is True
    svc, a, run = make()
    grant(svc, a, tool="connector_request", scope="github")
    with pytest.raises(ApprovalRequired):
        req(svc, a, run, tool="connector_request", scope="github",
            payload={"connector_id": "github", "method": "PUT", "path": "/repos/o/r/pulls/1/merge"})


# ---- read_fleet scoping --------------------------------------------------------------------------------

class _Repo:
    def hosts(self):
        return [{"id": "bender"}]

    def __getattr__(self, name):
        return lambda *a, **k: []


def _fleet_svc(allowed):
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "fleet_context": {"enabled": True, "allowed_principals": allowed}}}))
    svc.fleet_context = FleetContextProvider(_Repo())
    a = svc.create_assistant(tenant_id="t", user_id="u1", name="A")
    return svc, a


def test_read_fleet_is_denied_by_default_even_for_an_authenticated_user():
    svc, a = _fleet_svc([])
    with pytest.raises(AssistantForbidden):
        svc.execute_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id, tool_id="read_fleet", payload={})


def test_read_fleet_is_scoped_to_the_named_principals():
    svc, a = _fleet_svc(["u1"])
    assert "context" in svc.execute_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                                         tool_id="read_fleet", payload={})
    other = svc.create_assistant(tenant_id="t", user_id="u2", name="B")
    with pytest.raises(AssistantForbidden):
        svc.execute_tool(tenant_id="t", user_id="u2", assistant_id=other.assistant_id,
                         tool_id="read_fleet", payload={})
    assert _fleet_svc(["*"])[0].fleet_allowed("anyone")


def test_fleet_context_is_not_injected_into_the_prompt_of_an_unscoped_user():
    svc, a = _fleet_svc(["u1"])
    runner = HttpModelRunnerAdapter(svc.store, endpoint="http://x.invalid",
                                    context_provider=Mock(render=Mock(return_value="host bender up")),
                                    context_gate=lambda run: svc.fleet_allowed(run.user_id))
    th = svc.create_thread(tenant_id="t", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t", user_id="u1", assistant_id=a.assistant_id,
                           thread_id=th.thread_id, content="hi")
    assert runner._system_message(run) is not None
    stranger = svc.create_assistant(tenant_id="t", user_id="u2", name="B")
    th2 = svc.create_thread(tenant_id="t", user_id="u2", assistant_id=stranger.assistant_id)
    run2 = svc.send_message(tenant_id="t", user_id="u2", assistant_id=stranger.assistant_id,
                            thread_id=th2.thread_id, content="hi")
    assert runner._system_message(run2) is None


# ---- HTTP: connector bypass, role matrix for grants, routines/due -------------------------------------

KEYS = {"viewer": "k-viewer-0001", "ingest": "k-ingest-0001", "agent": "k-agent-00001",
        "reviewer": "k-reviewer-001", "operator": "k-operator-001", "operator2": "k-operator-002"}
ROLE_OF = {"viewer": "viewer", "ingest": "ingest", "agent": "agent", "reviewer": "reviewer",
           "operator": "operator", "operator2": "operator"}


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, who, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json", "X-API-Key": KEYS[who]})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})


@pytest.fixture()
def http(acknowledged_sink):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    transport = Mock(return_value={"status": 200, "body": "ok"})
    svc._transport = transport
    server = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                                api_keys={KEYS[k]: ROLE_OF[k] for k in KEYS})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    yield Client(server.port), svc, transport
    server.shutdown()


def _setup(c, who="operator"):
    _, a = c.call(who, "POST", "/api/v1/assistants", {"name": "A"})
    aid = a["assistant_id"]
    _, t = c.call(who, "POST", f"/api/v1/assistants/{aid}/threads", {})
    _, r = c.call(who, "POST", f"/api/v1/assistants/{aid}/threads/{t['thread_id']}/messages", {"content": "hi"})
    _, conn = c.call(who, "POST", "/api/v1/assistant-connectors",
                     {"name": "W", "kind": "http", "base_url": "https://w.example.com",
                      "auth": {"_allowed_origins": ["w.example.com"]}})
    return aid, r["run"]["run_id"], conn["connector_id"]


def test_connector_request_without_a_run_is_refused_and_calls_nothing(http):
    c, _, transport = http
    _, _, cid = _setup(c)
    status, body = c.call("operator", "POST", f"/api/v1/assistant-connectors/{cid}/request", {"path": "/x"})
    assert status == 400 and "run_id" in body["error"]
    transport.assert_not_called()


def test_connector_request_without_a_grant_is_denied_and_calls_nothing(http):
    c, _, transport = http
    _, rid, cid = _setup(c)
    status, body = c.call("operator", "POST", f"/api/v1/assistant-connectors/{cid}/request",
                          {"path": "/x", "run_id": rid, "idempotency_key": "k"})
    assert status == 403 and body["action"]["status"] == "denied"
    transport.assert_not_called()


def test_connector_request_with_a_grant_still_needs_approval_and_shows_a_dry_run(http):
    c, _, transport = http
    aid, rid, cid = _setup(c)
    assert c.call("operator", "POST", f"/api/v1/assistants/{aid}/permissions",
                  {"tool_id": "connector_request", "scope": cid})[0] == 201
    status, body = c.call("operator", "POST", f"/api/v1/assistant-connectors/{cid}/request",
                          {"path": "/x", "run_id": rid, "idempotency_key": "k"})
    assert status == 403 and body["code"] == "approval_needed"
    assert f"Call connector {cid}" in body["dry_run"]
    transport.assert_not_called()
    # the requester (this same key) cannot approve its own call
    status, body2 = c.call("operator", "POST", f"/api/v1/assistant-approvals/{body['approval_id']}/decision",
                           {"decision": "approved"})
    assert status == 403
    transport.assert_not_called()


def test_connector_request_dry_run_flag_describes_without_calling(http):
    c, _, transport = http
    aid, rid, cid = _setup(c)
    c.call("operator", "POST", f"/api/v1/assistants/{aid}/permissions", {"tool_id": "connector_request", "scope": cid})
    status, body = c.call("operator", "POST", f"/api/v1/assistant-connectors/{cid}/request",
                          {"path": "/x", "run_id": rid, "idempotency_key": "k", "dry_run": True})
    assert status == 200 and body["dry_run"] is True and body["approval_required"] is True
    transport.assert_not_called()


def test_preauthorized_connector_call_runs_once_and_replay_is_idempotent(http):
    c, _, transport = http
    aid, rid, cid = _setup(c)
    assert c.call("operator", "POST", f"/api/v1/assistants/{aid}/permissions",
                  {"tool_id": "connector_request", "scope": cid, "preauthorized": True})[0] == 201
    path = f"/api/v1/assistant-connectors/{cid}/request"
    body = {"path": "/x", "run_id": rid, "idempotency_key": "k"}
    assert c.call("operator", "POST", path, body)[1]["ok"] is True
    status, again = c.call("operator", "POST", path, body)
    assert status == 200 and again["replayed"] is True
    assert transport.call_count == 1


def test_another_user_cannot_use_a_connector_through_someone_elses_run(http):
    c, _, transport = http
    aid, rid, cid = _setup(c)
    c.call("operator", "POST", f"/api/v1/assistants/{aid}/permissions",
           {"tool_id": "connector_request", "scope": cid, "preauthorized": True})
    status, _ = c.call("operator2", "POST", f"/api/v1/assistant-connectors/{cid}/request",
                       {"path": "/x", "run_id": rid, "idempotency_key": "k"})
    assert status == 404
    transport.assert_not_called()


@pytest.mark.parametrize("who", ["viewer", "ingest", "agent", "reviewer"])
def test_only_operator_keys_can_create_any_grant_and_none_is_stored(http, who):
    """Confirms today's behaviour with a negative test: the permissions route already needs the operator role."""
    c, svc, _ = http
    aid, _, _ = _setup(c)
    for pre in (False, True):
        status, _ = c.call(who, "POST", f"/api/v1/assistants/{aid}/permissions",
                           {"tool_id": "edit_shared", "scope": "*", "preauthorized": pre})
        assert status in (403, 404)
    assert not svc.store.grants


def test_operator_can_preauthorize_a_normal_tool_but_not_send_message(http):
    c, svc, _ = http
    aid, _, _ = _setup(c)
    ok, _ = c.call("operator", "POST", f"/api/v1/assistants/{aid}/permissions",
                   {"tool_id": "edit_shared", "scope": "*", "preauthorized": True})
    assert ok == 201
    status, body = c.call("operator", "POST", f"/api/v1/assistants/{aid}/permissions",
                          {"tool_id": "send_message", "scope": "*", "preauthorized": True})
    assert status == 403 and body["code"] == "forbidden"


def test_open_listener_cannot_create_preauthorized_grants(acknowledged_sink):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    server = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        base = f"http://127.0.0.1:{server.port}"

        def post(path, body):
            r = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                       headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        _, a = post("/api/v1/assistants", {"name": "A"})
        status, _ = post(f"/api/v1/assistants/{a['assistant_id']}/permissions",
                         {"tool_id": "edit_shared", "scope": "*", "preauthorized": True})
        assert status == 403
        assert post(f"/api/v1/assistants/{a['assistant_id']}/permissions",
                    {"tool_id": "edit_shared", "scope": "*"})[0] == 201
    finally:
        server.shutdown()


def test_routines_due_route_is_reachable_and_scoped_to_the_assistant(http):
    c, svc, _ = http
    aid, _, _ = _setup(c)
    _, other = c.call("operator", "POST", "/api/v1/assistants", {"name": "B"})
    for assistant in (aid, other["assistant_id"]):
        c.call("operator", "POST", "/api/v1/assistant-routines",
               {"assistant_id": assistant, "name": "r", "trigger_type": "interval",
                "trigger_spec": {"every_seconds": 60}})
    for r in svc.store.routines.values():
        r.next_at = svc.now() - timedelta(seconds=5)
    status, body = c.call("operator", "GET", f"/api/v1/assistants/{aid}/routines/due")
    assert status == 200
    assert [r["assistant_id"] for r in body["routines"]] == [aid]
