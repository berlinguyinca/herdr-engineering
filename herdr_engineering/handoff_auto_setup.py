"""Install, remove and check the opt-in ``/handoff`` UserPromptSubmit hook in ``~/.claude/settings.json``.

Mirrors the join-hook installer in ``fabric_join_setup`` (same ``Item`` results, same atomic, format-preserving write), but ownership is
decided by the command's trailing marker comment rather than a manifest, so a hand-added identical entry is recognised and never duplicated.
Unparseable or unexpectedly shaped settings are never overwritten: the result is ``refused``.
"""
from __future__ import annotations

import json
from pathlib import Path

from .fabric_join_setup import FAIL_ACTIONS, Item, _home, _save_settings

HOOK_MARKER = "herdr-handoff-auto"
HOOK_EVENT = "UserPromptSubmit"
HOOK_COMMAND = ('[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng" handoff context-hook claude || true '
                f"# {HOOK_MARKER}")
SETTINGS_REL = Path(".claude/settings.json")
__all__ = ["FAIL_ACTIONS", "HOOK_COMMAND", "HOOK_EVENT", "HOOK_MARKER", "check", "install", "uninstall"]


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
    if not isinstance(hooks, dict) or not isinstance(hooks.get(HOOK_EVENT, []), list):
        raise _Unusable("settings.json has an unexpected shape; left untouched")
    return data, text


def _commands(entry) -> list[str]:
    if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
        return []
    return [h["command"] for h in entry["hooks"] if isinstance(h, dict) and isinstance(h.get("command"), str)]


def _marked(command: str) -> bool:
    return command.rstrip().endswith(f"# {HOOK_MARKER}")


def _present(data: dict) -> list[str]:
    return [c for e in data.get("hooks", {}).get(HOOK_EVENT, []) for c in _commands(e) if _marked(c)]


def install(home: Path | None = None, *, dry_run: bool = False) -> Item:
    path = _home(home) / SETTINGS_REL
    try:
        data, text = _load(path)
    except _Unusable as exc:
        return Item(str(path), "refused", str(exc))
    found = _present(data)
    if found:
        if HOOK_COMMAND in found:
            return Item(str(path), "unchanged", f"{HOOK_EVENT} handoff hook already present")
        return Item(str(path), "preserved", f"{HOOK_EVENT} handoff hook present with a different command; left as is")
    data.setdefault("hooks", {}).setdefault(HOOK_EVENT, []).append({"hooks": [{"type": "command", "command": HOOK_COMMAND}]})
    if not dry_run:
        _save_settings(path, data, text)
    return Item(str(path), "installed", f"{HOOK_EVENT} handoff hook")


def uninstall(home: Path | None = None, *, dry_run: bool = False) -> Item:
    path = _home(home) / SETTINGS_REL
    try:
        data, text = _load(path)
    except _Unusable as exc:
        return Item(str(path), "refused", str(exc))
    if not _present(data):
        return Item(str(path), "unchanged", f"no {HOOK_EVENT} handoff hook present")
    kept: list = []
    for entry in data["hooks"][HOOK_EVENT]:
        if any(_marked(c) for c in _commands(entry)):
            rest = [h for h in entry["hooks"] if not (isinstance(h, dict) and isinstance(h.get("command"), str) and _marked(h["command"]))]
            if rest:
                kept.append({**entry, "hooks": rest})
        else:
            kept.append(entry)
    if kept:
        data["hooks"][HOOK_EVENT] = kept
    else:
        del data["hooks"][HOOK_EVENT]
        if not data["hooks"]:
            del data["hooks"]
    if not dry_run:
        _save_settings(path, data, text)
    return Item(str(path), "removed", f"{HOOK_EVENT} handoff hook")


def check(home: Path | None = None) -> Item:
    path = _home(home) / SETTINGS_REL
    try:
        data, _ = _load(path)
    except _Unusable as exc:
        return Item(str(path), "refused", str(exc))
    if _present(data):
        return Item(str(path), "ok", f"{HOOK_EVENT} handoff hook present")
    return Item(str(path), "missing", f"{HOOK_EVENT} handoff hook not installed; run `herdr-eng handoff auto install`")
