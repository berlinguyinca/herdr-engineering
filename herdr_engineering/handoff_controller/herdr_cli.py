"""The controller's view of Herdr: the real `herdr` binary, one subprocess per call. No method raises."""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from enum import StrEnum

_CALL_MARGIN_S = 5.0  # a herdr call gets its own timeout plus this much before the subprocess is abandoned
_QUICK_CALL_S = 15.0  # herdr calls that answer from memory


class PromptResult(StrEnum):
    SENT = "sent"
    BLOCKED = "blocked"  # herdr refused: the agent waits on a dialog, nothing was typed
    FAILED = "failed"


class HerdrCli:
    """`binary` defaults to `herdr` on PATH; `env` replaces the environment (e.g. HERDR_SOCKET_PATH of another server)."""

    def __init__(self, binary: str | None = None, env: Mapping[str, str] | None = None) -> None:
        self._binary = binary or shutil.which("herdr") or "herdr"
        self._env = dict(env) if env is not None else None

    def _run(self, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run([self._binary, *args], env=self._env, capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def agent_status(self, pane_id: str) -> str | None:
        done = self._run(["agent", "get", pane_id], _QUICK_CALL_S)
        if done is None or done.returncode != 0:
            return None
        try:
            status = json.loads(done.stdout)["result"]["agent"]["agent_status"]
        except (ValueError, KeyError, TypeError):
            return None
        return status if isinstance(status, str) else None

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
        done = self._run(["agent", "prompt", pane_id, text], _QUICK_CALL_S)
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
