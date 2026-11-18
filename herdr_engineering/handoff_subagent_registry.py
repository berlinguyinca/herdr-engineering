"""Registry of running Claude subagents per herdr pane (``SubagentStart`` / ``SubagentStop`` hook body).

One JSON file per running subagent under ``$HERDR_ENGINEERING_STATE/subagents/<pane_id>/<agent_id>`` so that
``herdr-handoff takeover`` can refuse to close a pane that still has running subagents. A hook must never break
Claude, so every failure path prints nothing and exits 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

STALE_SECONDS = 6 * 3600


def _state_dir(env: Mapping[str, str]) -> Path:
    root = env.get("HERDR_ENGINEERING_STATE") or str(Path.home() / ".local/share/herdr-engineering/state")
    return Path(root) / "subagents"


def sanitize(name: str) -> str:
    """Restrict to ``[A-Za-z0-9_.:-]``; dot-only names (``.``, ``..``) can never name a path component."""
    clean = re.sub(r"[^A-Za-z0-9_.:-]", "_", name)[:200]
    return clean.replace(".", "_") if set(clean) <= {"."} else clean


def _pane_dir(env: Mapping[str, str], pane: str) -> Path:
    return _state_dir(env) / sanitize(pane)


def record(phase: str, raw: str, env: Mapping[str, str]) -> None:
    pane = env.get("HERDR_PANE_ID")
    if not pane:
        return
    data = json.loads(raw)
    agent_id = data.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return
    directory = _pane_dir(env, pane)
    target = directory / sanitize(agent_id)
    if phase == "stop":
        target.unlink(missing_ok=True)
        return
    agent_type = data.get("agent_type")
    entry = {"agent_id": agent_id, "agent_type": agent_type if isinstance(agent_type, str) else "", "started": int(time.time())}
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{target.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, target)


def run_subagent_hook(phase: str, env: Mapping[str, str] | None = None, stdin: TextIO | None = None) -> int:
    """Always returns 0 and prints nothing."""
    try:
        record(phase, (stdin or sys.stdin).read(), os.environ if env is None else env)
    except Exception:
        pass
    return 0


def live_entries(env: Mapping[str, str], pane: str, now: float | None = None) -> list[dict]:
    """Live entries for the pane; entries older than 6 hours (or unreadable) are stale and deleted."""
    now = time.time() if now is None else now
    entries: list[dict] = []
    directory = _pane_dir(env, pane)
    if not directory.is_dir():
        return entries
    for path in sorted(directory.iterdir()):
        if path.name.endswith(".tmp"):
            continue
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            started = entry["started"]
            if not isinstance(started, (int, float)) or now - started > STALE_SECONDS:
                path.unlink(missing_ok=True)
                continue
            entries.append(entry)
        except (OSError, ValueError, KeyError, TypeError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return entries


def run_subagents(pane: str | None, env: Mapping[str, str] | None = None, *, as_json: bool = False,
                  out: TextIO | None = None) -> int:
    """Print the live subagents of a pane; always returns 0."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        pane = pane or env.get("HERDR_PANE_ID") or ""
        entries = live_entries(env, pane) if pane else []
    except Exception:
        entries = []
    if as_json:
        print(json.dumps(entries), file=out)
    else:
        for e in entries:
            print(f"{e.get('agent_id')}\t{e.get('agent_type')}\t{e.get('started')}", file=out)
    return 0
