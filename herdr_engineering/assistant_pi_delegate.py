"""Host-side adapter from the canonical Personal Assistant to a scoped Pi task target."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
SHA = re.compile(r"sha256:[a-f0-9]{64}\Z")
ROLE_ID = "procedure:secretary-system"
ROLE_VERSION = "1.0.0"
ROLE_DIGEST = "sha256:942e5b1002d74a9d52ae2fb7c2f8ef07f4f6b08aa1b9e08a36b8e0504ffab120"


class PiDelegationError(RuntimeError):
    pass


class PiTaskTransport(Protocol):
    def create_task(self, *, room_id: str, agent_id: str, text: str,
                    idempotency_key: str) -> dict: ...
    def get_task(self, *, room_id: str, task_id: str) -> dict: ...
    def cancel_task(self, *, room_id: str, task_id: str) -> dict: ...


@dataclass(frozen=True)
class PiSecretaryBinding:
    room_id: str
    agent_id: str
    target_system: str = "pi-secretary"
    role_id: str = ROLE_ID
    role_version: str = ROLE_VERSION
    role_digest: str = ROLE_DIGEST

    def __post_init__(self):
        if (not REF.fullmatch(self.room_id) or not REF.fullmatch(self.agent_id)
                or self.target_system != "pi-secretary" or self.role_id != ROLE_ID
                or self.role_version != ROLE_VERSION or not SHA.fullmatch(self.role_digest)
                or self.role_digest != ROLE_DIGEST):
            raise PiDelegationError("invalid Pi Secretary binding")


class PiSecretaryDelegationAdapter:
    """No authority of its own; transport is injected by the authenticated root service."""

    def __init__(self, binding: PiSecretaryBinding, transport: PiTaskTransport):
        self.binding = binding
        self.transport = transport

    @staticmethod
    def _delegation(value) -> tuple[str, str, str, dict, list[str]]:
        delegation_id = getattr(value, "delegation_id", None)
        target = getattr(value, "target_system", None)
        task = getattr(value, "task", None)
        role_ref = getattr(value, "role_ref", None)
        context = getattr(value, "context", None)
        source_refs = getattr(value, "source_refs", None)
        if not REF.fullmatch(delegation_id or "") or target != "pi-secretary":
            raise PiDelegationError("unsupported delegation target")
        if role_ref != {"id": ROLE_ID, "version": ROLE_VERSION, "digest": ROLE_DIGEST}:
            raise PiDelegationError("Secretary role binding mismatch")
        if not isinstance(task, str) or not task.strip() or len(task) > 8000:
            raise PiDelegationError("invalid delegation task")
        if not isinstance(context, dict) or not isinstance(source_refs, list):
            raise PiDelegationError("invalid delegation context")
        if any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in source_refs):
            raise PiDelegationError("invalid delegation source reference")
        return delegation_id, target, task.strip(), context, list(source_refs)

    def submit(self, delegation) -> dict:
        delegation_id, _, task, context, source_refs = self._delegation(delegation)
        key = "pa-" + hashlib.sha256(delegation_id.encode()).hexdigest()[:32]
        prompt = json.dumps({"schema_version": 1, "task": task, "context": context,
                             "source_refs": source_refs}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
        if len(prompt.encode()) > 4000:
            raise PiDelegationError("delegation payload exceeds Pi task limit")
        value = self.transport.create_task(
            room_id=self.binding.room_id, agent_id=self.binding.agent_id,
            text=prompt, idempotency_key=key)
        task_id = value.get("task_id")
        if not REF.fullmatch(task_id or "") or value.get("status") != "pending":
            raise PiDelegationError("invalid Pi task receipt")
        return {"external_id": task_id, "status": "queued",
                "artifact_refs": [f"pi-task://{task_id}"]}

    def poll(self, external_id: str) -> dict:
        if not REF.fullmatch(external_id or ""):
            raise PiDelegationError("invalid external task")
        task = self.transport.get_task(room_id=self.binding.room_id, task_id=external_id)
        if task.get("task_id") != external_id:
            raise PiDelegationError("invalid Pi task response")
        state = task.get("status")
        mapped = {"pending": "queued", "accepted": "running", "done": "done",
                  "failed": "failed"}.get(state)
        if state == "cancelled":
            mapped = "cancelled" if task.get("execution_stopped") is True else "running"
        if mapped is None:
            raise PiDelegationError("unknown Pi task state")
        result = task.get("result")
        if result is not None and (not isinstance(result, str) or len(result) > 4000):
            raise PiDelegationError("invalid Pi task result")
        return {"external_id": external_id, "status": mapped,
                "cancel_requested": state == "cancelled" and mapped != "cancelled",
                "result_text": result, "artifact_refs": [f"pi-task://{external_id}"]}

    def cancel(self, external_id: str) -> dict:
        if not REF.fullmatch(external_id or ""):
            raise PiDelegationError("invalid external task")
        value = self.transport.cancel_task(room_id=self.binding.room_id, task_id=external_id)
        if value.get("ok") is not True:
            raise PiDelegationError("Pi task cancellation failed")
        return {"external_id": external_id, "status": "cancellation_requested",
                "cancel_requested": True}


_RUNTIME_LIVE = frozenset({"queued", "claimed", "starting", "ready"})
_RUNTIME_STOPPED = frozenset({"failed", "cancelled"})
_TASK_OPEN = frozenset({"pending", "accepted"})


class HcnPiTaskTransport:
    """``PiTaskTransport`` over the owner's own authenticated client; it adds no authority.

    Cancelling a task only withdraws the logical task. ``execution_stopped`` is reported only after the host has
    reported every runtime of the bound agent failed/cancelled, which the runner does after verified process and
    scope teardown. A missing or still-live runtime is never read as stopped."""

    def __init__(self, client, agent_id: str):
        if not REF.fullmatch(agent_id or ""):
            raise PiDelegationError("invalid Pi Secretary binding")
        self.client, self.agent_id = client, agent_id

    def create_task(self, *, room_id: str, agent_id: str, text: str, idempotency_key: str) -> dict:
        if agent_id != self.agent_id:
            raise PiDelegationError("transport is bound to a different agent")
        return self.client.task_create(room_id, agent_id, text, idempotency_key)

    def get_task(self, *, room_id: str, task_id: str) -> dict:
        task = dict(self.client.task_get(room_id, task_id))
        if task.get("target_agent_id") not in (None, self.agent_id):
            raise PiDelegationError("invalid Pi task response")
        if task.get("status") == "cancelled":
            task["execution_stopped"] = self._execution_stopped(room_id)
        return task

    def cancel_task(self, *, room_id: str, task_id: str) -> dict:
        value = self.client.task_cancel(room_id, task_id)
        if value.get("ok") is not True:
            return value
        # Withdrawing the task does not stop Pi. Ask the host to stop the bound runtime, but only when no other
        # open task of this agent would be failed by it; otherwise leave execution_stopped false (fail closed).
        if not self._other_open_tasks(room_id, task_id):
            for runtime in self._runtimes(room_id):
                if runtime.get("status") in _RUNTIME_LIVE and not runtime.get("cancel_requested"):
                    self.client.runtime_cancel(room_id, runtime["runtime_id"])
        return value

    def _runtimes(self, room_id: str) -> list[dict]:
        rows = self.client.runtimes_list(room_id).get("runtimes")
        if not isinstance(rows, list):
            raise PiDelegationError("invalid runtime listing")
        return [r for r in rows if isinstance(r, dict) and r.get("agent_id") == self.agent_id]

    def _other_open_tasks(self, room_id: str, task_id: str) -> bool:
        rows = self.client.tasks_list(room_id).get("tasks")
        if not isinstance(rows, list):
            raise PiDelegationError("invalid task listing")
        return any(t.get("target_agent_id") == self.agent_id and t.get("task_id") != task_id
                   and t.get("status") in _TASK_OPEN for t in rows if isinstance(t, dict))

    def _execution_stopped(self, room_id: str) -> bool:
        runtimes = self._runtimes(room_id)
        return bool(runtimes) and all(r.get("status") in _RUNTIME_STOPPED for r in runtimes)


def build_pi_delegation_resolver(bindings: list[dict[str, str]]):
    """Resolver for ``AssistantService`` from operator-owned ``delegation.pi_bindings``.

    Exact (tenant, user, "pi-secretary") match only; any other scope, target or an unreadable credential resolves
    to None, so delegation stays unavailable instead of falling back to another user's binding. The credential file
    is read when the adapter is first needed, never kept in configuration."""
    from .assistant_cli import AssistantClient, AssistantConfig, ConfigError, _read_token_file
    from pathlib import Path

    by_scope = {(b["tenant_id"], b["user_id"]): b for b in bindings}
    cache: dict[tuple[str, str], PiSecretaryDelegationAdapter] = {}

    def resolve(tenant_id: str, user_id: str, target: str):
        entry = by_scope.get((tenant_id, user_id))
        if entry is None or target != "pi-secretary":
            return None
        key = (tenant_id, user_id)
        if key not in cache:
            try:
                config = AssistantConfig(
                    base_url=entry["api_url"], auth_kind="user",
                    token=_read_token_file(Path(entry["credential_file"]).expanduser()))
                binding = PiSecretaryBinding(room_id=entry["room_id"], agent_id=entry["agent_id"])
            except (ConfigError, PiDelegationError):
                return None
            cache[key] = PiSecretaryDelegationAdapter(
                binding, HcnPiTaskTransport(AssistantClient(config), entry["agent_id"]))
        return cache[key]

    return resolve
