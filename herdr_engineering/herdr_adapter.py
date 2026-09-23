"""NativeHerdrAdapter — typed boundary over the real Herdr CLI/socket API.

Higher phases depend on this adapter, never on raw terminal scraping. It
normalizes Herdr state without duplicating it and exposes capabilities so the
UI can degrade rather than assume every version/plugin supports every action.

Uses ``herdr api snapshot --json``, ``herdr machine list --json``,
``herdr plugin list --json``, ``herdr integration status`` and the
``--machine`` forwarding primitive. Structured output is parsed when
supported; otherwise the adapter records the raw text behind a capability.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    InvalidError,
    TimeoutError_Engineering,
    UnreachableError,
    UnsupportedError,
)


@dataclass
class Machine:
    id: str
    label: str
    target: str
    session: str | None = None
    enabled: bool = True
    selected: bool = False
    reachable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "target": self.target,
                "session": self.session, "enabled": self.enabled,
                "selected": self.selected, "reachable": self.reachable}


@dataclass
class AgentState:
    agent: str
    status: str
    cwd: str | None = None
    pane_id: str | None = None
    tab_id: str | None = None
    workspace_id: str | None = None
    terminal_id: str | None = None
    terminal_title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "status": self.status, "cwd": self.cwd,
                "pane_id": self.pane_id, "tab_id": self.tab_id,
                "workspace_id": self.workspace_id, "terminal_id": self.terminal_id,
                "terminal_title": self.terminal_title}


@dataclass
class Workspace:
    workspace_id: str
    label: str
    agent_status: str | None = None
    number: int | None = None
    tab_count: int | None = None
    pane_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"workspace_id": self.workspace_id, "label": self.label,
                "agent_status": self.agent_status, "number": self.number,
                "tab_count": self.tab_count, "pane_count": self.pane_count}


@dataclass
class HerdrCapabilities:
    version: str = ""
    protocol: int | None = None
    schema_version: int | None = None
    machines: bool = False
    snapshot: bool = False
    plugins: bool = False
    machine_forwarding: bool = False
    worktrees: bool = False
    integration_status: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "protocol": self.protocol,
                "schema_version": self.schema_version, "machines": self.machines,
                "snapshot": self.snapshot, "plugins": self.plugins,
                "machine_forwarding": self.machine_forwarding,
                "worktrees": self.worktrees,
                "integration_status": self.integration_status}


class NativeHerdrAdapter:
    """Adapter over the real ``herdr`` binary.

    ``runner`` is injectable for tests (recorded fixtures / mocks).
    """

    def __init__(self, herdr_bin: str = "herdr", runner=None) -> None:
        self.herdr_bin = herdr_bin
        self._runner = runner or _SubprocessRunner(herdr_bin)
        self._capabilities: HerdrCapabilities | None = None

    # -- low-level ---------------------------------------------------------
    def _run(self, args: list[str], timeout: int = 30) -> str:
        try:
            return self._runner.run(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError_Engineering("herdr command timed out",
                                           target=" ".join(args))
        except FileNotFoundError:
            raise UnreachableError("herdr binary not found", target=self.herdr_bin)

    def _run_json(self, args: list[str], timeout: int = 30) -> Any:
        out = self._run(args, timeout=timeout).strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise InvalidError(f"expected JSON from {args!r}, got: {out[:200]}")

    # -- capabilities ------------------------------------------------------
    def capabilities(self, refresh: bool = False) -> HerdrCapabilities:
        if self._capabilities is not None and not refresh:
            return self._capabilities
        caps = HerdrCapabilities()
        try:
            caps.version = self._run(["--version"]).strip()
        except Exception:
            pass
        try:
            schema = self._run_json(["api", "schema", "--json"])
            caps.protocol = schema.get("protocol")
            caps.schema_version = schema.get("schema_version")
            caps.raw["schema"] = schema
        except Exception:
            pass
        try:
            machines = self._run_json(["machine", "list", "--json"])
            caps.machines = isinstance(machines, list)
        except Exception:
            pass
        try:
            snap = self._run(["api", "snapshot"])
            caps.snapshot = bool(snap.strip())
        except Exception:
            pass
        try:
            plugins = self._run_json(["plugin", "list", "--json"])
            caps.plugins = "plugins" in str(plugins)
        except Exception:
            pass
        try:
            self._run(["integration", "status"])
            caps.integration_status = True
        except Exception:
            pass
        try:
            self._run(["worktree", "list"])
            caps.worktrees = True
        except Exception:
            pass
        caps.machine_forwarding = caps.machines  # --machine requires saved machines
        self._capabilities = caps
        return caps

    # -- machines ----------------------------------------------------------
    def list_machines(self) -> list[Machine]:
        data = self._run_json(["machine", "list", "--json"])
        machines = []
        for item in data if isinstance(data, list) else []:
            machines.append(Machine(
                id=item.get("id", ""),
                label=item.get("label", ""),
                target=item.get("target", item.get("label", "")),
                session=item.get("session"),
                enabled=item.get("enabled", True),
                selected=item.get("selected", False)))
        return machines

    def resolve_machine(self, label_or_id: str) -> Machine | None:
        for m in self.list_machines():
            if m.label == label_or_id or m.id == label_or_id:
                return m
        return None

    def machine_targeted(self, label: str, command: list[str], timeout: int = 60) -> str:
        """Run a command on a saved SSH machine via ``--machine`` forwarding."""
        return self._run(["--machine", label] + command, timeout=timeout)

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return self._run_json(["api", "snapshot"])

    def list_agents(self) -> list[AgentState]:
        snap = self.snapshot()
        agents = []
        for a in snap.get("result", {}).get("snapshot", {}).get("agents", []):
            agents.append(AgentState(
                agent=a.get("agent", ""),
                status=a.get("agent_status", "unknown"),
                cwd=a.get("cwd"),
                pane_id=a.get("pane_id"),
                tab_id=a.get("tab_id"),
                workspace_id=a.get("workspace_id"),
                terminal_id=a.get("terminal_id"),
                terminal_title=a.get("terminal_title")))
        return agents

    def list_workspaces(self) -> list[Workspace]:
        snap = self.snapshot()
        workspaces = []
        for w in snap.get("result", {}).get("snapshot", {}).get("workspaces", []):
            workspaces.append(Workspace(
                workspace_id=w.get("workspace_id", ""),
                label=w.get("label", ""),
                agent_status=w.get("agent_status"),
                number=w.get("number"),
                tab_count=w.get("tab_count"),
                pane_count=w.get("pane_count")))
        return workspaces

    # -- plugins / integration ----------------------------------------------
    def list_plugins(self) -> list[dict[str, Any]]:
        data = self._run_json(["plugin", "list", "--json"])
        plugins = data.get("result", {}).get("plugins", []) if isinstance(data, dict) else []
        return plugins if isinstance(plugins, list) else []

    def integration_status(self) -> str:
        return self._run(["integration", "status"])

    # -- worktrees ----------------------------------------------------------
    def list_worktrees(self) -> list[dict[str, Any]]:
        try:
            out = self._run(["worktree", "list", "--json"])
            return json.loads(out) if isinstance(out, str) and out.strip() else []
        except Exception:
            raise UnsupportedError("worktree list not supported by this Herdr version")

    def create_worktree(self, path: str, branch: str) -> dict[str, Any]:
        out = self._run(["worktree", "add", path, "-b", branch])
        return {"path": path, "branch": branch, "output": out}

    # -- input ---------------------------------------------------------------
    def send_input(self, session_or_pane: str, text: str, machine: str | None = None) -> str:
        """Send permitted input to a session/pane. Explicit and auditable."""
        args = ["--session", session_or_pane] if not session_or_pane.startswith(("w", "t")) else []
        if machine:
            return self.machine_targeted(machine, ["--session", session_or_pane, text])
        return self._run(args + ["type", text])


class _SubprocessRunner:
    def __init__(self, herdr_bin: str) -> None:
        self.herdr_bin = herdr_bin

    def run(self, args: list[str], timeout: int = 30) -> str:
        proc = subprocess.run(
            [self.herdr_bin] + args, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise InvalidError(f"herdr {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout
