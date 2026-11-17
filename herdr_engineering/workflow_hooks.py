"""Managed Claude Code hooks that make the default engineering workflow automatic.

Reuses the herdr-memory hook machinery (``ClaudeHooksUnit``) with its own marker, so
foreign entries -- including the herdr-memory hooks -- are never touched.  The hooks
only print a reminder; they never run anything, never block, and always exit 0.
"""
from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .memory_install import (
    ABSENT,
    CURRENT,
    FOREIGN,
    INVALID,
    STALE,
    ClaudeHooksUnit,
    _atomic_write,
    _default_command,
    _diff,
)

MARKER = "herdr-workflow:managed"
EVENTS = ("SessionStart", "PreCompact")

REMINDER = """Engineering defaults (herdr-engineering managed hook):
- Delegate multi-file changes, refactors, debugging, reviews and research to sonnet subagents; do trivial work directly.
- Verify claims with command output before saying work is done.
- Ship via branch -> PR -> review -> CI green -> ready; never commit to main.
- Use real dependencies in tests; no mocks, stubs or fakes.
- Never push herdr-engineering. Keep nothing under /tmp.
- CI is Woodpecker (ci.metabolomics.us) where the repo uses it.
- Context getting long? /handoff moves the work to a fresh Herdr pane on the same harness."""

PRECOMPACT_MESSAGE = ("Context is about to be compacted. Consider /handoff first: it moves the work to a "
                      "fresh Herdr pane on the same harness with a self-sufficient briefing. "
                      "This is a reminder only; nothing was started.")


class _WorkflowHooksUnit(ClaudeHooksUnit):
    marker = MARKER


def _hook_command(command: list[str], event: str) -> str:
    run = shlex.join([*command, "workflow", "hook", event])
    return f"[ -x {shlex.quote(command[0])} ] && {run} || true # {MARKER}"


@dataclass
class Result:
    ok: bool = True
    action: str = "noop"
    state: str = ABSENT
    path: str = ""
    diff: str = ""
    dry_run: bool = False

    def to_dict(self):
        return {"ok": self.ok, "action": self.action, "state": self.state, "path": self.path,
                "dry_run": self.dry_run}


class WorkflowInstaller:
    def __init__(self, home: Path | None = None, *, command: list[str] | None = None,
                 which: Callable[[str], str | None] | None = None):
        self.home = Path(home) if home else Path.home()
        self.command = command or _default_command()
        self.which = which or shutil.which
        self.unit = _WorkflowHooksUnit(
            "workflow-hooks", self.home / ".claude" / "settings.json",
            {ev: _hook_command(self.command, ev) for ev in EVENTS})

    def _text(self) -> str | None:
        return self.unit.path.read_text(encoding="utf-8") if self.unit.path.is_file() else None

    def _claude_present(self) -> bool:
        return bool(self.which("claude")) or (self.home / ".claude").is_dir()

    def status(self) -> Result:
        state = self.unit.state(self._text())
        action = {CURRENT: "noop", ABSENT: "create", STALE: "update", FOREIGN: "conflict",
                  INVALID: "invalid"}[state]
        return Result(state in (CURRENT,), action, state, str(self.unit.path))

    def _backup(self) -> None:
        path = self.unit.path
        if path.is_file():
            dest = self.home / ".local" / "share" / "herdr-engineering" / "workflow" / "backups"
            dest.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, dest / f"{path.name}.bak")

    def install(self, *, dry_run: bool = False) -> Result:
        if not self._claude_present():
            return Result(True, "skip", ABSENT, str(self.unit.path), "claude not installed", dry_run)
        text = self._text()
        state = self.unit.state(text)
        res = Result(True, "noop", state, str(self.unit.path), dry_run=dry_run)
        if state == CURRENT:
            return res
        if state in (INVALID, FOREIGN):
            res.ok, res.action = False, "invalid" if state == INVALID else "conflict"
            return res
        new = self.unit.install(text)
        res.action = "create" if state == ABSENT else "update"
        res.diff = _diff(text, new, self.unit.path.name)
        if not dry_run:
            self._backup()
            _atomic_write(self.unit.path, new)
        return res

    repair = install

    def uninstall(self, *, dry_run: bool = False) -> Result:
        text = self._text()
        state = self.unit.state(text)
        res = Result(True, "noop", state, str(self.unit.path), dry_run=dry_run)
        if state in (CURRENT, STALE):
            new = self.unit.remove(text)
            res.action, res.diff = "remove", _diff(text, new, self.unit.path.name)
            if not dry_run:
                self._backup()
                _atomic_write(self.unit.path, new)
        elif state == INVALID:
            res.ok, res.action = False, "invalid"
        return res

    def doctor(self) -> dict:
        r = self.status()
        detail = {CURRENT: "managed hooks match the expected commands",
                  ABSENT: "not installed (herdr-eng workflow install)",
                  STALE: "drifted from expected commands (herdr-eng workflow repair)",
                  INVALID: "settings.json is not valid JSON; left untouched",
                  FOREIGN: "foreign entry in the way"}[r.state]
        return {"ok": r.ok, "overall": r.state,
                "checks": [{"name": "workflow-hooks", "status": r.state, "detail": f"{detail}: {r.path}"}]}


def run_hook(event: str, stdin: IO[str], stdout: IO[str]) -> int:
    """Print the reminder for ``event``. Always returns 0 and prints nothing on any failure."""
    try:
        try:
            stdin.read()
        except (OSError, ValueError):
            pass
        if event == "SessionStart":
            out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": REMINDER}}
        elif event == "PreCompact":
            out = {"systemMessage": PRECOMPACT_MESSAGE}
        else:
            return 0
        stdout.write(json.dumps(out) + "\n")
    except Exception:  # a reminder must never fail a session
        return 0
    return 0
