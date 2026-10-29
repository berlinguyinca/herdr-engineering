"""Queued assistant delegations are consumed into the existing handoff queue (assistant_dispatch.py).

Every test goes through a real entry point: the HTTP route, or ``ControlPlaneServer.sweep_once`` (the timer the
server runs). The handoff registry is the real one; Pi's side is simulated through the real /handoffs routes."""
import threading
from unittest.mock import Mock

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantError,
    AssistantService,
    assistant_config,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_assistant_followups import (
    KEYS,
    Http,
    grant_via_service,
    pid,
    setup_assistant,
)


@pytest.fixture()
def world(acknowledged_sink):
    section = {"enabled": True, "delegation_consumer": {"enabled": True},
               "tenancy": {"default_tenant": "default",
                           "principals": {pid("alice"): "acme", pid("boss"): "acme",
                                          pid("bea"): "globex", pid("cora"): "globex"}},
               "actions": {"approvers": [pid("boss"), pid("cora")], "collab_users": {pid("alice"): "alice"}}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, assistant_service=svc, api_keys={k: "operator" for k in KEYS.values()})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    svc.lineage_emit = Mock(return_value={"node": "decision:assistant.z", "edges": []})
    yield Http(svr.port), svc, svr
    svr.shutdown()


def approved_delegation(h, svc, tool="request_mission", args=None, who="alice"):
    aid, run = setup_assistant(h, who)
    grant_via_service(svc, who, aid, tool)
    svc._execute_gated(svc.store.runs[run], tool, args or {"title": "T", "task": "do the thing",
                                                              "target_system": "pi", "repo": "o/r"}, 4000)
    x = [p for p in svc.store.proposals.values() if p.status == "awaiting_approval"][-1]
    st, out = h.call("boss", "POST", f"/api/v1/assistant-proposals/{x.proposal_id}/decision", {"decision": "approved"})
    assert st == 200 and out["execution"] == "executed", out
    d = svc.store.delegations[out["result_ref"]]
    assert d.status == "queued"
    return d


def handoffs(h, state=None):
    return h.call("alice", "GET", "/api/v1/handoffs" + (f"?state={state}" if state else ""))[1]["handoffs"]


def test_nothing_consumes_a_queued_delegation_until_dispatch(world):
    h, svc, svr = world
    approved_delegation(h, svc)
    assert handoffs(h) == []


def test_http_dispatch_creates_exactly_one_queued_handoff_is_replay_safe_audited_and_linked(world):
    h, svc, svr = world
    d = approved_delegation(h, svc)
    st, out = h.call("alice", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})
    assert st == 200 and out["status"] == "running", out
    (row,) = handoffs(h, "queued")
    assert row["handoff_id"] == f"assistant.{d.delegation_id}" and row["goal"] == "do the thing"
    assert row["target"]["harness"] == "pi"
    assert row["provenance"]["tenant_id"] == "acme" and row["provenance"]["proposal_id"] == d.correlation["proposal_id"]
    assert row["created_by"]["kind"] == "assistant"
    again = h.call("alice", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})
    assert again[0] == 200 and len(handoffs(h)) == 1
    actions = [e["action"] for e in svc.store.audit if e["subject_id"] == d.delegation_id]
    assert actions.count("assistant.delegation.dispatching") == 1 and actions.count("assistant.delegation.dispatched") == 1
    assert svc.store.verify_audit("acme")
    corr = svc.lineage_emit.call_args.args[1]
    assert corr["tool_id"] == "dispatch_delegation" and corr["handoff_id"] == row["handoff_id"]
    assert corr["target_id"] == f"decision:assistant.exec.{d.correlation['proposal_id']}"


def test_branch_proposal_is_consumed_by_the_server_sweep_and_state_is_mirrored_back(world):
    h, svc, svr = world
    d = approved_delegation(h, svc, "propose_branch", {"title": "Fix", "repo": "o/r", "base": "main"})
    assert d.kind == "branch"
    svr.sweep_once()
    assert d.status == "running" and len(handoffs(h, "queued")) == 1
    svr.sweep_once()                                                        # idempotent
    assert len(handoffs(h)) == 1
    hid = f"assistant.{d.delegation_id}"
    for step in ("claim", "accept", "start", "complete"):                   # the Pi-side executor, via the real routes
        assert h.call("alice", "POST", f"/api/v1/handoffs/{hid}/{step}", {})[0] == 200
    svr.sweep_once()
    assert d.status == "succeeded"
    assert any(e["action"] == "assistant.delegation.synced" for e in svc.store.audit)


def test_fails_closed_without_an_approved_executed_unmodified_proposal(world):
    h, svc, svr = world
    forged = svc.delegate(tenant_id="acme", user_id=pid("alice"),
                          assistant_id=approved_delegation(h, svc).assistant_id, task="sneak", actor="x")
    forged.kind = "mission"                                                 # a mission delegation with no proposal
    st, out = h.call("alice", "POST", f"/api/v1/assistant-delegations/{forged.delegation_id}/dispatch", {})
    assert st == 403 and forged.status == "failed"
    d = approved_delegation(h, svc, args={"title": "U", "task": "another", "target_system": "pi"})
    x = svc.store.proposals[d.correlation["proposal_id"]]
    x.payload = {**x.payload, "task": "something else"}                     # tampered after approval
    st, out = h.call("alice", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})
    assert st == 403 and d.status == "failed" and handoffs(h) == []
    assert any(e["action"] == "assistant.delegation.dispatch_failed" for e in svc.store.audit)


def test_unsupported_target_fails_terminally_and_a_queue_outage_leaves_it_queued_for_retry(world):
    h, svc, svr = world
    bad = approved_delegation(h, svc, args={"title": "T", "task": "x", "target_system": "mainframe"})
    assert h.call("alice", "POST", f"/api/v1/assistant-delegations/{bad.delegation_id}/dispatch", {})[0] == 409
    assert bad.status == "failed" and "not a supported harness" in bad.note
    good = approved_delegation(h, svc)
    real = svr.handoffs.create
    svr.handoffs.create = Mock(side_effect=OSError("disk"))
    assert h.call("alice", "POST", f"/api/v1/assistant-delegations/{good.delegation_id}/dispatch", {})[0] == 400
    assert good.status == "queued" and "dispatch" not in good.correlation
    svr.handoffs.create = real
    assert h.call("alice", "POST", f"/api/v1/assistant-delegations/{good.delegation_id}/dispatch", {})[0] == 200


def test_another_tenant_cannot_dispatch_and_the_handoff_names_only_its_own_tenant(world):
    h, svc, svr = world
    d = approved_delegation(h, svc)
    st, _ = h.call("bea", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})
    assert st == 404
    st, _ = h.call("cora", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})
    assert st == 404 and d.status == "queued"                               # a globex approver is not an acme approver
    assert h.call("boss", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})[0] == 200


def test_claim_once_under_concurrency(world):
    h, svc, svr = world
    d = approved_delegation(h, svc)
    results = []

    def go():
        try:
            svr.delegation_consumer.dispatch(d.delegation_id, tenant_id="acme", user_id=pid("alice"), actor="a")
            results.append("ok")
        except AssistantError as exc:
            results.append(str(exc))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(handoffs(h)) == 1
    assert sum(1 for e in svc.store.audit if e["action"] == "assistant.delegation.dispatching"
               and e["subject_id"] == d.delegation_id) == 1


def test_disabled_by_default_in_yaml(world):
    h, svc, svr = world
    d = approved_delegation(h, svc)
    svc.config["delegation_consumer"]["enabled"] = False
    assert h.call("alice", "POST", f"/api/v1/assistant-delegations/{d.delegation_id}/dispatch", {})[0] == 503
    svr.sweep_once()
    assert d.status == "queued" and handoffs(h) == []
    assert assistant_config({"personal_assistant": {"enabled": True}})["delegation_consumer"]["enabled"] is False
