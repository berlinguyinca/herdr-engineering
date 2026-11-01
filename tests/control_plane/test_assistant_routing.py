"""On-demand delegation: Rakazo picks the target for a task, explains why, and sets a specialist up only inside the
approved delegation. Routing names a place; it never grants anything."""
from unittest.mock import Mock

import pytest

from herdr_engineering.control_plane.assistant import AssistantError, assistant_config
from tests.control_plane.test_assistant_dispatch import world  # noqa: F401  (fixture)
from tests.control_plane.test_placement import hz  # noqa: F401  (fixture: targets + hosts + server)

T, U = "default", "alice"


class _Setup:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def provision(self, type_name):
        from herdr_engineering.assistant_self_setup import SelfSetupError
        self.calls.append(type_name)
        if self.fail:
            raise SelfSetupError(self.fail)
        return {"room_id": "room_1", "agent_id": "agent_1"}


def route(svc, task, **kw):
    return svc.route_delegation(task, tenant_id=T, user_id=U, **kw)


def test_handles_are_validated_words_only():
    def cfg(handles):
        return assistant_config({"personal_assistant": {"enabled": True, "delegation": {"targets": {
            "pi": {"role_ref": None}, "scout": {"role_ref": None, "handles": handles}}}}})
    assert cfg(["web", "browse"])["delegation"]["targets"]["scout"]["handles"] == ["browse", "web"]
    for bad in ([], "web", ["Web"], ["a"], ["x y"], [1]):
        with pytest.raises(ValueError):
            cfg(bad)


def test_no_match_goes_to_the_engineering_default(hz):  # noqa: F811
    _, svc, _ = hz
    out = route(svc, "refactor the parser")
    assert out["target"] == "pi" and out["kind"] == "harness" and out["skipped"] == []


def test_a_named_harness_is_honoured(hz):  # noqa: F811
    _, svc, _ = hz
    assert route(svc, "ask codex to review this")["target"] == "codex"
    assert route(svc, "anything", hint="opencode")["target"] == "opencode"
    with pytest.raises(AssistantError):
        route(svc, "anything", hint="nope")


def test_a_matching_specialist_that_cannot_run_is_reported_and_the_task_still_goes_somewhere(hz):  # noqa: F811
    _, svc, _ = hz
    svc.config["delegation"]["targets"]["pi-secretary"]["handles"] = ["schedule"]
    out = route(svc, "schedule a call with Sam")
    assert out["target"] == "pi" and out["skipped"] == [
        {"target": "pi-secretary", "reason": "not set up and no setup is available"}]


def test_a_matching_specialist_with_setup_and_a_host_is_chosen(hz):  # noqa: F811
    _, svc, _ = hz
    svc.config["delegation"]["targets"]["pi-secretary"]["handles"] = ["schedule"]
    svc.self_setup = _Setup()
    out = route(svc, "please schedule a call")
    assert out["target"] == "pi-secretary" and out["kind"] == "specialist"
    assert "will be set up when you approve" in out["reasons"]
    assert svc.self_setup.calls == []                                           # routing set nothing up


def test_a_specialist_no_host_can_carry_is_skipped(hz):  # noqa: F811
    _, svc, _ = hz
    svc.config["delegation"]["targets"]["scout"]["handles"] = ["browse"]
    svc.self_setup = _Setup()
    svc.host_candidates = lambda: []
    out = route(svc, "browse the vendor site")
    assert out["target"] == "pi" and out["skipped"] == [{"target": "scout", "reason": "no host can carry it"}]


def test_setup_runs_lazily_once_and_only_for_a_configured_specialist(hz):  # noqa: F811
    _, svc, _ = hz
    svc.self_setup = _Setup()
    svc._ensure_specialist("pi", T, U)
    svc._ensure_specialist("claude", T, U)
    assert svc.self_setup.calls == []
    svc._ensure_specialist("pi-secretary", T, U)
    assert svc.self_setup.calls == ["pi-secretary"]
    assert [e["action"] for e in svc.store.audit].count("assistant.setup") == 1


def test_a_failed_lazy_setup_refuses_the_delegation_with_the_reason(hz):  # noqa: F811
    _, svc, _ = hz
    svc.self_setup = _Setup(fail="no active agent named pi-secretary is enrolled for you; enroll it first")
    with pytest.raises(AssistantError, match="cannot start pi-secretary: no active agent"):
        svc._ensure_specialist("pi-secretary", T, U)


def test_auto_target_is_resolved_before_grants_and_approval_see_it(hz):  # noqa: F811
    _, svc, _ = hz
    svc.config["delegation"]["targets"]["pi-secretary"]["handles"] = ["schedule"]
    svc.self_setup = _Setup()
    a = svc.create_assistant(tenant_id=T, user_id=U, name="A")
    th = svc.create_thread(tenant_id=T, user_id=U, assistant_id=a.assistant_id)
    run = svc.send_message(tenant_id=T, user_id=U, assistant_id=a.assistant_id, thread_id=th.thread_id, content="hi")
    svc.execute_tool = Mock(return_value={"context": "ran"})
    svc.grant_tool(tenant_id=T, user_id=U, assistant_id=a.assistant_id, tool_id="delegate", scope="pi-secretary")
    out = svc._execute_gated(run, "delegate", {"task": "schedule a call", "target_system": "auto"}, 500)
    assert "approval required" in out                                            # the grant is for the ROUTED target
    approvals = list(svc.store.approvals.values())
    audit = [e for e in svc.store.audit if e["action"] == "assistant.delegation.routed"]
    assert audit and audit[-1]["detail"].startswith("pi-secretary:")
    assert approvals and "pi-secretary" in approvals[-1].policy_snapshot       # the approval names the routed target
    assert svc.self_setup.calls == []                                           # still nothing set up before approval


def test_route_endpoint_matches_the_service_and_never_creates_anything(hz):  # noqa: F811
    h, svc, _ = hz
    svc.config["delegation"]["targets"]["pi-secretary"]["handles"] = ["schedule"]
    svc.self_setup = _Setup()
    before = (len(svc.store.assistants), len(svc.store.delegations), len(svc.store.grants), len(svc.store.audit))
    st, out = h.call("alice", "GET", "/api/v1/assistant-route?task=schedule%20a%20call")
    assert st == 200 and out["target"] == "pi-secretary"
    assert h.call("alice", "GET", "/api/v1/assistant-route?task=")[0] == 400
    assert h.call("alice", "GET", "/api/v1/assistant-route?task=x&target=nope")[0] == 400
    assert (len(svc.store.assistants), len(svc.store.delegations), len(svc.store.grants), len(svc.store.audit)) == before
    assert svc.self_setup.calls == []
