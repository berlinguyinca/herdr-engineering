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
)
from .workflow_guard import MATCHER as GUARD_MATCHER
from .workflow_guard import decide

MARKER = "herdr-workflow:managed"
EVENTS = ("SessionStart", "PreCompact", "PreToolUse")

REMINDER = """Engineering defaults (herdr-engineering managed hook):
- Delegate multi-file changes, refactors, debugging, reviews and research to sonnet subagents; do trivial work directly.
- Verify claims with command output before saying work is done.
- Ship via branch -> PR -> review -> CI green -> ready; never commit to main.
- Use real dependencies in tests; no mocks, stubs or fakes.
- Never push herdr-engineering. Keep nothing under /tmp.
- To delete a worktree, build target, scratch dir or test container you created, run `herdr-eng cleanup --scope {worktree,target,tmp,docker} --path <p>`; never raw rm -rf, git worktree remove or docker rm.
- Start throwaway containers with `docker run $(herdr-eng cleanup --print-run-args)` so they are reaped.
- CI is Woodpecker (ci.metabolomics.us) where the repo uses it.
- Do not ask the user about choices these defaults already settle; apply them.
- Context getting long? Run /handoff yourself, without asking: first wait for background subagents to report and commit/push the work where pushing is allowed. It moves the work to a fresh Herdr pane on the same harness."""

PRECOMPACT_MESSAGE = ("Context is about to be compacted. Run /handoff yourself now, without asking the "
                      "user: first let background subagents report and commit/push the work where "
                      "pushing is allowed. It moves the work to a fresh Herdr pane on the same harness "
                      "with a self-sufficient briefing. The hook only prints this; nothing was started.")


ALLOW_RULE = "Bash(herdr-eng cleanup:*)"


class _WorkflowHooksUnit(ClaudeHooksUnit):
    """Managed hooks plus the allow rule for the cleanup command, offered ONCE.

    The rule is the user's to keep or revoke: it never counts toward drift. ``offer_rule`` is
    True only while no "offered" marker exists (``WorkflowInstaller`` writes it the first time
    the rule is added or seen), so a later removal is respected and never re-added.
    """
    marker = MARKER
    matchers = {"PreToolUse": GUARD_MATCHER}

    def install(self, text, offer_rule=False):
        data = self._load(super().install(text))
        if offer_rule:
            allow = data.setdefault("permissions", {}).setdefault("allow", [])
            if ALLOW_RULE not in allow:
                allow.append(ALLOW_RULE)
        return _dump_json(data, text)

    def offer(self, text):
        """``text`` with the allow rule added when absent (hooks untouched)."""
        data = self._load(text)
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        if ALLOW_RULE not in allow:
            allow.append(ALLOW_RULE)
        return _dump_json(data, text)

    def remove(self, text):
        data = self._load(super().remove(text))
        permissions = data.get("permissions", {})
        if ALLOW_RULE in permissions.get("allow", []):
            permissions["allow"].remove(ALLOW_RULE)
            if not permissions["allow"]:
                del permissions["allow"]
            if not permissions:
                del data["permissions"]
        return _dump_json(data, text)


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
        self.command = command or [sys.executable, "-m", "herdr_engineering.cli"]
        self.which = which or shutil.which
        self.unit = _WorkflowHooksUnit(
            "workflow-hooks", self.home / ".claude" / "settings.json",
            {ev: _hook_command(self.command, ev) for ev in EVENTS})

    def _offered_marker(self) -> Path:
        return self.home / ".local" / "share" / "herdr-engineering" / "workflow" / "allow-rule-offered"

    def _mark_offered(self) -> None:
        marker = self._offered_marker()
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.write_text("the cleanup allow rule was added once; removing it is respected\n")

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

    def install(self, *, dry_run: bool = False) -> Result:
        if not self._claude_present():
            return Result(True, "skip", ABSENT, str(self.unit.path), "claude not installed", dry_run)
        text = self._text()
        state = self.unit.state(text)
        res = Result(True, "noop", state, str(self.unit.path), dry_run=dry_run)
        offer = not self._offered_marker().exists()
        if state in (INVALID, FOREIGN):
            res.ok, res.action = False, "invalid" if state == INVALID else "conflict"
            return res
        if state == CURRENT:
            if not offer:
                return res
            # hooks installed before the rule existed: offer it once, then respect removal
            new = self.unit.offer(text)
            if new != text:
                res.action = "update"
                res.diff = _diff(text, new, self.unit.path.name)
        else:
            new = self.unit.install(text, offer_rule=offer)
            res.action = "create" if state == ABSENT else "update"
            res.diff = _diff(text, new, self.unit.path.name)
        if not dry_run:
            if new != text:
                self._backup()
                _atomic_write(self.unit.path, new)
            if offer:
                self._mark_offered()
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
                self._offered_marker().unlink(missing_ok=True)  # a reinstall offers the rule again
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
        elif event == "PreCompact":
            out = {"systemMessage": PRECOMPACT_MESSAGE}
        else:
            return 0
        stdout.write(json.dumps(out) + "\n")
    except Exception:  # a reminder must never fail a session
        return 0
    return 0
