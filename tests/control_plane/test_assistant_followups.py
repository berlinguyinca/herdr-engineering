"""Follow-ups to the lineage/assistant work: an executor for APPROVED actions and proposals, the connector test flow,
tenants from the authenticated principal, and a durable append-only audit log.

Each behaviour is exercised through a real entry point (the HTTP route, or the same service call the route makes),
with the negative cases: not approved, expired, revoked, tampered, another tenant, replay, concurrency."""
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    ApprovalRequired,
    AssistantError,
    AssistantForbidden,
    AssistantNotFound,
    AssistantService,
    assistant_config,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

PR = "pr:github.com/o/r#7"
ANCHOR = {"kind": "node", "node_id": "agent:a1"}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def build(extra=None, tenant="t"):
    clock = Clock()
    section = {"enabled": True, "actions": {"collab_users": {"key:u1": "alice"}, "approvers": ["key:boss"]}}
    for k, v in (extra or {}).items():
        section[k] = {**section.get(k, {}), **v} if isinstance(v, dict) else v
    svc = AssistantService(config=assistant_config({"personal_assistant": section}), now=clock)
    a = svc.create_assistant(tenant_id=tenant, user_id="key:u1", name="A")
    th = svc.create_thread(tenant_id=tenant, user_id="key:u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id=tenant, user_id="key:u1", assistant_id=a.assistant_id,
                           thread_id=th.thread_id, content="hi")
    svc.review_rereview = Mock(return_value="re-review recorded for head abc")
    svc.comment_poster = Mock(return_value=(201, {"comment": {"comment_id": "c1"}}))
    svc.artifact_writer = Mock(return_value={"artifact_id": "art1", "version_id": "v1", "version_number": 1})
    svc.lineage_emit = Mock(return_value={"node": "decision:assistant.x", "edges": []})
    return svc, run, clock


def grant(svc, run, tool, scope="*"):
    return svc.grant_tool(tenant_id=run.tenant_id, user_id=run.user_id, assistant_id=run.assistant_id,
                          tool_id=tool, scope=scope)


def request(svc, run, tool, args, scope="*", key="k1"):
    """The /assistant-runs/{id}/tool entry point. Returns the action (pending approval included)."""
    try:
        return svc.request_tool_action(
            tenant_id=run.tenant_id, user_id=run.user_id, assistant_id=run.assistant_id, tool_id=tool,
            scope=scope, run_id=run.run_id, idempotency_key=key, request_payload=args,
            actor=f"assistant:{run.assistant_id}")
    except ApprovalRequired as ar:
        return next(a for a in svc.store.actions.values() if a.approval_id == ar.approval_id)


def approve(svc, action, by="key:u1"):
    return svc.decide_approval(action.approval_id, tenant_id=action.tenant_id, user_id=by,
                               decision="approved", actor=by)


def execute(svc, action, by="key:u1"):
    return svc.execute_approved_action(action.action_id, tenant_id=action.tenant_id, user_id=by, actor=by)


REVIEW = {"target_system": "claude", "pr_id": PR, "head": "a" * 40, "task": "review please"}
COMMENT = {"room_id": "room1", "anchor": ANCHOR, "text": "looks odd"}


# ---- 1. executor for approved tool actions -----------------------------------------------------------------

def test_approved_review_runs_once_through_the_rereview_ledger_with_lineage_and_audit():
    svc, run, _ = build()
    grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    assert action.status == "requested" and not svc.store.delegations      # a grant alone does nothing
    approve(svc, action)
    assert action.status == "approved" and not svc.store.delegations       # approval is permission, not execution
    done = execute(svc, action)
    assert done.status == "completed" and done.result["delegation_id"]
    (d,) = svc.store.delegations.values()
    assert d.kind == "review" and d.status == "queued" and d.target_system == "claude"
    svc.review_rereview.assert_called_once_with("t", "key:u1", PR, "a" * 40)
    corr = svc.lineage_emit.call_args.args[1]
    assert corr["action_id"] == action.action_id and corr["approval_id"] == action.approval_id
    assert corr["delegation_id"] == d.delegation_id and corr["tool_id"] == "request_review"
    assert any(e["action"] == "assistant.action.executed" and e["subject_id"] == action.action_id
               for e in svc.store.audit)
    again = execute(svc, action)                                            # replay: recorded outcome, no new effect
    assert again.status == "completed" and len(svc.store.delegations) == 1
    assert svc.review_rereview.call_count == 1 and svc.lineage_emit.call_count == 1


def test_approved_comment_is_posted_as_the_owner_once():
    svc, run, _ = build()
    grant(svc, run, "comment_on_node", "room1")
    action = request(svc, run, "comment_on_node", COMMENT, scope="room1")
    approve(svc, action)
    done = execute(svc, action)
    assert done.status == "completed" and done.result["comment_id"] == "c1"
    collab_user, room, body = svc.comment_poster.call_args.args
    assert (collab_user, room, body["text"]) == ("alice", "room1", "looks odd")
    execute(svc, action)
    assert svc.comment_poster.call_count == 1


def test_an_unmapped_collab_identity_fails_closed_and_is_not_retried():
    svc, run, _ = build({"actions": {"collab_users": {}}})
    grant(svc, run, "comment_on_node", "room1")
    action = request(svc, run, "comment_on_node", COMMENT, scope="room1")
    approve(svc, action)
    failed = execute(svc, action)
    assert failed.status == "failed" and "no collaboration identity" in failed.result["error"]
    svc.comment_poster.assert_not_called()
    with pytest.raises(AssistantError, match="not retried"):
        execute(svc, action)


def test_draft_email_saves_a_draft_and_never_sends():
    svc, run, _ = build()
    grant(svc, run, "draft_email", "bob@example.com")
    args = {"to": "bob@example.com", "subject": "hello", "body": "text"}
    action = request(svc, run, "draft_email", args, scope="bob@example.com")
    assert action.status == "authorized"                                   # non-sensitive: a grant is the consent
    done = execute(svc, action)
    assert done.status == "completed" and done.result["sent"] is False and done.result["artifact_id"] == "art1"
    draft = svc.artifact_writer.call_args.args[1]
    assert draft["sent"] is False and draft["status"] == "draft"


def test_send_message_has_no_executor_even_when_approved():
    svc, run, _ = build()
    svc._transport = Mock()
    grant(svc, run, "send_message", "bob@example.com")
    action = request(svc, run, "send_message", {"to": "bob@example.com", "body": "hi"}, scope="bob@example.com")
    svc.decide_approval(action.approval_id, tenant_id="t", user_id="key:boss", decision="approved",
                        actor="key:boss")
    with pytest.raises(AssistantForbidden, match="no post-approval executor"):
        execute(svc, action, by="key:boss")
    svc._transport.assert_not_called()
    assert action.status == "approved" and not svc.store.delegations


def test_not_approved_denied_or_expired_never_runs():
    svc, run, clock = build()
    grant(svc, run, "request_review", "claude")
    pending = request(svc, run, "request_review", REVIEW, scope="claude", key="p")
    with pytest.raises(AssistantError, match="not been approved"):
        execute(svc, pending)
    denied = request(svc, run, "request_review", {**REVIEW, "task": "other"}, scope="claude", key="d")
    svc.decide_approval(denied.approval_id, tenant_id="t", user_id="key:u1", decision="denied", actor="key:u1")
    with pytest.raises(AssistantError):
        execute(svc, denied)
    stale = request(svc, run, "request_review", {**REVIEW, "task": "late"}, scope="claude", key="s")
    approve(svc, stale)
    clock.advance(hours=2)                                                  # past the approval and the action TTL
    with pytest.raises(AssistantError, match="expired|no valid approval"):
        execute(svc, stale)
    svc.review_rereview.assert_not_called()
    assert not svc.store.delegations


def test_an_approval_that_expires_before_execution_is_not_honoured():
    svc, run, clock = build({"approval_ttl_seconds": 60})
    grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    approve(svc, action)
    clock.advance(seconds=120)                                              # the action (1h) is alive, the approval is not
    with pytest.raises(AssistantForbidden, match="no valid approval"):
        execute(svc, action)
    svc.review_rereview.assert_not_called()


def test_revoked_grant_stops_an_approved_action():
    svc, run, _ = build()
    g = grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    approve(svc, action)
    svc.revoke_grant(g.grant_id, tenant_id="t", user_id="key:u1")
    with pytest.raises(AssistantForbidden, match="grant"):
        execute(svc, action)
    svc.review_rereview.assert_not_called()


def test_a_payload_changed_after_approval_is_refused():
    svc, run, _ = build()
    grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    approve(svc, action)
    action.request_payload = {**action.request_payload, "task": "something else entirely"}
    with pytest.raises(AssistantForbidden, match="no longer matches"):
        execute(svc, action)
    svc.review_rereview.assert_not_called()
    assert action.status == "failed"


def test_another_tenant_or_user_cannot_execute_it():
    svc, run, _ = build()
    grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    approve(svc, action)
    for tenant, user in (("other", "key:u1"), ("t", "key:stranger")):
        with pytest.raises(AssistantNotFound):
            svc.execute_approved_action(action.action_id, tenant_id=tenant, user_id=user, actor=user)
    with pytest.raises(AssistantNotFound):
        svc.execute_approved_action("act_missing", tenant_id="t", user_id="key:u1", actor="key:u1")
    svc.review_rereview.assert_not_called()


def test_two_concurrent_executions_run_the_side_effect_once():
    svc, run, _ = build()
    gate, entered = threading.Event(), threading.Event()

    def slow(*a):
        entered.set()
        gate.wait(2)
        return "ok"
    svc.review_rereview = Mock(side_effect=slow)
    grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    approve(svc, action)
    outcomes = []

    def go():
        try:
            outcomes.append(execute(svc, action).status)
        except AssistantError as exc:
            outcomes.append(str(exc))
    t1 = threading.Thread(target=go)
    t1.start()
    assert entered.wait(2)
    go()                                                                    # second caller while the first runs
    gate.set()
    t1.join(3)
    assert svc.review_rereview.call_count == 1
    assert "completed" in outcomes and any("already running" in o for o in outcomes)


# ---- 1b. executor for approved proposals -------------------------------------------------------------------

def propose(svc, run, tool, args):
    return svc._execute_gated(run, tool, args, 4000)


def test_approved_mission_proposal_becomes_one_queued_delegation_with_lineage_and_a_notice():
    svc, run, _ = build()
    grant(svc, run, "request_mission")
    out = propose(svc, run, "request_mission", {"title": "Add health endpoint", "task": "do it", "target_system": "pi"})
    (x,) = svc.store.proposals.values()
    assert x.status == "awaiting_approval" and x.execution == "not_started" and "proposal" in out
    with pytest.raises(AssistantError, match="not been approved"):
        svc.execute_approved_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    svc.decide_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    assert not svc.store.delegations                                        # approval alone starts nothing
    done = svc.execute_approved_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    assert done.execution == "executed" and done.result_ref
    (d,) = svc.store.delegations.values()
    assert d.kind == "mission" and d.status == "queued" and d.task == "do it" and d.target_system == "pi"
    assert d.correlation["proposal_id"] == x.proposal_id
    corr = svc.lineage_emit.call_args.args[1]
    assert corr["tool_id"] == "request_mission" and corr["delegation_id"] == d.delegation_id
    assert corr["action_id"] == f"exec.{x.proposal_id}"
    assert any(n.key == f"proposal-executed:{x.proposal_id}" and n.external is False
               for n in svc.store.notifications.values())
    assert svc.execute_approved_proposal(x.proposal_id, tenant_id="t", user_id="key:u1",
                                         actor="key:u1").execution == "executed"
    assert len(svc.store.delegations) == 1                                  # replay
    assert any(e["action"] == "assistant.proposal.executed" for e in svc.store.audit)


def test_approved_branch_proposal_is_a_branch_delegation_and_writes_no_code():
    svc, run, _ = build()
    grant(svc, run, "propose_branch")
    propose(svc, run, "propose_branch", {"title": "Fix flaky test", "repo": "o/r", "base": "main"})
    (x,) = svc.store.proposals.values()
    svc.decide_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    svc.execute_approved_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    (d,) = svc.store.delegations.values()
    assert d.kind == "branch" and "o/r" in d.task and d.status == "queued"


def test_proposals_that_are_rejected_expired_tampered_or_foreign_never_execute():
    svc, run, clock = build()
    grant(svc, run, "request_mission")
    propose(svc, run, "request_mission", {"title": "A", "task": "t1"})
    propose(svc, run, "request_mission", {"title": "B", "task": "t2"})
    propose(svc, run, "request_mission", {"title": "C", "task": "t3"})
    a, b, c = sorted(svc.store.proposals.values(), key=lambda p: p.title)
    svc.decide_proposal(a.proposal_id, tenant_id="t", user_id="key:u1", decision="rejected", actor="key:u1")
    with pytest.raises(AssistantError):
        svc.execute_approved_proposal(a.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    svc.decide_proposal(b.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    b.payload = {**b.payload, "task": "rewritten after approval"}
    with pytest.raises(AssistantForbidden, match="changed after approval"):
        svc.execute_approved_proposal(b.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    svc.decide_proposal(c.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    with pytest.raises(AssistantNotFound):
        svc.execute_approved_proposal(c.proposal_id, tenant_id="other", user_id="key:u1", actor="key:u1")
    clock.advance(days=30)
    with pytest.raises(AssistantError, match="expired"):
        svc.execute_approved_proposal(c.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    assert not svc.store.delegations


def test_the_requester_cannot_approve_its_own_proposal_and_so_nothing_runs():
    svc, run, _ = build()
    grant(svc, run, "request_mission")
    propose(svc, run, "request_mission", {"title": "A", "task": "t1"})
    (x,) = svc.store.proposals.values()
    x.requested_by = "key:u1"
    with pytest.raises(AssistantForbidden, match="must differ"):
        svc.decide_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    with pytest.raises(AssistantError):
        svc.execute_approved_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    assert not svc.store.delegations


# ---- HTTP harness -------------------------------------------------------------------------------------------

KEYS = {"alice": "assistant-test-alice-key", "boss": "assistant-test-boss-key", "bea": "assistant-test-bea-key",
        "cora": "assistant-test-cora-key"}


def pid(name):
    return f"key:{hashlib.sha256(KEYS[name].encode()).hexdigest()[:12]}"


class Http:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, who, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-API-Key": KEYS[who],
                                              "X-Herdr-Tenant": "forged-tenant"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw.decode(errors="replace")}


@pytest.fixture()
def world(acknowledged_sink):
    """alice + boss in tenant 'acme' (boss is a configured approver), bea + cora in tenant 'globex'."""
    section = {"enabled": True,
               "tenancy": {"default_tenant": "default",
                           "principals": {pid("alice"): "acme", pid("boss"): "acme",
                                          pid("bea"): "globex", pid("cora"): "globex"}},
               "actions": {"approvers": [pid("boss"), pid("cora")], "collab_users": {pid("alice"): "alice"}}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, assistant_service=svc, api_keys={k: "operator" for k in KEYS.values()})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    svc.review_rereview = Mock(return_value="re-review recorded")
    svc.real_lineage = svc.lineage_emit                      # the emitter the server wired to the real graph
    svc.lineage_emit = Mock(return_value={"node": "decision:assistant.z", "edges": []})
    yield Http(svr.port), svc, repo
    svr.shutdown()


def setup_assistant(h, who):
    st, a = h.call(who, "POST", "/api/v1/assistants", {"name": "A"})
    assert st == 201, a
    st, t = h.call(who, "POST", f"/api/v1/assistants/{a['assistant_id']}/threads", {})
    assert st in (200, 201), t
    st, m = h.call(who, "POST", f"/api/v1/assistants/{a['assistant_id']}/threads/{t['thread_id']}/messages",
                   {"content": "hi"})
    assert st in (200, 201, 202), m
    return a["assistant_id"], m.get("run_id") or m["run"]["run_id"]


def grant_via_service(svc, who, assistant_id, tool, scope="*"):
    svc.grant_tool(tenant_id=svc.tenant_for(pid(who)), user_id=pid(who), assistant_id=assistant_id,
                   tool_id=tool, scope=scope)


def test_http_approve_executes_and_the_result_is_visible_with_audit(world):
    h, svc, _ = world
    aid, run = setup_assistant(h, "alice")
    grant_via_service(svc, "alice", aid, "request_review", "claude")
    body = {"tool_id": "request_review", "scope": "claude", "idempotency_key": "r1", "payload": REVIEW}
    st, out = h.call("alice", "POST", f"/api/v1/assistant-runs/{run}/tool", body)
    assert st == 403 and out["code"] == "approval_needed"
    appr = out["approval_id"]
    st, listed = h.call("alice", "GET", "/api/v1/assistant-approvals")
    assert listed["approvals"][0]["action_status"] == "requested"
    assert h.call("alice", "GET", "/api/v1/assistant-delegations")[1]["delegations"] == []
    st, decided = h.call("boss", "POST", f"/api/v1/assistant-approvals/{appr}/decision", {"decision": "approved"})
    assert st == 200 and decided["execution"]["status"] == "completed", decided
    st, dl = h.call("alice", "GET", "/api/v1/assistant-delegations")
    assert len(dl["delegations"]) == 1 and dl["delegations"][0]["kind"] == "review"
    st, again = h.call("boss", "POST", f"/api/v1/assistant-approvals/{appr}/execute", {})
    assert st == 200 and again["status"] == "completed"
    assert len(h.call("alice", "GET", "/api/v1/assistant-delegations")[1]["delegations"]) == 1
    st, aud = h.call("alice", "GET", "/api/v1/assistant-audit")
    assert any(e["action"] == "assistant.action.executed" for e in aud["audit"]) and aud["chain_intact"]


def test_http_auto_execute_can_be_switched_off_in_yaml_and_the_explicit_route_still_works(world):
    h, svc, _ = world
    svc.config["actions"]["auto_execute"] = False
    aid, run = setup_assistant(h, "alice")
    grant_via_service(svc, "alice", aid, "request_review", "claude")
    st, out = h.call("alice", "POST", f"/api/v1/assistant-runs/{run}/tool",
                     {"tool_id": "request_review", "scope": "claude", "idempotency_key": "r1", "payload": REVIEW})
    st, decided = h.call("boss", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/decision",
                         {"decision": "approved"})
    assert "execution" not in decided and not svc.store.delegations
    st, ran = h.call("alice", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/execute", {})
    assert st == 200 and ran["status"] == "completed" and len(svc.store.delegations) == 1


def test_http_approved_proposal_is_executed_and_preview_has_no_side_effect(world):
    h, svc, _ = world
    aid, run = setup_assistant(h, "alice")
    grant_via_service(svc, "alice", aid, "request_mission")
    svc._execute_gated(svc.store.runs[run], "request_mission", {"title": "T", "task": "do the thing"}, 4000)
    (x,) = svc.store.proposals.values()
    st, pv = h.call("alice", "GET", f"/api/v1/assistant-proposals/{x.proposal_id}/preview")
    assert st == 200 and pv["dry_run"] and pv["payload_intact"] and not svc.store.delegations
    st, listed = h.call("alice", "GET", "/api/v1/assistant-proposals")
    assert listed["proposals"][0]["execution"] == "not_started"
    st, out = h.call("boss", "POST", f"/api/v1/assistant-proposals/{x.proposal_id}/decision", {"decision": "approved"})
    assert st == 200 and out["execution"] == "executed" and out["result_ref"]
    dl = h.call("alice", "GET", "/api/v1/assistant-delegations")[1]["delegations"]
    assert [d["kind"] for d in dl] == ["mission"]
    assert h.call("boss", "POST", f"/api/v1/assistant-proposals/{x.proposal_id}/execute", {})[0] == 200
    assert len(svc.store.delegations) == 1


# ---- 3. the dashboard's connector test button ---------------------------------------------------------------

def test_connector_test_goes_through_the_gate_and_reports_approval_pending(world):
    h, svc, _ = world
    aid, _run = setup_assistant(h, "alice")
    st, conn = h.call("alice", "POST", "/api/v1/assistant-connectors",
                      {"name": "W", "kind": "http", "base_url": "https://w.example.com",
                       "auth": {"_allowed_origins": ["w.example.com"]}})
    assert st == 201
    cid = conn["connector_id"]
    old = h.call("alice", "POST", f"/api/v1/assistant-connectors/{cid}/request", {"path": "/"})
    assert old[0] == 400                                                    # the bare call the old button made
    st, out = h.call("alice", "POST", f"/api/v1/assistant-connectors/{cid}/test", {"assistant_id": aid})
    assert st == 403 and out.get("code") != "approval_needed" and "not authorized" in out["error"]   # no grant yet
    grant_via_service(svc, "alice", aid, "connector_request", cid)
    svc._transport = Mock(return_value={"status": 200, "body": '{"pong": true}'})
    st, out = h.call("alice", "POST", f"/api/v1/assistant-connectors/{cid}/test", {"assistant_id": aid})
    assert st == 403 and out["code"] == "approval_needed" and out["approval_id"] and out["dry_run"]
    svc._transport.assert_not_called()                                      # nothing is called until a person approves
    st, listed = h.call("alice", "GET", "/api/v1/assistant-approvals")
    pending = [a for a in listed["approvals"] if a["approval_id"] == out["approval_id"]][0]
    assert pending["decision"] == "pending" and pending["tool_id"] == "connector_request"
    st, decided = h.call("boss", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/decision",
                         {"decision": "approved"})
    assert st == 200 and decided["execution"]["status"] == "completed", decided
    assert decided["action_status"] == "completed" and decided["result"]["ok"] is True
    svc._transport.assert_called_once()
    assert svc._transport.call_args.args[0] == "https://w.example.com/"


def test_connector_test_needs_an_assistant_of_the_caller(world):
    h, svc, _ = world
    aid, _ = setup_assistant(h, "alice")
    st, conn = h.call("alice", "POST", "/api/v1/assistant-connectors",
                      {"name": "W", "kind": "http", "base_url": "https://w.example.com",
                       "auth": {"_allowed_origins": ["w.example.com"]}})
    assert h.call("alice", "POST", f"/api/v1/assistant-connectors/{conn['connector_id']}/test", {})[0] == 400
    assert h.call("bea", "POST", f"/api/v1/assistant-connectors/{conn['connector_id']}/test",
                  {"assistant_id": aid})[0] == 404


# ---- 4. tenants ---------------------------------------------------------------------------------------------

def test_tenant_comes_from_yaml_for_the_authenticated_principal_not_from_the_client(world):
    h, svc, _ = world
    assert svc.tenant_for(pid("alice")) == "acme" and svc.tenant_for(pid("bea")) == "globex"
    assert svc.tenant_for("key:unlisted") == "default" and svc.tenant_for(None) == "default"
    aid, _ = setup_assistant(h, "alice")                  # sent with a forged X-Herdr-Tenant header
    assert svc.store.assistants[aid].tenant_id == "acme"


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "../etc", None, 7])
def test_a_malformed_tenant_in_yaml_stops_startup(bad):
    with pytest.raises(ValueError, match="tenant id"):
        assistant_config({"personal_assistant": {"enabled": True, "tenancy": {"principals": {"key:x": bad}}}})
    with pytest.raises(ValueError, match="tenant id"):
        assistant_config({"personal_assistant": {"enabled": True, "tenancy": {"default_tenant": bad}}})


def test_tenant_isolation_actions_approvals_proposals_notifications_memory_and_audit(world):
    h, svc, _ = world
    a_acme, run_acme = setup_assistant(h, "alice")
    setup_assistant(h, "bea")
    grant_via_service(svc, "alice", a_acme, "request_review", "claude")
    grant_via_service(svc, "alice", a_acme, "request_mission")
    st, out = h.call("alice", "POST", f"/api/v1/assistant-runs/{run_acme}/tool",
                     {"tool_id": "request_review", "scope": "claude", "idempotency_key": "iso", "payload": REVIEW})
    appr = out["approval_id"]
    svc._execute_gated(svc.store.runs[run_acme], "request_mission", {"title": "T", "task": "t"}, 4000)
    (prop,) = svc.store.proposals.values()
    note, _ = svc.notify(tenant_id="acme", user_id=pid("alice"), key="n1", priority="normal", text="hello", link="#/x")
    st, mem = h.call("alice", "POST", f"/api/v1/assistants/{a_acme}/memory", {"content": "acme secret plan"})
    assert st in (200, 201), mem
    mem_id = mem["memory_id"]
    action_id = svc.store.approvals[appr].action_id

    # tenant B, including B's configured approver (cora), sees and can do nothing of A's
    for who in ("bea", "cora"):
        assert h.call(who, "GET", "/api/v1/assistant-approvals")[1]["approvals"] == []
        assert h.call(who, "GET", "/api/v1/assistant-proposals")[1]["proposals"] == []
        assert h.call(who, "GET", "/api/v1/assistant-notifications")[1]["notifications"] == []
        assert h.call(who, "GET", f"/api/v1/assistant-memory/{mem_id}")[0] == 404
        assert h.call(who, "GET", f"/api/v1/assistants/{a_acme}")[0] == 404
        assert h.call(who, "GET", f"/api/v1/assistants/{a_acme}/memory?q=acme")[1].get("memory", []) == []
        assert h.call(who, "POST", f"/api/v1/assistant-approvals/{appr}/decision", {"decision": "approved"})[0] == 404
        assert h.call(who, "POST", f"/api/v1/assistant-approvals/{appr}/execute", {})[0] == 404
        assert h.call(who, "POST", f"/api/v1/assistant-proposals/{prop.proposal_id}/decision",
                      {"decision": "approved"})[0] == 404
        assert h.call(who, "POST", f"/api/v1/assistant-proposals/{prop.proposal_id}/execute", {})[0] == 404
        assert h.call(who, "GET", f"/api/v1/assistant-proposals/{prop.proposal_id}/preview")[0] == 404
        assert h.call(who, "POST", f"/api/v1/assistant-notifications/{note.notification_id}/ack", {})[0] == 404
        assert h.call(who, "DELETE", f"/api/v1/assistant-memory/{mem_id}")[0] == 404
        assert all(e["tenant_id"] == "globex" for e in h.call(who, "GET", "/api/v1/assistant-audit")[1]["audit"])
        assert h.call(who, "POST", f"/api/v1/assistant-runs/{run_acme}/tool",
                      {"tool_id": "request_review", "scope": "claude", "idempotency_key": "x",
                       "payload": REVIEW})[0] == 404
    with pytest.raises(AssistantNotFound):
        svc.execute_approved_action(action_id, tenant_id="globex", user_id=pid("cora"), actor=pid("cora"))
    # nothing of A's changed, and A's own side still works
    assert svc.store.approvals[appr].decision == "pending" and prop.status == "awaiting_approval"
    assert svc.store.memory[mem_id].content == "acme secret plan"
    assert svc.store.notifications[note.notification_id].state != "acknowledged"
    assert h.call("boss", "POST", f"/api/v1/assistant-approvals/{appr}/decision",
                  {"decision": "approved"})[0] == 200


def test_an_approver_in_one_tenant_is_not_an_approver_in_another(world):
    h, svc, _ = world
    a_glob, run_glob = setup_assistant(h, "bea")
    grant_via_service(svc, "bea", a_glob, "request_review", "claude")
    st, out = h.call("bea", "POST", f"/api/v1/assistant-runs/{run_glob}/tool",
                     {"tool_id": "request_review", "scope": "claude", "idempotency_key": "g1", "payload": REVIEW})
    assert h.call("boss", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/decision",
                  {"decision": "approved"})[0] == 404            # boss approves for acme only
    assert h.call("cora", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/decision",
                  {"decision": "approved"})[0] == 200


# ---- 5. durable, append-only audit --------------------------------------------------------------------------

def restart(repo):
    """A new process: a fresh repo rebuilt from the exported event streams."""
    fresh = ControlPlaneRepo()
    fresh.import_state(repo.export_state())
    return fresh


def test_audit_survives_a_restart_through_the_repo_event_store(world):
    h, svc, repo = world
    aid, run = setup_assistant(h, "alice")
    grant_via_service(svc, "alice", aid, "request_review", "claude")
    h.call("alice", "POST", f"/api/v1/assistant-runs/{run}/tool",
           {"tool_id": "request_review", "scope": "claude", "idempotency_key": "au", "payload": REVIEW})
    before = svc.store.audit
    assert before and any(e["action"] == "assistant.approval.requested" for e in before)
    assert repo.events_of_type("assistant_audit")                            # it is in the event store, not only a list

    fresh_repo = restart(repo)
    svr2 = ControlPlaneServer(fresh_repo, assistant_service=AssistantService(
        config=assistant_config({"personal_assistant": {"enabled": True}}), repo=fresh_repo),
        api_keys={k: "operator" for k in KEYS.values()})
    after = svr2.assistant.store.audit
    assert [e["event_id"] for e in after if e["tenant_id"] == "acme"] == [e["event_id"] for e in before
                                                                          if e["tenant_id"] == "acme"]
    assert svr2.assistant.store.verify_audit("acme")


def test_audit_cannot_be_rewritten_through_the_service_or_the_api(world):
    h, svc, repo = world
    aid, run = setup_assistant(h, "alice")
    snapshot = svc.store.audit
    assert snapshot
    snapshot[0]["action"] = "tampered"
    snapshot.clear()
    assert svc.store.audit and svc.store.audit[0]["action"] != "tampered"   # callers only ever get copies
    assert not [m for m in dir(svc) + dir(svc.store)
                if any(w in m for w in ("delete_audit", "clear_audit", "edit_audit", "update_audit", "truncate"))]
    for method in ("POST", "PATCH", "DELETE", "PUT"):
        st, _ = h.call("alice", method, "/api/v1/assistant-audit", {"x": 1})
        assert st not in (200, 201, 204), method
    n = len(svc.store.audit)
    h.call("alice", "DELETE", f"/api/v1/assistants/{aid}")                  # legitimate deletes only ever append
    assert len(svc.store.audit) >= n
    # an entry edited behind the service's back is detected
    entry = svc.store._audit[0]
    entry["detail"] = "forged"
    assert not svc.store.verify_audit("acme")


def test_audit_write_failure_fails_the_operation_closed(world):
    h, svc, repo = world
    setup_assistant(h, "alice")
    count = len(svc.store.audit)
    svc.store.audit_sink = Mock(side_effect=OSError("event store unavailable"))
    with pytest.raises(OSError):
        svc.create_assistant(tenant_id="acme", user_id=pid("alice"), name="B")
    assert len(svc.store.audit) == count                                    # nothing visible that is not durable


def test_audit_is_read_only_scoped_to_the_caller_and_tenant(world):
    h, svc, _ = world
    setup_assistant(h, "alice")
    setup_assistant(h, "bea")
    mine = h.call("alice", "GET", "/api/v1/assistant-audit")[1]
    assert mine["audit"] and {e["tenant_id"] for e in mine["audit"]} == {"acme"}
    assert {e["user_id"] for e in mine["audit"]} == {pid("alice")}
    theirs = h.call("bea", "GET", "/api/v1/assistant-audit")[1]
    assert {e["tenant_id"] for e in theirs["audit"]} == {"globex"}


# ---- review follow-ups: audit-before-effect, real lineage, ingest cannot forge audit -------------------------

def test_the_audit_store_being_down_stops_an_approved_action_before_its_side_effect():
    svc, run, _ = build()
    grant(svc, run, "request_review", "claude")
    action = request(svc, run, "request_review", REVIEW, scope="claude")
    approve(svc, action)
    real_sink = svc.store.audit_sink
    svc.store.audit_sink = Mock(side_effect=OSError("event store unavailable"))
    with pytest.raises(OSError):
        execute(svc, action)
    svc.review_rereview.assert_not_called()                 # the "executing" entry could not be made durable
    assert action.status == "approved" and not svc.store.delegations
    svc.store.audit_sink = real_sink
    assert execute(svc, action).status == "completed"       # nothing was burned: it runs once the store is back
    assert svc.review_rereview.call_count == 1
    actions = [e["action"] for e in svc.store.audit if e["subject_id"] == action.action_id]
    assert actions.index("assistant.action.executing") < actions.index("assistant.action.executed")


def test_the_audit_store_being_down_stops_an_approved_proposal_before_it_is_handed_off():
    svc, run, _ = build()
    grant(svc, run, "request_mission")
    propose(svc, run, "request_mission", {"title": "A", "task": "t1"})
    (x,) = svc.store.proposals.values()
    svc.decide_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    svc.store.audit_sink = Mock(side_effect=OSError("event store unavailable"))
    with pytest.raises(OSError):
        svc.execute_approved_proposal(x.proposal_id, tenant_id="t", user_id="key:u1", actor="key:u1")
    assert x.execution == "not_started" and not svc.store.delegations


def test_http_a_down_audit_store_reports_not_executed_and_the_approval_stands(world):
    h, svc, _ = world
    aid, run = setup_assistant(h, "alice")
    grant_via_service(svc, "alice", aid, "request_review", "claude")
    st, out = h.call("alice", "POST", f"/api/v1/assistant-runs/{run}/tool",
                     {"tool_id": "request_review", "scope": "claude", "idempotency_key": "d1", "payload": REVIEW})
    real_sink = svc.store.audit_sink
    calls = {"n": 0}

    def flaky(entry):
        if entry["action"] == "assistant.action.executing":
            raise OSError("event store unavailable")
        real_sink(entry)
    svc.store.audit_sink = flaky
    st, decided = h.call("boss", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/decision",
                         {"decision": "approved"})
    assert st == 200 and decided["decision"] == "approved"
    assert decided["execution"]["status"] == "not_executed" and not svc.store.delegations
    calls["n"] += 1
    svc.store.audit_sink = real_sink
    st, ran = h.call("alice", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/execute", {})
    assert st == 200 and ran["status"] == "completed"


def test_real_lineage_node_and_delegation_edge_for_an_executed_review_and_an_executed_proposal(world):
    h, svc, repo = world
    svc.lineage_emit = svc.real_lineage                      # the emitter the API wired: real ingest, real graph
    aid, run = setup_assistant(h, "alice")
    grant_via_service(svc, "alice", aid, "request_review", "claude")
    grant_via_service(svc, "alice", aid, "request_mission")
    st, out = h.call("alice", "POST", f"/api/v1/assistant-runs/{run}/tool",
                     {"tool_id": "request_review", "scope": "claude", "idempotency_key": "ln1", "payload": REVIEW})
    st, decided = h.call("boss", "POST", f"/api/v1/assistant-approvals/{out['approval_id']}/decision",
                         {"decision": "approved"})
    assert decided["execution"]["status"] == "completed"
    action_id = svc.store.approvals[out["approval_id"]].action_id
    thread_id = svc.store.runs[run].thread_id
    st, node = h.call("alice", "GET", f"/api/v1/graph/nodes/decision:assistant.{action_id}")
    assert st == 200 and node["node"]["attrs"]["approval_id"] == out["approval_id"]
    assert node["node"]["attrs"]["delegation_id"]
    st, hood = h.call("alice", "GET", f"/api/v1/graph/nodes/decision:assistant.{action_id}/neighborhood?depth=1")
    assert st == 200
    edges = {(e["edge_type"], e["from_id"], e["to_id"]) for e in hood["edges"]}
    assert ("delegation", f"thread:assistant.{thread_id}", f"decision:assistant.{action_id}") in edges
    # a proposal: approved, executed, and the execution is its own node (id with an extra dot) on the same thread
    svc._execute_gated(svc.store.runs[run], "request_mission", {"title": "T", "task": "do it"}, 4000)
    (prop,) = svc.store.proposals.values()
    st, done = h.call("boss", "POST", f"/api/v1/assistant-proposals/{prop.proposal_id}/decision",
                      {"decision": "approved"})
    assert done["execution"] == "executed"
    exec_node = f"decision:assistant.exec.{prop.proposal_id}"
    st, node2 = h.call("alice", "GET", f"/api/v1/graph/nodes/{exec_node}")
    assert st == 200 and node2["node"]["attrs"]["delegation_id"] == done["result_ref"]
    st, hood2 = h.call("alice", "GET", f"/api/v1/graph/nodes/{exec_node}/neighborhood?depth=1")
    assert ("delegation", f"thread:assistant.{thread_id}", exec_node) in {
        (e["edge_type"], e["from_id"], e["to_id"]) for e in hood2["edges"]}
    assert h.call("bea", "GET", f"/api/v1/graph/nodes/{exec_node}")[0] == 404         # private to its user


def test_ingest_cannot_write_or_forge_audit_events_and_readback_ignores_foreign_ones(world):
    h, svc, repo = world
    setup_assistant(h, "alice")
    forged = {"schema_version": 2, "event_id": "forge-1", "source": "pi", "source_sequence": 0,
              "entity_type": "assistant_audit", "entity_id": "acme", "event_type": "AssistantAuditAppended",
              "payload": {"event_id": "evt_forged", "tenant_id": "acme", "user_id": pid("alice"),
                          "actor": "attacker", "action": "assistant.approval.approved", "subject_id": "x"}}
    st, _ = h.call("alice", "POST", "/api/v1/ingest", forged)
    assert st >= 400
    assert not [e for e in svc.store.audit if e["event_id"] == "evt_forged"]
    # even an event of that type that reached the store by another path is ignored unless it is ours
    repo.record_event("assistant_audit", "acme", "SomethingElse", {"event_id": "evt_other", "tenant_id": "acme"})
    repo.record_event("assistant_audit", "acme", "AssistantAuditAppended",
                      {"event_id": "evt_env", "tenant_id": "acme", "_envelope": {"content_sha256": "x"}})
    fresh_repo = restart(repo)
    svr2 = ControlPlaneServer(fresh_repo, assistant_service=AssistantService(
        config=assistant_config({"personal_assistant": {"enabled": True}}), repo=fresh_repo),
        api_keys={k: "operator" for k in KEYS.values()})
    ids = {e["event_id"] for e in svr2.assistant.store.audit}
    assert "evt_other" not in ids and "evt_env" not in ids and ids


def test_restore_never_drops_a_live_audit_entry(world):
    h, svc, _ = world
    setup_assistant(h, "alice")
    live = {e["event_id"] for e in svc.store.audit}
    svc.store.restore({"audit": []})                          # e.g. an older snapshot loaded after the sink attached
    assert live <= {e["event_id"] for e in svc.store.audit}


def test_repeated_connector_test_clicks_reuse_the_pending_approval_and_the_summary_has_no_body(world):
    h, svc, _ = world
    aid, _ = setup_assistant(h, "alice")
    st, conn = h.call("alice", "POST", "/api/v1/assistant-connectors",
                      {"name": "W", "kind": "http", "base_url": "https://w.example.com",
                       "auth": {"_allowed_origins": ["w.example.com"]}})
    cid = conn["connector_id"]
    grant_via_service(svc, "alice", aid, "connector_request", cid)
    svc._transport = Mock(return_value={"status": 200, "body": "EXTERNAL-RESPONSE-BODY"})
    first = h.call("alice", "POST", f"/api/v1/assistant-connectors/{cid}/test", {"assistant_id": aid})[1]
    second = h.call("alice", "POST", f"/api/v1/assistant-connectors/{cid}/test", {"assistant_id": aid})[1]
    assert first["approval_id"] == second["approval_id"]
    assert len([a for a in svc.store.approvals.values() if a.decision == "pending"]) == 1
    st, decided = h.call("boss", "POST", f"/api/v1/assistant-approvals/{first['approval_id']}/decision",
                         {"decision": "approved"})
    assert decided["execution"]["status"] == "completed"
    assert "EXTERNAL-RESPONSE-BODY" not in json.dumps(decided["result"])
