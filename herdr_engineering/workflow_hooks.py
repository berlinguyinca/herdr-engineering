"""Managed Claude Code hooks that make the default engineering workflow automatic.

Reuses the herdr-memory hook machinery (``ClaudeHooksUnit``) with its own marker, so
foreign entries -- including the herdr-memory hooks -- are never touched.  The hooks
only print a reminder; they never run anything, never block, and always exit 0.
"""
from __future__ import annotations

import json
import shlex
import shutil
import sys
import time
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
    _diff,
    _dump_json,
    writable_target,
)
from .workflow_guard import MATCHER as GUARD_MATCHER
from .workflow_guard import decide

MARKER = "herdr-workflow:managed"
EVENTS = ("SessionStart", "PreToolUse")
RETIRED_EVENTS = ("PreCompact",)  # installed by older versions; install removes only our managed command

REMINDER = """Engineering defaults (herdr-engineering managed hook):
- Delegate multi-file changes, refactors, debugging, reviews and research to sonnet subagents; do trivial work directly.
- Verify claims with command output before saying work is done.
- Ship via branch -> PR -> review -> CI green -> ready; never commit to main.
- Use real dependencies in tests; no mocks, stubs or fakes.
- Never push herdr-engineering. Keep nothing under /tmp.
- CI is Woodpecker (ci.metabolomics.us) where the repo uses it."""


class _WorkflowHooksUnit(ClaudeHooksUnit):
    marker = MARKER
    matchers = {"PreToolUse": GUARD_MATCHER}

    def _is_retired(self, hook, event: str) -> bool:
        command = hook.get("command") if isinstance(hook, dict) else None
        return isinstance(command, str) and self.marker in command and f"workflow hook {event}" in command

    def _retired_present(self, data: dict) -> bool:
        hooks = data.get("hooks", {})
        return any(self._is_retired(h, ev) for ev in RETIRED_EVENTS if isinstance(hooks.get(ev), list)
                   for e in hooks[ev] if isinstance(e, dict) and isinstance(e.get("hooks"), list) for h in e["hooks"])

    def _drop_retired(self, data: dict) -> None:
        """Remove our retired command from the retired events; any other hook, even in the same entry, stays."""
        hooks = data.get("hooks", {})
        for ev in RETIRED_EVENTS:
            if not isinstance(hooks.get(ev), list):
                continue
            kept = []
            for e in hooks[ev]:
                if isinstance(e, dict) and isinstance(e.get("hooks"), list) and any(self._is_retired(h, ev) for h in e["hooks"]):
                    rest = [h for h in e["hooks"] if not self._is_retired(h, ev)]
                    if rest:
                        kept.append({**e, "hooks": rest})
                else:
                    kept.append(e)
            if kept:
                hooks[ev] = kept
            else:
                del hooks[ev]
        if not hooks:
            data.pop("hooks", None)

    def state(self, text):
        state = super().state(text)
        if state == INVALID:
            return state
        return STALE if self._retired_present(self._load(text)) else state

    def install(self, text):
        data = self._load(text)
        self._drop_retired(data)
        return super().install(_dump_json(data, text))


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
    detail: str = ""

    def to_dict(self):
        return {"ok": self.ok, "action": self.action, "state": self.state, "path": self.path,
                "dry_run": self.dry_run, **({"detail": self.detail} if self.detail else {})}


class WorkflowInstaller:
    def __init__(self, home: Path | None = None, *, command: list[str] | None = None,
                 which: Callable[[str], str | None] | None = None):
        self.home = Path(home) if home else Path.home()
        self.command = command or [sys.executable, "-m", "herdr_engineering.cli"]
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
            pristine = dest / f"{path.name}.bak"
            # The first backup is the untouched pre-install copy; later ones never overwrite it.
            shutil.copy2(path, pristine if not pristine.exists() else dest / f"{path.name}.{time.time_ns()}.bak")

    def _write(self, res: Result, new: str) -> None:
        """Back up, then write; a read-only file or directory turns `res` into a failure with a one-line reason and writes nothing."""
        try:
            writable_target(self.unit.path)
            self._backup()
            _atomic_write(self.unit.path, new)
        except OSError as exc:
            res.ok, res.action, res.detail = False, "refused", f"cannot write settings.json: {exc}"

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
            self._write(res, new)
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
                self._write(res, new)
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
            raw = stdin.read()
        except (OSError, ValueError):
            raw = ""
        if event == "PreToolUse":
            verdict = decide(json.loads(raw))
            if verdict is None:
                return 0
            out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": verdict[0],
                                          "permissionDecisionReason": verdict[1]}}
        elif event == "SessionStart":
            out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": REMINDER}}
        else:
            return 0
        stdout.write(json.dumps(out) + "\n")
    except Exception:  # a reminder must never fail a session
        return 0
    return 0
