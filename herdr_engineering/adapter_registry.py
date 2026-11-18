"""Shared launch-adapter boundary for every Herdr-controlled agent family."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

SUPPORTED_FAMILIES = frozenset({"pi", "claude-code", "codex", "agent"})


def register_native_families(registry, adapter, *, resolve_host, resolve_workspace):
    """Register actual native launch callbacks for prepared, owner-scoped work."""
    def launch(host_uid, request):
        route = resolve_host(host_uid)
        workspace = resolve_workspace(request.workspace_id, host_uid)
        if not isinstance(route, dict) or route.get("host_uid") != host_uid:
            raise RuntimeError("native host routing identity is unavailable")
        if (not isinstance(workspace, dict) or workspace.get("ready") is not True
                or workspace.get("owner") != request.owner_id
                or workspace.get("host_uid") != host_uid):
            raise RuntimeError("prepared workspace ownership is unavailable")
        name = "scheduler-" + hashlib.sha256(request.request_id.encode()).hexdigest()[:24]
        return adapter.launch_agent(name=name, family=request.agent_family,
                                    cwd=workspace.get("path"), machine=route.get("machine"),
                                    session=route.get("session"))
    for family in ("pi", "claude-code", "codex"):
        registry.register(family, launch)
    return registry


def native_machine_launcher(adapter, command_builder: Callable):
    """Bridge a registry launch to NativeHerdrAdapter machine forwarding.

    The command builder is deliberately supplied by the family adapter; this
    helper never turns task text into shell arguments.
    """
    if not callable(command_builder):
        raise TypeError("command_builder must be callable")

    def launch(host_uid: str, request):
        machine = adapter.resolve_machine(host_uid)
        if machine is None:
            raise RuntimeError("scheduler host is not a saved Herdr machine")
        command = command_builder(request)
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            raise ValueError("native command builder must return a non-empty argv list")
        if any("\x00" in arg or "\n" in arg or "\r" in arg for arg in command):
            raise ValueError("native command contains control characters")
        return adapter.machine_targeted(machine.label, command)

    return launch


@dataclass(frozen=True)
class LaunchResult:
    family: str
    request_id: str
    host_uid: str
    reservation_id: str
    lease_id: str | None
    native_result: object


class AdapterRegistry:
    """Admit once, then delegate physical launch to the existing native adapter."""

    def __init__(self, scheduler_service):
        self.scheduler = scheduler_service
        self._launchers: dict[str, Callable] = {}

    def register(self, family: str, launcher: Callable) -> None:
        if family not in SUPPORTED_FAMILIES:
            raise ValueError("unsupported agent family")
        if not callable(launcher):
            raise TypeError("launcher must be callable")
        if family in self._launchers:
            raise ValueError("agent family already registered")
        self._launchers[family] = launcher

    def launch(self, body: dict, principal: str) -> LaunchResult:
        family = body.get("agent_family")
        launcher = self._launchers.get(family)
        if launcher is None:
            raise ValueError("agent family has no registered launcher")
        # parent_agent_id is lineage only. No parent_host/host inheritance is
        # copied into the placement request.
        request = self.scheduler.request(body, principal)
        reservation = self.scheduler.reserve(body, principal)
        if reservation.get("state") == "committed":
            raise RuntimeError("execution requires reconciliation before retry")
        if reservation.get("state") != "reserved":
            raise RuntimeError("placement is queued")
        committed = self.scheduler.commit(reservation["reservation_id"])
        self.scheduler.claim_execution(reservation["reservation_id"])
        # A transport error after submission cannot prove the remote process
        # stopped. Preserve the committed reservation and its capacity until
        # native execution is reconciled; retrying must not launch again.
        native_result = launcher(committed["host_uid"], request)
        return LaunchResult(
            family=family, request_id=request.request_id,
            host_uid=committed["host_uid"], reservation_id=committed["reservation_id"],
            lease_id=committed.get("lease_id"), native_result=native_result,
        )
