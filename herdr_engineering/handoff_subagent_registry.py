"""Registry of running Claude subagents per herdr pane (``SubagentStart`` / ``SubagentStop`` hook body).

One JSON file per running subagent under ``$HERDR_ENGINEERING_STATE/subagents/<pane_id>/<agent_id>`` so that
``herdr-handoff takeover`` can refuse to close a pane that still has running subagents. A hook must never break
Claude, so every failure path prints nothing and exits 0.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
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
    tmp = directory / f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
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


@dataclass(frozen=True)
class SubagentStatus:
    """``known`` is False when no hook ever fired for the pane; ``live`` counts non-stale entries."""

    known: bool
    live: int


def status(pane_id: str, env: Mapping[str, str], now: float | None = None) -> SubagentStatus:
    """Never raises: a vanished or unreadable registry is reported as unknown, the safe direction (the drain order
    then asks the session to list and wait for its own agents)."""
    try:
        if not pane_id or not _pane_dir(env, pane_id).is_dir():
            return SubagentStatus(known=False, live=0)
        return SubagentStatus(known=True, live=len(live_entries(env, pane_id, now)))
    except OSError:
        return SubagentStatus(known=False, live=0)


def wait_status(pane_id: str, env: Mapping[str, str], timeout_s: float, poll_s: float = 2.0) -> SubagentStatus:
    """The status that ended the wait: the idle one, or the last one seen when ``timeout_s`` ran out."""
    deadline = time.monotonic() + timeout_s
    while True:
        current = status(pane_id, env)
        if current.live == 0:
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return current
        time.sleep(min(poll_s, remaining))


def wait_until_idle(pane_id: str, env: Mapping[str, str], timeout_s: float, poll_s: float = 2.0) -> bool:
    """True when no live subagent remains before ``timeout_s``. An unknown pane (``known=False``) returns True at
    once: nothing is recorded to wait for, and the caller decides what "unknown" means."""
    return wait_status(pane_id, env, timeout_s, poll_s).live == 0


def purge_pane(pane_id: str, env: Mapping[str, str]) -> int:
    """Delete the pane's registry directory (call when the pane closes so a reused id starts clean); returns the
    number of entries actually removed (0 when the directory is still there)."""
    if not pane_id:
        return 0
    directory = _pane_dir(env, pane_id)
    try:
        if not directory.is_dir():
            return 0
        count = sum(1 for p in directory.iterdir() if not p.name.endswith(".tmp"))
    except OSError:
        count = 0
    shutil.rmtree(directory, ignore_errors=True)
    return 0 if directory.exists() else count


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
