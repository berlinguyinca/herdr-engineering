"""The controller's view of `herdr-handoff`: one subprocess per call, never raises."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..handoff_adapters.base import ExitPlan
from .proc import run_group


@dataclass(frozen=True)
class HandoffCliResult:
    returncode: int  # 124 timed out, 127 could not be started
    stdout: str
    stderr: str


def takeover_args(store_root: Path, handoff_id: str, *, parent_pane: str, worktree: Path, exit_plan: ExitPlan,
                  allow_dirty: bool, confirm_timeout_s: float, keep_source: bool = False) -> list[str]:
    """Arguments of `herdr-handoff takeover` (after the program name). The source harness is asked to exit by
    herdr-handoff itself, which sends the plan's text and keys only when Herdr reports an agent in the pane."""
    args = ["--root", str(store_root), "takeover", handoff_id, "--parent-pane", parent_pane, "--direction", "down",
            "--cwd", str(worktree), "--source-cwd", str(worktree), "--confirm-timeout", f"{confirm_timeout_s:g}"]
    if exit_plan.text:
        args += ["--source-exit-text", exit_plan.text]
    for key in exit_plan.keys:
        args += ["--source-exit-key", key.lower()]  # Herdr takes lowercase key names
    if exit_plan.text or exit_plan.keys:
        args += ["--source-exit-timeout", f"{exit_plan.process_end_timeout_s:g}"]
    if allow_dirty:
        args.append("--allow-dirty-source")
    if keep_source:
        args.append("--keep-source")
    return args


class HandoffCli:
    """`argv_prefix` is the command that starts the CLI (default the installed `herdr-handoff`); `env` replaces
    the environment (the HERDR_HANDOFF_STORE of another store, a moved HOME, HERDR_SOCKET_PATH of another Herdr
    server: herdr-handoff passes its environment on to every `herdr` it runs); `cwd` is the working directory of
    every call (a relative HERDR_SOCKET_PATH in `env` is resolved from there)."""

    def __init__(self, argv_prefix: Sequence[str] = ("herdr-handoff",), env: Mapping[str, str] | None = None,
                 cwd: Path | None = None) -> None:
        self._prefix = tuple(argv_prefix)
        self._env = dict(env) if env is not None else None
        self._cwd = cwd

    def run(self, args: Sequence[str], *, timeout_s: float, input_text: str | None = None) -> HandoffCliResult:
        done = run_group([*self._prefix, *args], timeout_s, env=self._env, cwd=self._cwd, input_text=input_text)
        return HandoffCliResult(done.returncode, done.stdout, done.stderr)

    def _json(self, args: Sequence[str], timeout_s: float) -> dict[str, object] | None:
        done = self.run(args, timeout_s=timeout_s)
        try:
            parsed = json.loads(done.stdout)
        except ValueError:
            return None
        return parsed if done.returncode == 0 and isinstance(parsed, dict) else None

    def inspect(self, store_root: Path, handoff_id: str, *, timeout_s: float = 60.0) -> dict[str, object] | None:
        """The record, or None when it cannot be read."""
        return self._json(["--root", str(store_root), "inspect", handoff_id], timeout_s)

    def document(self, store_root: Path, handoff_id: str, *, timeout_s: float = 60.0) -> Path | None:
        """Absolute path of the briefing document, as herdr-handoff itself derives (and writes) it."""
        found = self._json(["--root", str(store_root), "resume", handoff_id], timeout_s)
        path = found.get("document") if found else None
        return Path(path) if isinstance(path, str) and Path(path).is_absolute() else None

    def takeover(self, store_root: Path, handoff_id: str, *, parent_pane: str, worktree: Path, exit_plan: ExitPlan,
                 allow_dirty: bool, confirm_timeout_s: float, keep_source: bool = False,
                 timeout_s: float) -> HandoffCliResult:
        args = takeover_args(store_root, handoff_id, parent_pane=parent_pane, worktree=worktree, exit_plan=exit_plan,
                             allow_dirty=allow_dirty, confirm_timeout_s=confirm_timeout_s, keep_source=keep_source)
        return self.run(args, timeout_s=timeout_s)
