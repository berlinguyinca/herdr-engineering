"""Install, remove and check the ``SubagentStart`` / ``SubagentStop`` registry hooks in ``~/.claude/settings.json``.

The retired 50% context reminder (``UserPromptSubmit`` running ``handoff context-hook``) is removed by ``install`` and ``uninstall`` and
reported by ``check``; the automatic-handoff controller replaced it. Only an entry carrying the marker AND that command is touched.

Mirrors the join-hook installer in ``fabric_join_setup`` (same ``Item`` results, same atomic, format-preserving write), but ownership is
decided by the command's trailing marker comment rather than a manifest, so a hand-added identical entry is recognised and never duplicated.
Unparseable or unexpectedly shaped settings are never overwritten: the result is ``refused``.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .fabric_join_setup import FAIL_ACTIONS, Item, _home, _save_settings
from .memory_install import writable_target
from .workflow_hooks import WorkflowInstaller

HOOK_MARKER = "herdr-handoff-auto"
LEGACY_EVENT = "UserPromptSubmit"
LEGACY_FRAGMENT = "handoff context-hook"
_BIN = '[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng"'
START_COMMAND = f"{_BIN} handoff subagent-hook start || true # {HOOK_MARKER}"
STOP_COMMAND = f"{_BIN} handoff subagent-hook stop || true # {HOOK_MARKER}"
HOOKS: dict[str, str] = {"SubagentStart": START_COMMAND, "SubagentStop": STOP_COMMAND}
SETTINGS_REL = Path(".claude/settings.json")
__all__ = ["FAIL_ACTIONS", "HOOKS", "HOOK_MARKER", "START_COMMAND", "STOP_COMMAND", "check", "install", "install_subagent_hooks",
           "uninstall"]


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
    if not isinstance(hooks, dict) or not all(isinstance(hooks.get(e, []), list) for e in (*HOOKS, LEGACY_EVENT)):
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


def _is_legacy(hook) -> bool:
    command = hook.get("command") if isinstance(hook, dict) else None
    return isinstance(command, str) and _marked(command) and LEGACY_FRAGMENT in command


def _legacy_present(data: dict) -> bool:
    return any(_is_legacy(h) for e in data.get("hooks", {}).get(LEGACY_EVENT, []) if isinstance(e, dict) and isinstance(e.get("hooks"), list)
               for h in e["hooks"])


def _remove_legacy(data: dict, target: str) -> tuple[Item, bool]:
    """Drop the retired context-hook command (and an entry left empty by that); everything else stays as it is."""
    if not _legacy_present(data):
        return Item(target, "unchanged", f"no retired {LEGACY_EVENT} context-hook present"), False
    kept: list = []
    for entry in data["hooks"][LEGACY_EVENT]:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(_is_legacy(h) for h in entry["hooks"]):
            rest = [h for h in entry["hooks"] if not _is_legacy(h)]
            if rest:
                kept.append({**entry, "hooks": rest})
        else:
            kept.append(entry)
    if kept:
        data["hooks"][LEGACY_EVENT] = kept
    else:
        del data["hooks"][LEGACY_EVENT]
        if not data["hooks"]:
            del data["hooks"]
    return Item(target, "removed", f"retired {LEGACY_EVENT} context-hook (the controller replaced it)"), True


def _save(home: Path | None, path: Path, data: dict, text: str) -> None:
    """Back the file up first (``settings.json.bak`` is the pristine copy and is never overwritten), then write atomically.
    A read-only settings.json or directory raises PermissionError before the backup."""
    writable_target(path)
    if path.is_file():
        dest = _home(home) / ".local/share/herdr-engineering/handoff/backups"
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        pristine = dest / f"{path.name}.bak"
        shutil.copy2(path, pristine if not pristine.exists() else dest / f"{path.name}.{time.time_ns()}.bak")
    _save_settings(path, data, text)


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
    legacy = _remove_legacy(data, str(path))
    if legacy[1]:
        results.append(legacy)
    if any(changed for _, changed in results) and not dry_run:
        try:
            _save(home, path, data, text)
        except OSError as exc:
            return [Item(str(path), "refused", f"cannot write settings.json: {exc}")]
    return [item for item, _ in results]


def install(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    """One Item per hook event, in HOOKS order; a single ``refused`` Item when settings.json is unusable."""
    return _install_events(HOOKS, home, dry_run)


def install_subagent_hooks(home: Path | None = None, *, dry_run: bool = False, path: str | None = None) -> list[Item]:
    """Install only ``SubagentStart`` / ``SubagentStop`` (the registry hooks the installer adds by default); also drops the retired
    ``UserPromptSubmit`` context-hook.

    Gated like the workflow hooks (``WorkflowInstaller._claude_present``: ``claude`` on ``path`` (default ``$PATH``) or ``~/.claude``
    exists): without Claude Code a single ``skipped`` item is returned and nothing is written.
    """
    if not WorkflowInstaller(home, which=lambda name: shutil.which(name, path=path))._claude_present():
        return [Item(str(_home(home) / SETTINGS_REL), "skipped", "Claude Code is not installed on this host")]
    return _install_events(HOOKS, home, dry_run)


def uninstall(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    path = _home(home) / SETTINGS_REL
    try:
        data, text = _load(path)
    except _Unusable as exc:
        return [Item(str(path), "refused", str(exc))]
    results = [_uninstall_one(data, event, str(path)) for event in HOOKS]
    legacy = _remove_legacy(data, str(path))
    if legacy[1]:
        results.append(legacy)
    if any(changed for _, changed in results) and not dry_run:
        try:
            _save(home, path, data, text)
        except OSError as exc:
            return [Item(str(path), "refused", f"cannot write settings.json: {exc}")]
    return [item for item, _ in results]


def check(home: Path | None = None) -> list[Item]:
    path = _home(home) / SETTINGS_REL
    try:
        data, _ = _load(path)
    except _Unusable as exc:
        return [Item(str(path), "refused", str(exc))]
    items = [Item(str(path), "ok", f"{event} handoff hook present") if _present(data, event) else
             Item(str(path), "missing", f"{event} handoff hook not installed; run `herdr-eng handoff auto install`") for event in HOOKS]
    if _legacy_present(data):
        items.append(Item(str(path), "stale", f"retired {LEGACY_EVENT} context-hook still installed (it does nothing now); "
                                              "run `herdr-eng handoff auto install` to remove it"))
    return items
