"""Install, remove and check the opt-in ``/handoff`` hooks (UserPromptSubmit, SubagentStart, SubagentStop) in ``~/.claude/settings.json``.

Mirrors the join-hook installer in ``fabric_join_setup`` (same ``Item`` results, same atomic, format-preserving write), but ownership is
decided by the command's trailing marker comment rather than a manifest, so a hand-added identical entry is recognised and never duplicated.
Unparseable or unexpectedly shaped settings are never overwritten: the result is ``refused``.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .fabric_join_setup import FAIL_ACTIONS, Item, _home, _save_settings
from .workflow_hooks import WorkflowInstaller

HOOK_MARKER = "herdr-handoff-auto"
HOOK_EVENT = "UserPromptSubmit"
HOOK_COMMAND = ('[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng" handoff context-hook claude || true '
                f"# {HOOK_MARKER}")
_BIN = '[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng"'
START_COMMAND = f"{_BIN} handoff subagent-hook start || true # {HOOK_MARKER}"
STOP_COMMAND = f"{_BIN} handoff subagent-hook stop || true # {HOOK_MARKER}"
HOOKS: dict[str, str] = {HOOK_EVENT: HOOK_COMMAND, "SubagentStart": START_COMMAND, "SubagentStop": STOP_COMMAND}
SUBAGENT_EVENTS = ("SubagentStart", "SubagentStop")
SETTINGS_REL = Path(".claude/settings.json")
__all__ = ["FAIL_ACTIONS", "HOOKS", "HOOK_COMMAND", "HOOK_EVENT", "HOOK_MARKER", "START_COMMAND", "STOP_COMMAND", "check", "install",
           "install_subagent_hooks", "uninstall"]


class _Unusable(Exception):
    pass


def _load(path: Path) -> tuple[dict, str]:
    """Return (settings, raw text); a missing or blank file is an empty settings object."""
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as exc:
        raise _Unusable(f"cannot read settings.json: {exc}") from exc
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise _Unusable("settings.json is not valid JSON; left untouched") from exc
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or not all(isinstance(hooks.get(e, []), list) for e in HOOKS):
        raise _Unusable("settings.json has an unexpected shape; left untouched")
    return data, text


def _commands(entry) -> list[str]:
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return []
    return [h["command"] for h in entry["hooks"] if isinstance(h, dict) and isinstance(h.get("command"), str)]


def _marked(command: str) -> bool:
    return command.rstrip().endswith(f"# {HOOK_MARKER}")


def _present(data: dict, event: str) -> list[str]:
    return [c for e in data.get("hooks", {}).get(event, []) for c in _commands(e) if _marked(c)]


def _install_one(data: dict, event: str, command: str, target: str) -> tuple[Item, bool]:
    found = _present(data, event)
    if found:
        if command in found:
            return Item(target, "unchanged", f"{event} handoff hook already present"), False
        return Item(target, "preserved", f"{event} handoff hook present with a different command; left as is"), False
    data.setdefault("hooks", {}).setdefault(event, []).append({"hooks": [{"type": "command", "command": command}]})
    return Item(target, "installed", f"{event} handoff hook"), True


def _uninstall_one(data: dict, event: str, target: str) -> tuple[Item, bool]:
    if not _present(data, event):
        return Item(target, "unchanged", f"no {event} handoff hook present"), False
    kept: list = []
    for entry in data["hooks"][event]:
        if any(_marked(c) for c in _commands(entry)):
            rest = [h for h in entry["hooks"] if not (isinstance(h, dict) and isinstance(h.get("command"), str) and _marked(h["command"]))]
            if rest:
                kept.append({**entry, "hooks": rest})
        else:
            kept.append(entry)
    if kept:
        data["hooks"][event] = kept
    else:
        del data["hooks"][event]
        if not data["hooks"]:
            del data["hooks"]
    return Item(target, "removed", f"{event} handoff hook"), True


def _install_events(events: dict[str, str], home: Path | None, dry_run: bool) -> list[Item]:
    path = _home(home) / SETTINGS_REL
    try:
        data, text = _load(path)
    except _Unusable as exc:
        return [Item(str(path), "refused", str(exc))]
    results = [_install_one(data, event, command, str(path)) for event, command in events.items()]
    if any(changed for _, changed in results) and not dry_run:
        _save_settings(path, data, text)
    return [item for item, _ in results]


def install(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    """One Item per hook event, in HOOKS order; a single ``refused`` Item when settings.json is unusable."""
    return _install_events(HOOKS, home, dry_run)


def install_subagent_hooks(home: Path | None = None, *, dry_run: bool = False, path: str | None = None) -> list[Item]:
    """Install only ``SubagentStart`` / ``SubagentStop`` (the registry hooks the installer adds by default).

    Gated like the workflow hooks (``WorkflowInstaller._claude_present``: ``claude`` on ``path`` (default ``$PATH``) or ``~/.claude``
    exists): without Claude Code a single ``skipped`` item is returned and nothing is written.
    """
    if not WorkflowInstaller(home, which=lambda name: shutil.which(name, path=path))._claude_present():
        return [Item(str(_home(home) / SETTINGS_REL), "skipped", "Claude Code is not installed on this host")]
    return _install_events({e: HOOKS[e] for e in SUBAGENT_EVENTS}, home, dry_run)


def uninstall(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    path = _home(home) / SETTINGS_REL
    try:
        data, text = _load(path)
    except _Unusable as exc:
        return [Item(str(path), "refused", str(exc))]
    results = [_uninstall_one(data, event, str(path)) for event in HOOKS]
    if any(changed for _, changed in results) and not dry_run:
        _save_settings(path, data, text)
    return [item for item, _ in results]


def check(home: Path | None = None) -> list[Item]:
    path = _home(home) / SETTINGS_REL
    try:
        data, _ = _load(path)
    except _Unusable as exc:
        return [Item(str(path), "refused", str(exc))]
    return [Item(str(path), "ok", f"{event} handoff hook present") if _present(data, event) else
            Item(str(path), "missing", f"{event} handoff hook not installed; run `herdr-eng handoff auto install`") for event in HOOKS]
