import json
from types import SimpleNamespace

import pytest

from herdr_engineering.assistant_pi_delegate import (
    ROLE_DIGEST,
    PiDelegationError,
    PiSecretaryBinding,
    PiSecretaryDelegationAdapter,
)


class Transport:
    def __init__(self):
        self.created = []; self.tasks = {}; self.cancelled = []

    def create_task(self, **value):
        self.created.append(value)
        task = {"task_id": "grant_task1", "status": "pending", "result": None}
        self.tasks[task["task_id"]] = task
        return task

    def get_task(self, **value):
        return dict(self.tasks[value["task_id"]])

    def cancel_task(self, **value):
        self.cancelled.append(value); return {"ok": True}


def delegation(target="pi-secretary", text="Prepare a bounded summary"):
    return SimpleNamespace(
        delegation_id="dlg_123", target_system=target, task=text,
        role_ref={"id": "procedure:secretary-system", "version": "1.0.0", "digest": ROLE_DIGEST},
        context={}, source_refs=[])


def adapter():
    transport = Transport()
    return PiSecretaryDelegationAdapter(
        PiSecretaryBinding(room_id="room_1", agent_id="agent_1"), transport), transport


def test_submit_uses_fixed_target_and_idempotent_task_contract():
    subject, transport = adapter()
    result = subject.submit(delegation())
    assert result == {"external_id": "grant_task1", "status": "queued",
                      "artifact_refs": ["pi-task://grant_task1"]}
    sent = transport.created[0]
    assert sent["room_id"] == "room_1" and sent["agent_id"] == "agent_1"
    assert json.loads(sent["text"]) == {"schema_version": 1,
        "task": "Prepare a bounded summary", "context": {}, "source_refs": []}
    assert sent["idempotency_key"].startswith("pa-")
    assert set(sent) == {"room_id", "agent_id", "text", "idempotency_key"}


def test_binding_is_exact_and_role_never_becomes_authority():
    binding = PiSecretaryBinding(room_id="room_1", agent_id="agent_1")
    assert binding.role_digest == ROLE_DIGEST
    with pytest.raises(PiDelegationError):
        PiSecretaryBinding(room_id="room_1", agent_id="agent_1", role_digest="sha256:" + "0" * 64)
    with pytest.raises(PiDelegationError):
        adapter()[0].submit(delegation(target="pi"))
    wrong = delegation(); wrong.role_ref = {**wrong.role_ref, "digest": "sha256:" + "0" * 64}
    with pytest.raises(PiDelegationError, match="role binding"):
        adapter()[0].submit(wrong)


@pytest.mark.parametrize(("pi_state", "root_state"), [
    ("pending", "queued"), ("accepted", "running"), ("done", "done"),
    ("failed", "failed"),
])
def test_poll_maps_existing_task_states(pi_state, root_state):
    subject, transport = adapter(); subject.submit(delegation())
    transport.tasks["grant_task1"].update(status=pi_state, result="ready" if pi_state == "done" else None)
    result = subject.poll("grant_task1")
    assert result["status"] == root_state
    assert result["result_text"] == ("ready" if pi_state == "done" else None)


def test_cancel_is_task_scoped():
    subject, transport = adapter()
    assert subject.cancel("grant_task1") == {"external_id": "grant_task1",
        "status": "cancellation_requested", "cancel_requested": True}
    assert transport.cancelled == [{"room_id": "room_1", "task_id": "grant_task1"}]


def test_private_context_is_not_added_by_adapter():
    subject, transport = adapter()
    subject.submit(delegation(text="minimal context only"))
    assert json.loads(transport.created[0]["text"])["task"] == "minimal context only"
    assert "tenant" not in transport.created[0] and "user" not in transport.created[0]


def test_authorized_context_and_provenance_are_forwarded_as_data():
    subject, transport = adapter(); value = delegation()
    value.context = {"known": ["8 adults", "10 children"]}
    value.source_refs = ["event:request-1"]
    subject.submit(value)
    payload = json.loads(transport.created[0]["text"])
    assert payload["context"] == value.context and payload["source_refs"] == value.source_refs
    assert "user_id" not in payload and "credential" not in payload


def test_cancelled_task_requires_verified_execution_stop():
    subject, transport = adapter(); subject.submit(delegation())
    transport.tasks["grant_task1"].update(status="cancelled")
    result = subject.poll("grant_task1")
    assert result["status"] == "running" and result["cancel_requested"] is True
    transport.tasks["grant_task1"]["execution_stopped"] = True
    assert subject.poll("grant_task1")["status"] == "cancelled"
