"""Claude Code ``UserPromptSubmit`` hook body: tell the agent to run ``/handoff`` when the context is filling up.

The hook cannot write the handoff (that needs the model); it only injects an instruction, once per session.
Context used is read from the last assistant message's usage in the transcript JSONL. A hook must never break a
prompt, so every failure path prints nothing and exits 0.
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

DEFAULT_MAX_TOKENS = 200_000  # the transcript does not name the window; see _window()
LARGE_MAX_TOKENS = 1_000_000  # assumed once usage has exceeded the default window
REMIND_STEP_POINTS = 10  # an ignored instruction is repeated only after this many more points of context
DEFAULT_THRESHOLD = 0.50
_TAIL_BYTES = 1_048_576
_MARKER_MAX_AGE_SECONDS = 30 * 86400
_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _state_dir(env: Mapping[str, str]) -> Path:
    root = env.get("HERDR_ENGINEERING_STATE") or str(Path.home() / ".local/share/herdr-engineering/state")
    return Path(root) / "handoff-context"


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _prune_stale_markers(directory: Path) -> None:
    """Drop markers for sessions older than 30 days so the state directory cannot grow without bound."""
    cutoff = time.time() - _MARKER_MAX_AGE_SECONDS
    for old in directory.glob("*.fired"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def context_tokens(transcript: Path) -> int | None:
    """Tokens in context at the last assistant message, or None when the transcript has none."""
    size = transcript.stat().st_size
    with open(transcript, "rb") as fh:
        fh.seek(max(0, size - _TAIL_BYTES))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if isinstance(rec, dict) and rec.get("type") == "assistant" and isinstance(usage, dict):
            return sum(v for k in _USAGE_KEYS if isinstance(v := usage.get(k), int))
    return None


def _window(env: Mapping[str, str], used: int) -> float:
    """Window size: the env override, else 200k, else 1M once usage proves a 200k window cannot be right."""
    window = _float_env(env, "HERDR_HANDOFF_CONTEXT_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    if "HERDR_HANDOFF_CONTEXT_MAX_TOKENS" not in env and used > window:
        return float(LARGE_MAX_TOKENS)
    return window


def _claim(marker: Path, used: int, window: float) -> bool:
    """Record this reminder (as absolute tokens, so a change of window cannot skew it). False when one was
    already given within REMIND_STEP_POINTS of the current window."""
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            last = int(marker.read_text().strip())
        except (OSError, ValueError):
            return False
        if used < last + window * REMIND_STEP_POINTS / 100:
            return False
        marker.write_text(str(used))
        return True
    with os.fdopen(fd, "w") as fh:
        fh.write(str(used))
    return True


def _message(percent: int) -> str:
    return (f"Context is at {percent}% of the window. Finish or record any running subagents, then run the "
            "/handoff skill now so the work moves to a fresh pane. If the user's current message is mid-task, "
            "finish the current step first.")


def evaluate(raw: str, env: Mapping[str, str]) -> str | None:
    """The hook output to print, or None to stay silent. Claims the per-session marker when it fires."""
    if env.get("HERDR_HANDOFF_AUTO") == "0":
        return None
    data = json.loads(raw)
    session, path = data["session_id"], data["transcript_path"]
    if not isinstance(session, str) or not isinstance(path, str) or not session:
        return None
    used = context_tokens(Path(path))
    if used is None:
        return None
    window = _window(env, used)
    if used / window < _float_env(env, "HERDR_HANDOFF_CONTEXT_THRESHOLD", DEFAULT_THRESHOLD):
        return None
    percent = round(100 * used / window)
    marker = _state_dir(env) / (re.sub(r"[^A-Za-z0-9._-]", "_", session)[:128] + ".fired")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not _claim(marker, used, window):
        return None
    _prune_stale_markers(marker.parent)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                              "additionalContext": _message(percent)}})


def run_context_hook(harness: str, env: Mapping[str, str] | None = None, stdin: TextIO | None = None,
                     out: TextIO | None = None) -> int:
    """Always returns 0; prints the hook JSON only when the threshold is crossed for the first time."""
    env = os.environ if env is None else env
    try:
        if harness != "claude":
            return 0
        result = evaluate((stdin or sys.stdin).read(), env)
        if result:
            print(result, file=out or sys.stdout)
    except Exception:
        pass
    return 0
