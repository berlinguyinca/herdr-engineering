"""Durable Pi delegation lifecycle owned by the existing AssistantService."""
from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from herdr_engineering.assistant_pi_delegate import PiSecretaryBinding, PiSecretaryDelegationAdapter
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantError,
    AssistantService,
    AssistantStore,
    StoreBackend,
    assistant_config,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ROLE = {"id": "procedure:secretary-system", "version": "1.0.0",
        "digest": "sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120"}


class Adapter:
    def __init__(self):
        self.submitted = []; self.polled = []; self.cancelled = []; self.state = "queued"
        self.fail_submit = False

    def submit(self, delegation):
        self.submitted.append(delegation)
        if self.fail_submit:
            raise RuntimeError("private connector detail must not escape")
        return {"external_id": "grant_task1", "status": "queued",
                "artifact_refs": ["pi-task://grant_task1"]}

    def poll(self, external_id):
        self.polled.append(external_id)
        result = {"external_id": external_id, "status": self.state,
                  "artifact_refs": [f"pi-task://{external_id}"]}
        if self.state in {"done", "failed", "needs_context"}:
            result["result_text"] = "Please provide the city." if self.state == "needs_context" else "bounded result"
        return result

    def cancel(self, external_id):
        self.cancelled.append(external_id)
        return {"external_id": external_id, "status": "cancelled"}


class RacingAdapter(Adapter):
    def __init__(self):
        super().__init__(); self.calls = 0; self.guard = threading.Lock()

    def poll(self, external_id):
        with self.guard:
            self.calls += 1
            call = self.calls
        if call == 1:
            time.sleep(0.05)
            return {"external_id": external_id, "status": "running", "artifact_refs": []}
        return {"external_id": external_id, "status": "done",
                "result_text": "complete", "artifact_refs": []}


class DurableBackend(StoreBackend):
    def __init__(self):
        self.value = None
        self.fail_required = False

    def persist(self, snapshot):
        self.value = copy.deepcopy(snapshot)

    def persist_required(self, snapshot):
        if self.fail_required:
            raise OSError("durability unavailable")
        self.value = copy.deepcopy(snapshot)

    def load(self):
        return copy.deepcopy(self.value)


class PiTransport:
    def __init__(self):
        self.task = {"task_id": "grant_real_adapter", "status": "pending", "result": None}

    def create_task(self, **_value):
        return dict(self.task)

    def get_task(self, **_value):
        return dict(self.task)

    def cancel_task(self, **_value):
        self.task["status"] = "cancelled"
        return {"ok": True}


def service(adapter=None, *, targets=None, store=None):
    if store is None:
        store = AssistantStore(persist_backend=DurableBackend())
    cfg = assistant_config({"personal_assistant": {
        "enabled": True,
        "delegation": {"targets": targets or {
            "pi": {"role_ref": None}, "pi-secretary": {"role_ref": ROLE},
        }, "binding_scope": {"tenant_id": "t1", "user_id": "u1"}},
    }})
    return AssistantService(config=cfg, store=store, delegation_adapter=adapter)


def root(svc):
    return svc.create_assistant(tenant_id="t1", user_id="u1", name="Root")


def test_submit_is_persisted_scoped_and_sanitized_before_adapter():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(
        tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
        task="Ask only for the missing location.", target_system="pi-secretary",
        context={"known": ["8 adults", "10 children"]}, source_refs=["event:request-1"],
        run_id="", idempotency_key="secretary-request-1", actor="u1",
    )
    assert delegation.status == "queued" and delegation.external_id == "grant_task1"
    assert delegation.role_ref == ROLE and delegation.artifact_refs == ["pi-task://grant_task1"]
    sent = asdict(adapter.submitted[0])
    assert sent == {
        "delegation_id": delegation.delegation_id, "assistant_id": assistant.assistant_id,
        "run_id": "", "task": "Ask only for the missing location.",
        "target_system": "pi-secretary", "context": {"known": ["8 adults", "10 children"]},
        "source_refs": ["event:request-1"], "role_ref": ROLE,
    }
    assert "tenant_id" not in sent and "user_id" not in sent and "credential" not in sent
    assert svc.store.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1").external_id == "grant_task1"


def test_idempotency_replays_without_resubmit_and_conflicts_on_change():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    args = dict(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                task="T", target_system="pi-secretary", idempotency_key="idem-1")
    first = svc.delegate(**args)
    assert svc.delegate(**args).delegation_id == first.delegation_id
    assert len(adapter.submitted) == 1
    with pytest.raises(AssistantError, match="idempotency"):
        svc.delegate(**{**args, "task": "changed"})


def test_run_provenance_is_server_validated_and_cross_scope_hidden():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    thread = svc.create_thread(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id)
    run = svc.send_message(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                           thread_id=thread.thread_id, content="delegate this")
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary", run_id=run.run_id)
    assert delegation.run_id == run.run_id
    with pytest.raises(AssistantError, match="run"):
        svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                     task="T2", target_system="pi-secretary", run_id="run_forged")
    with pytest.raises(KeyError):
        svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u2")


def test_poll_maps_running_success_artifacts_and_survives_restore():
    adapter = Adapter(); store = AssistantStore(persist_backend=DurableBackend())
    svc = service(adapter, store=store); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    adapter.state = "running"; svc.maintain_delegations()
    assert svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1").status == "running"
    adapter.state = "done"; svc.maintain_delegations()
    done = svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert done.status == "succeeded" and done.result_text == "bounded result"
    assert done.artifact_refs == ["pi-task://grant_task1"] and done.completed_at is not None

    restored = AssistantStore(); restored.restore(store.snapshot())
    copy = restored.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert copy.status == "succeeded" and copy.result_text == "bounded result"


def test_needs_context_is_terminal_bounded_and_connector_free():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    adapter.state = "needs_context"; svc.maintain_delegations()
    result = svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert result.status == "needs_context" and result.result_text == "Please provide the city."
    assert adapter.submitted[0].context == {} and not hasattr(adapter.submitted[0], "connectors")


def test_cancel_waits_for_adapter_receipt_and_is_task_scoped():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    cancelled = svc.cancel_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert cancelled.status == "cancelled" and cancelled.cancel_requested is True
    assert adapter.cancelled == ["grant_task1"]
    with pytest.raises(KeyError):
        svc.cancel_delegation(delegation.delegation_id, tenant_id="t1", user_id="u2")


def test_actual_pi_cancel_stays_nonterminal_until_execution_stop_receipt():
    transport = PiTransport()
    adapter = PiSecretaryDelegationAdapter(
        PiSecretaryBinding(room_id="room_1", agent_id="agent_1"), transport)
    svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    result = svc.cancel_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert result.status == "running" and result.cancel_requested and result.cancel_dispatched
    assert svc.get_delegation(
        delegation.delegation_id, tenant_id="t1", user_id="u1").status == "running"
    transport.task["execution_stopped"] = True
    assert svc.get_delegation(
        delegation.delegation_id, tenant_id="t1", user_id="u1").status == "cancelled"


def test_adapter_failure_stays_queued_and_never_echoes_private_error():
    adapter = Adapter(); adapter.fail_submit = True
    svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    assert delegation.status == "queued" and delegation.external_id == ""
    assert delegation.error == "runtime_unavailable"
    assert "private connector" not in str(delegation.as_dict())
    adapter.fail_submit = False; svc.maintain_delegations()
    assert delegation.external_id == "grant_task1"


def test_target_is_configured_and_context_is_bounded():
    svc = service(Adapter(), targets={"pi": {"role_ref": None}}); assistant = root(svc)
    with pytest.raises(AssistantError, match="target"):
        svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                     task="T", target_system="pi-secretary")
    with pytest.raises(AssistantError, match="context"):
        svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                     task="T", context={"too_much": "x" * 9000})
    with pytest.raises(ValueError, match="reviewed Secretary role"):
        assistant_config({"personal_assistant": {"enabled": True, "delegation": {"targets": {
            "pi-secretary": {"role_ref": {**ROLE, "digest": "sha256:" + "0" * 64}},
        }}}})
    for context in ({"connectors": ["gmail"]}, {"note": "Authorization: Bearer secret123"}):
        with pytest.raises(AssistantError, match="context"):
            svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                         task="T", context=context)
    with pytest.raises(AssistantError, match="source_refs"):
        svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                     task="T", source_refs=["token=private-secret"])


def test_existing_server_sweep_advances_injected_delegation_adapter():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    adapter.state = "done"
    server = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc)
    server.sweep_once()
    assert svc.store.get_delegation(
        delegation.delegation_id, tenant_id="t1", user_id="u1").status == "succeeded"


def test_existing_server_constructor_fails_closed_without_durable_adapter_store():
    adapter = Adapter()
    with pytest.raises(ValueError, match="acknowledged assistant persistence"):
        ControlPlaneServer(
            ControlPlaneRepo(),
            assistant_config={"personal_assistant": {
                "enabled": True, "delegation": {
                    "targets": {"pi-secretary": {"role_ref": ROLE}},
                    "binding_scope": {"tenant_id": "t1", "user_id": "u1"},
                }}},
            assistant_delegation_adapter=adapter,
        )


def test_existing_service_drives_actual_pi_secretary_adapter_contract():
    transport = PiTransport()
    adapter = PiSecretaryDelegationAdapter(
        PiSecretaryBinding(room_id="room_1", agent_id="agent_1"), transport)
    svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="Return only the bounded clarification.",
                              target_system="pi-secretary", idempotency_key="actual-adapter-1")
    assert delegation.external_id == "grant_real_adapter" and delegation.status == "queued"
    transport.task.update(status="done", result="Which city should I search near?")
    result = svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert result.status == "succeeded"
    assert result.result_text == "Which city should I search near?"
    assert result.artifact_refs == ["pi-task://grant_real_adapter"]


def test_fixed_adapter_is_single_scope_and_resolver_can_select_per_owner():
    adapter = Adapter(); svc = service(adapter)
    first = root(svc)
    other = svc.create_assistant(tenant_id="t2", user_id="u2", name="Other")
    own = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=first.assistant_id,
                       task="own", target_system="pi-secretary")
    foreign = svc.delegate(tenant_id="t2", user_id="u2", assistant_id=other.assistant_id,
                           task="foreign", target_system="pi-secretary")
    assert own.external_id == "grant_task1"
    assert foreign.external_id == "" and foreign.status == "queued"
    assert len(adapter.submitted) == 1

    a1, a2 = Adapter(), Adapter()
    def resolver(tenant, user, _target):
        return {("t1", "u1"): a1, ("t2", "u2"): a2}.get((tenant, user))
    cfg = assistant_config({"personal_assistant": {"enabled": True, "delegation": {"targets": {
        "pi-secretary": {"role_ref": ROLE},
    }}}})
    store = AssistantStore(persist_backend=DurableBackend())
    resolved = AssistantService(config=cfg, store=store, delegation_adapter_resolver=resolver)
    r1 = root(resolved); r2 = resolved.create_assistant(tenant_id="t2", user_id="u2", name="Other")
    resolved.delegate(tenant_id="t1", user_id="u1", assistant_id=r1.assistant_id,
                      task="one", target_system="pi-secretary")
    resolved.delegate(tenant_id="t2", user_id="u2", assistant_id=r2.assistant_id,
                      task="two", target_system="pi-secretary")
    assert len(a1.submitted) == 1 and len(a2.submitted) == 1


def test_atomic_idempotency_and_required_persistence_precede_external_submit():
    adapter = Adapter(); backend = DurableBackend()
    svc = service(adapter, store=AssistantStore(persist_backend=backend)); assistant = root(svc)
    args = dict(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                task="T", target_system="pi-secretary", idempotency_key="atomic-1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _n: svc.delegate(**args), range(8)))
    assert len({row.delegation_id for row in rows}) == 1
    assert len(adapter.submitted) == 1

    failing_adapter, failing_backend = Adapter(), DurableBackend()
    failing_backend.fail_required = True
    failing = service(failing_adapter, store=AssistantStore(persist_backend=failing_backend))
    root_assistant = root(failing)
    with pytest.raises(AssistantError, match="persistence"):
        failing.delegate(tenant_id="t1", user_id="u1", assistant_id=root_assistant.assistant_id,
                         task="T", target_system="pi-secretary", idempotency_key="persist-1")
    assert failing_adapter.submitted == [] and failing.store.delegations == {}

    resolver_adapter, resolver_backend = Adapter(), DurableBackend()
    resolver_backend.fail_required = True
    enabled = {"value": False}
    def delayed_resolver(_tenant, _user, _target):
        return resolver_adapter if enabled["value"] else None
    cfg = assistant_config({"personal_assistant": {"enabled": True, "delegation": {"targets": {
        "pi-secretary": {"role_ref": ROLE},
    }}}})
    delayed = AssistantService(
        config=cfg, store=AssistantStore(persist_backend=resolver_backend),
        delegation_adapter_resolver=delayed_resolver)
    delayed_root = root(delayed)
    with pytest.raises(AssistantError, match="persistence"):
        delayed.delegate(tenant_id="t1", user_id="u1", assistant_id=delayed_root.assistant_id,
                         task="T", target_system="pi-secretary", idempotency_key="delayed-1")
    enabled["value"] = True
    delayed.maintain_delegations()
    assert resolver_adapter.submitted == [] and delayed.store.delegations == {}


def test_restart_recovers_reserved_external_id_without_resubmit():
    adapter, backend = Adapter(), DurableBackend()
    svc = service(adapter, store=AssistantStore(persist_backend=backend)); assistant = root(svc)
    args = dict(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                task="T", target_system="pi-secretary", idempotency_key="restart-1")
    first = svc.delegate(**args)
    restored_store = AssistantStore(persist_backend=backend); restored_store.load_persisted()
    restored = service(adapter, store=restored_store)
    replay = restored.delegate(**args)
    assert replay.delegation_id == first.delegation_id and replay.external_id == "grant_task1"
    assert len(adapter.submitted) == 1


def test_concurrent_maintenance_cannot_regress_terminal_receipt():
    adapter = RacingAdapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _n: svc.get_delegation(
            delegation.delegation_id, tenant_id="t1", user_id="u1"), range(2)))
    result = svc.store.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert result.status == "succeeded" and result.result_text == "complete"


def test_invalid_runtime_receipt_fails_once_instead_of_retrying_forever():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    adapter.poll = lambda _external: {"external_id": "grant_task1", "status": "invented"}
    result = svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert result.status == "failed" and result.error == "runtime_contract_invalid"
    revision = result.revision
    assert svc.get_delegation(
        delegation.delegation_id, tenant_id="t1", user_id="u1").revision == revision


def test_adapter_bound_artifacts_require_runtime_receipt():
    adapter = Adapter(); svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    with pytest.raises(AssistantError, match="adapter owns"):
        svc.register_delegation_artifact(
            delegation.delegation_id, tenant_id="t1", user_id="u1", artifact_ref="artifact://forged")


def test_cancel_recovers_lost_submit_receipt_before_requesting_stop():
    adapter = Adapter(); adapter.fail_submit = True
    svc = service(adapter); assistant = root(svc)
    delegation = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id,
                              task="T", target_system="pi-secretary")
    first = svc.cancel_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert first.status == "queued" and first.cancel_requested and first.external_id == ""
    adapter.fail_submit = False
    recovered = svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert recovered.external_id == "grant_task1" and recovered.status == "queued"
    stopped = svc.get_delegation(delegation.delegation_id, tenant_id="t1", user_id="u1")
    assert stopped.status == "cancelled"


class HcnClient:
    """Models the owner's API surface: task cancel is logical; only a host report makes a runtime terminal."""

    def __init__(self, agent_id="agent_pi"):
        self.agent_id = agent_id
        self.task = {"task_id": "grant_t1", "target_agent_id": agent_id, "status": "accepted", "result": None}
        self.other_tasks = []
        self.runtimes = [{"runtime_id": "rt1", "agent_id": agent_id, "status": "ready", "cancel_requested": False}]
        self.runtime_cancels = []

    def task_create(self, room_id, agent_id, text, key):
        return {"task_id": "grant_t1", "status": "pending"}

    def task_get(self, room_id, task_id):
        return dict(self.task)

    def task_cancel(self, room_id, task_id):
        self.task["status"] = "cancelled"
        return {"ok": True}

    def tasks_list(self, room_id):
        return {"tasks": [self.task, *self.other_tasks]}

    def runtimes_list(self, room_id):
        return {"runtimes": [dict(r) for r in self.runtimes]}

    def runtime_cancel(self, room_id, runtime_id):
        self.runtime_cancels.append(runtime_id)
        for r in self.runtimes:
            if r["runtime_id"] == runtime_id:
                r["cancel_requested"] = True
        return {"runtime": {}}


def hcn_adapter(client):
    from herdr_engineering.assistant_pi_delegate import HcnPiTaskTransport
    binding = PiSecretaryBinding(room_id="room_1", agent_id=client.agent_id)
    return PiSecretaryDelegationAdapter(binding, HcnPiTaskTransport(client, client.agent_id))


def test_hcn_transport_cancel_is_not_terminal_until_the_host_reports_the_runtime_stopped():
    client = HcnClient()
    adapter = hcn_adapter(client)
    assert adapter.cancel("grant_t1")["status"] == "cancellation_requested"
    assert client.runtime_cancels == ["rt1"]
    polled = adapter.poll("grant_t1")
    assert polled["status"] == "running" and polled["cancel_requested"] is True      # logical cancel is not a stop
    client.runtimes[0]["status"] = "cancelled"                                       # host reports verified teardown
    assert adapter.poll("grant_t1")["status"] == "cancelled"


def test_hcn_transport_never_infers_a_stop_from_a_missing_or_live_runtime():
    client = HcnClient()
    adapter = hcn_adapter(client)
    adapter.cancel("grant_t1")
    client.runtimes = []
    assert adapter.poll("grant_t1")["status"] == "running"                           # no runtime evidence: not stopped
    client.runtimes = [{"runtime_id": "rt1", "agent_id": client.agent_id, "status": "ready"},
                       {"runtime_id": "rt2", "agent_id": client.agent_id, "status": "cancelled"}]
    assert adapter.poll("grant_t1")["status"] == "running"                           # one still live
    client.runtimes = [{"runtime_id": "rt9", "agent_id": "agent_other", "status": "cancelled"}]
    assert adapter.poll("grant_t1")["status"] == "running"                           # another agent's runtime is ignored


def test_hcn_transport_does_not_stop_a_runtime_that_serves_other_open_tasks():
    client = HcnClient()
    client.other_tasks = [{"task_id": "grant_t2", "target_agent_id": client.agent_id, "status": "pending"}]
    adapter = hcn_adapter(client)
    adapter.cancel("grant_t1")
    assert client.runtime_cancels == []                                              # would fail the other delegation
    assert adapter.poll("grant_t1")["status"] == "running"


def test_hcn_transport_rejects_a_foreign_agent_or_task_response():
    from herdr_engineering.assistant_pi_delegate import HcnPiTaskTransport, PiDelegationError
    client = HcnClient()
    transport = HcnPiTaskTransport(client, client.agent_id)
    with pytest.raises(PiDelegationError):
        transport.create_task(room_id="room_1", agent_id="agent_other", text="x", idempotency_key="k")
    client.task["target_agent_id"] = "agent_other"
    with pytest.raises(PiDelegationError):
        transport.get_task(room_id="room_1", task_id="grant_t1")


def _binding(tmp_path, **over):
    cred = tmp_path / "cred"
    cred.write_text("hcu_token\n")
    cred.chmod(0o600)
    row = {"tenant_id": "t1", "user_id": "u1", "room_id": "room_1", "agent_id": "agent_pi",
           "api_url": "http://127.0.0.1:8080", "credential_file": str(cred)}
    return {**row, **over}


def test_pi_bindings_config_is_strict_and_unique(tmp_path):
    cfg = lambda rows: assistant_config({"personal_assistant": {"enabled": True, "delegation": {"pi_bindings": rows}}})
    assert cfg(None)["delegation"]["pi_bindings"] == []
    assert len(cfg([_binding(tmp_path)])["delegation"]["pi_bindings"]) == 1
    with pytest.raises(ValueError):
        cfg([_binding(tmp_path), _binding(tmp_path, room_id="room_2")])         # one binding per tenant+user
    with pytest.raises(ValueError):
        cfg([{**_binding(tmp_path), "token": "inline-secret"}])                 # no inline credentials
    bad = _binding(tmp_path)
    del bad["agent_id"]
    with pytest.raises(ValueError):
        cfg([bad])


def test_resolver_matches_exact_scope_target_and_fails_closed_on_a_bad_credential(tmp_path):
    from herdr_engineering.assistant_pi_delegate import build_pi_delegation_resolver
    row = _binding(tmp_path)
    resolve = build_pi_delegation_resolver([row])
    adapter = resolve("t1", "u1", "pi-secretary")
    assert adapter is not None and adapter.binding.agent_id == "agent_pi" and adapter.binding.room_id == "room_1"
    assert resolve("t1", "u1", "pi-secretary") is adapter
    assert resolve("t1", "u2", "pi-secretary") is None                          # another user never inherits it
    assert resolve("t2", "u1", "pi-secretary") is None
    assert resolve("t1", "u1", "pi") is None
    cred = tmp_path / "cred"
    cred.chmod(0o644)                                                           # group/world readable: refused
    assert build_pi_delegation_resolver([row])("t1", "u1", "pi-secretary") is None
    missing = build_pi_delegation_resolver([{**row, "credential_file": str(tmp_path / "absent")}])
    assert missing("t1", "u1", "pi-secretary") is None


def _bound_section(tmp_path):
    return {"enabled": True, "delegation": {
        "targets": {"pi": {"role_ref": None}, "pi-secretary": {"role_ref": ROLE}},
        "pi_bindings": [_binding(tmp_path)]}}


def test_normal_startup_with_bindings_requires_acknowledged_persistence(tmp_path, acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)                             # no personal_assistant.persistence.dsn
    with pytest.raises(ValueError, match="acknowledged assistant persistence"):
        ControlPlaneServer(repo, assistant_config={"personal_assistant": _bound_section(tmp_path)}, api_keys={})
    with pytest.raises(ValueError, match="not both"):
        ControlPlaneServer(repo, assistant_config={"personal_assistant": _bound_section(tmp_path)}, api_keys={},
                           assistant_delegation_adapter=Adapter())


def test_service_accepts_the_operator_resolver_with_durable_persistence(tmp_path):
    from herdr_engineering.assistant_pi_delegate import build_pi_delegation_resolver
    cfg = assistant_config({"personal_assistant": _bound_section(tmp_path)})
    resolver = build_pi_delegation_resolver(cfg["delegation"]["pi_bindings"])
    svc = AssistantService(config=cfg, store=AssistantStore(persist_backend=DurableBackend()),
                           delegation_adapter_resolver=resolver)
    assert svc.delegation_adapter_resolver("t1", "u1", "pi-secretary").binding.agent_id == "agent_pi"
    assert svc.delegation_adapter_resolver("t1", "someone-else", "pi-secretary") is None


@pytest.mark.parametrize("with_adapter", [True, False])
def test_a_failing_created_audit_leaves_no_orphan_and_submits_nothing(with_adapter):
    adapter = Adapter() if with_adapter else None
    svc = service(adapter); assistant = root(svc)
    real = svc.store.audit_record

    def audit(**kw):
        if kw.get("action") == "assistant.delegation.created":
            raise OSError("audit unavailable")
        return real(**kw)
    svc.store.audit_record = audit
    with pytest.raises(OSError):
        svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id, task="Find a time.",
                     target_system="pi-secretary", context={}, source_refs=[], run_id="",
                     idempotency_key="audit-fail-1", actor="u1")
    assert not svc.store.delegations and not svc.store._delegation_idempotency
    assert adapter is None or adapter.submitted == []
    svc.store.audit_record = real
    again = svc.delegate(tenant_id="t1", user_id="u1", assistant_id=assistant.assistant_id, task="Find a time.",
                         target_system="pi-secretary", context={}, source_refs=[], run_id="",
                         idempotency_key="audit-fail-1", actor="u1")
    assert again.status == "queued" and (adapter is None or len(adapter.submitted) == 1)
