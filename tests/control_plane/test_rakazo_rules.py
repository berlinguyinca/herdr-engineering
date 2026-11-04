# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Approval rules and auto review: stored per account/space, enforced through the assistant's rule_policy."""
import pytest

from herdr_engineering.control_plane import rakazo_rules as rr
from tests.control_plane.test_rakazo_bridge import alice, make_server, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate


def test_decide_and_match():
    allow = {"effect": "always_allow", "matchKind": "tool", "matchValue": "github.read"}
    deny = {"effect": "deny", "matchKind": "site", "matchValue": "example.com"}
    assert rr.decide([allow], "github.read", "") == "always_allow"
    assert rr.decide([allow], "github.write", "") is None
    assert rr.decide([allow, deny], "github.read", "https://a.example.com/x") == "deny"
    assert rr.matches({"matchKind": "connector", "matchValue": "github"}, "github.read", "")
    assert not rr.matches(deny, "t", "notexample.com")


def test_rules_lifecycle_and_enforcement(srv, alice):
    server, api = srv
    bridge = server.rakazo
    validate("approvalRules/list", api.ok("approvalRules/list", {}, alice))
    made = api.ok("approvalRules/set", {"effect": "always_allow", "matchKind": "tool", "matchValue": "github.read"}, alice)
    validate("approvalRules/set", made)
    assert made["effect"] == "always_allow" and api.ok("approvalRules/list", {}, alice) == [made]
    again = api.ok("approvalRules/set", {"effect": "deny", "matchKind": "tool", "matchValue": "github.read"}, alice)
    assert again["id"] == made["id"] and len(api.ok("approvalRules/list", {}, alice)) == 1
    prev = api.ok("approvalRules/preview", {"id": made["id"], "effect": "remove",
                                            "sample": {"tools": [{"name": "github.read"}]}}, alice)
    validate("approvalRules/preview", prev)
    assert prev == {"before": "deny", "after": "default"}
    user = next(iter(bridge._by_principal))
    tenant = bridge.svc.tenant_for(user)
    assert bridge.svc.rule_policy(tenant, user, "github.read", "") == "deny"
    assert bridge.svc.rule_policy(tenant, "stranger", "github.read", "") is None
    removed = api.ok("approvalRules/remove", {"id": made["id"]}, alice)
    validate("approvalRules/remove", removed)
    assert removed == {"ok": True}
    assert api.ok("approvalRules/list", {}, alice) == []
    assert bridge.svc.rule_policy(tenant, user, "github.read", "") is None
    assert api.rpc("approvalRules/remove", {"id": made["id"]}, alice)[0] == 404


def test_rule_validation_and_account_scope(srv, alice):
    server, api = srv
    bridge = server.rakazo
    for inp in ({"effect": "require_approval", "matchKind": "tool", "matchValue": "x"},
                {"effect": "deny", "matchKind": "nope", "matchValue": "x"},
                {"effect": "deny", "matchKind": "tool", "matchValue": " "}):
        assert api.rpc("approvalRules/set", inp, alice)[0] == 400
    api.ok("approvalRules/set", {"scope": "account", "effect": "deny", "matchKind": "site", "matchValue": "Bad.COM"}, alice)
    assert api.ok("approvalRules/list", {"scope": "account"}, alice)[0]["matchValue"] == "bad.com"
    assert api.ok("approvalRules/list", {}, alice) == []
    user = next(iter(bridge._by_principal))
    assert bridge.svc.rule_policy(bridge.svc.tenant_for(user), user, "fetch", "https://x.bad.com/") == "deny"


def test_auto_review(srv, alice):
    _, api = srv
    got = api.ok("autoReview/get", {}, alice)
    validate("autoReview/get", got)
    assert got == {"mode": "never", "enabled": False, "checkerAvailable": False, "skipApprovals": False}
    setres = api.ok("autoReview/set", {"mode": "ask", "enabled": True}, alice)
    validate("autoReview/set", setres)
    assert setres["mode"] == "ask"
    assert api.ok("autoReview/get", {}, alice)["enabled"] is True
    assert api.rpc("autoReview/set", {"skipApprovals": True}, alice)[0] == 400
    assert api.rpc("autoReview/set", {"mode": "bogus"}, alice)[0] == 400


def _run(svc, tool, policy, grant=True):
    svc.rule_policy = policy
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    if grant:
        svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, tool_id=tool, scope="*")
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, thread_id=t.thread_id, content="hi")
    return svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, tool_id=tool, scope="*",
                                   run_id=run.run_id, idempotency_key="k1", request_payload={}, actor="u1")


def test_policy_deny_beats_a_grant_and_allow_never_creates_one():
    from tests.control_plane.test_assistant_service import make_service
    assert _run(make_service(), "browse", lambda *a: "deny").status == "denied"
    assert _run(make_service(), "browse", lambda *a: "always_allow", grant=False).status == "denied"
    assert _run(make_service(), "browse", lambda *a: 1 / 0).status == "denied"       # a broken lookup fails closed


def test_always_allow_skips_the_prompt_for_a_granted_sensitive_tool():
    import pytest

    from herdr_engineering.control_plane.assistant import ApprovalRequired
    from tests.control_plane.test_assistant_service import make_service
    with pytest.raises(ApprovalRequired):
        _run(make_service(), "delegate", lambda *a: None)
    assert _run(make_service(), "delegate", lambda *a: "always_allow").status in ("authorized", "approved")
    with pytest.raises(ApprovalRequired):      # a tool that can never be preauthorized still asks
        _run(make_service(), "send_message", lambda *a: "always_allow")


def _make_family(server, email="alice@example.com"):
    from dataclasses import replace
    bridge = server.rakazo
    account = next(a for a in bridge._by_id.values() if a.email == email)
    bridge._by_id[account.account_id] = replace(account, role="family")
    bridge._by_principal[account.principal_id] = bridge._by_id[account.account_id]
    return bridge._by_id[account.account_id]


def test_a_family_account_may_deny_but_never_skip_approvals(srv, alice):
    server, api = srv
    allow = api.ok("approvalRules/set", {"effect": "always_allow", "matchKind": "tool", "matchValue": "delegate"}, alice)
    account = _make_family(server)
    assert api.rpc("approvalRules/set", {"effect": "always_allow", "matchKind": "tool", "matchValue": "edit_shared"}, alice)[0] == 403
    assert api.ok("approvalRules/set", {"effect": "deny", "matchKind": "tool", "matchValue": "edit_shared"}, alice)["effect"] == "deny"
    bridge = server.rakazo
    tenant = bridge.svc.tenant_for(account.principal_id)
    assert bridge.svc.rule_policy(tenant, account.principal_id, "delegate", "") is None       # an earlier allow no longer counts
    assert bridge.svc.rule_policy(tenant, account.principal_id, "edit_shared", "") == "deny"
    assert allow["id"]


def test_a_deny_rule_also_stops_the_dry_run_and_an_approved_action_from_running():
    from herdr_engineering.control_plane.assistant import AssistantForbidden
    from tests.control_plane.test_assistant_service import make_service
    svc = make_service()
    a = svc.create_assistant(tenant_id="t1", user_id="u1", name="Helper")
    svc.grant_tool(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, tool_id="browse", scope="*")
    dry = lambda: svc.dry_run_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id,  # noqa: E731
                                          tool_id="browse", scope="*", request_payload={})
    assert dry()["granted"] is True
    t = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, thread_id=t.thread_id, content="hi")
    action = svc.request_tool_action(tenant_id="t1", user_id="u1", assistant_id=a.assistant_id, tool_id="browse", scope="*",
                                     run_id=run.run_id, idempotency_key="k1", request_payload={}, actor="u1")
    assert action.status in ("authorized", "approved")
    svc.rule_policy = lambda *args: "deny"
    assert dry()["granted"] is False
    with pytest.raises(AssistantForbidden, match="deny rule"):
        svc.execute_approved_action(action.action_id, tenant_id="t1", user_id="u1", actor="u1")
