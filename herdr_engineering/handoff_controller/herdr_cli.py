"""The controller's view of Herdr: the real `herdr` binary, one subprocess per call. No method raises."""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..handoff_adapters.base import Harness

_CALL_MARGIN_S = 5.0  # a herdr call gets its own timeout plus this much before the subprocess is abandoned
_QUICK_CALL_S = 15.0  # herdr calls that answer from memory


# Herdr's agent name per harness, the one place the controller and the drain's wrong-agent guard read. Measured on a
# live Herdr 0.9.1 (`herdr agent list`, 2026-11-18): `claude` and `pi`. ASSUMED, never seen: `codex` and `opencode`
# (Herdr names a pane's agent after the program it detects, as it does for the two measured ones).
_AGENT_NAMES: dict[str, Harness] = {"claude": Harness.CLAUDE, "pi": Harness.PI, "codex": Harness.CODEX,
                                    "opencode": Harness.OPENCODE}


def harness_for_agent_name(name: str | None) -> Harness | None:
    """The harness Herdr's agent name stands for, None for an agent the controller does not manage."""
    return _AGENT_NAMES.get(name) if name else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class PromptResult(StrEnum):
    SENT = "sent"
    BLOCKED = "blocked"  # herdr refused: the agent waits on a dialog, nothing was typed
    FAILED = "failed"


@dataclass(frozen=True)
class AgentInfo:
    name: str | None  # what Herdr detected in the pane: the harness name (claude, codex, pi, opencode) or None
    status: str | None


@dataclass(frozen=True)
class AgentPane:
    """One row of `herdr agent list`: an agent Herdr detected in a pane."""
    pane_id: str
    tab_id: str | None
    name: str | None
    status: str | None
    cwd: Path | None
    session: str | None  # agent_session.value: a conversation id (Claude, Codex) or a record path (Pi)


class HerdrCli:
    """`binary` defaults to `herdr` on PATH; `env` replaces the environment (e.g. HERDR_SOCKET_PATH of another server)."""

    def __init__(self, binary: str | None = None, env: Mapping[str, str] | None = None,
                 cwd: Path | None = None) -> None:
        self._cwd = cwd  # a relative HERDR_SOCKET_PATH in `env` is resolved from here
        self._binary = binary or shutil.which("herdr") or "herdr"
        self._env = dict(env) if env is not None else None

    def _run(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run([self._binary, *args], env=self._env, capture_output=True, text=True,
                                  timeout=timeout_s, check=False, cwd=self._cwd)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def agent(self, pane_id: str) -> AgentInfo | None:
        done = self._run(["agent", "get", pane_id], _QUICK_CALL_S)
        if done is None or done.returncode != 0:
            return None
        try:
            agent = json.loads(done.stdout)["result"]["agent"]
            name, status = agent.get("agent"), agent["agent_status"]
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        return AgentInfo(name if isinstance(name, str) else None, status if isinstance(status, str) else None)

    def agents(self) -> list[AgentPane] | None:
        """Every agent Herdr detects, or None when Herdr cannot be asked (an empty list means there are none)."""
        done = self._run(["agent", "list"], _QUICK_CALL_S)
        if done is None or done.returncode != 0:
            return None
        try:
            rows = json.loads(done.stdout)["result"]["agents"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(rows, list):
            return None
        found: list[AgentPane] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("pane_id"), str):
                continue
            session, cwd = row.get("agent_session"), row.get("cwd")
            value = session.get("value") if isinstance(session, dict) else None
            found.append(AgentPane(row["pane_id"], _text(row.get("tab_id")), _text(row.get("agent")),
                                   _text(row.get("agent_status")), Path(cwd) if isinstance(cwd, str) and cwd else None,
                                   _text(value)))
        return found

    def agent_status(self, pane_id: str) -> str | None:
        info = self.agent(pane_id)
        return info.status if info else None

    def wait_idle(self, pane_id: str, timeout_s: float) -> bool:
        """True when the agent reached idle or done within `timeout_s`."""
        ms = max(1, int(timeout_s * 1000))
        done = self._run(["agent", "wait", pane_id, "--until", "idle", "--until", "done", "--timeout", str(ms)],
                         timeout_s + _CALL_MARGIN_S)
        return done is not None and done.returncode == 0

    def read_visible(self, pane_id: str) -> str:
        """The visible screen with its ANSI escape codes (the adapters tell a faint placeholder from typed text by them); "" on failure."""
        done = self._run(["agent", "read", pane_id, "--source", "visible", "--format", "ansi"], _QUICK_CALL_S)
        return done.stdout if done is not None and done.returncode == 0 else ""

    def prompt(self, pane_id: str, text: str) -> PromptResult:
        """Submit `text` the way Herdr does itself (bracketed paste, then Enter); Herdr refuses a blocked agent."""
        done = self._run(["agent", "prompt", pane_id, text], _QUICK_CALL_S)  # measured: herdr takes "-x" text as is, and rejects a `--` ("unknown option")
        if done is None:
            return PromptResult.FAILED
        try:
            reply = json.loads(done.stdout)
        except ValueError:
            return PromptResult.FAILED
        if not isinstance(reply, dict):
            return PromptResult.FAILED
        error = reply.get("error")
        if isinstance(error, dict) and error.get("code") == "agent_blocked":
            return PromptResult.BLOCKED
        return PromptResult.SENT if done.returncode == 0 and "result" in reply else PromptResult.FAILED

    def send_text(self, pane_id: str, text: str) -> bool:
        """Type `text` into the pane verbatim, without submitting it."""
        done = self._run(["pane", "send-text", pane_id, text], _QUICK_CALL_S)
        return done is not None and done.returncode == 0

    def send_keys(self, pane_id: str, keys: Sequence[str]) -> bool:
        """Press `keys` (Herdr's lowercase names, e.g. enter) in the pane."""
        done = self._run(["pane", "send-keys", pane_id, *keys], _QUICK_CALL_S)
        return done is not None and done.returncode == 0
