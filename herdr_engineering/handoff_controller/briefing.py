"""Wait for the briefing a drained session writes, and write one with a second model call when it does not.

Secrets: the transcript tail goes only to the same harness the session already used (same provider). It is not
written to disk anywhere except inside the briefing the model returns, which `herdr-handoff create` redacts and
stores like any other briefing.

`wait_for_briefing` reads the store's on-disk layout (`<root>/outbox/handoff_<32 hex>.json`) rather than the CLI's
`list`: `list` fails as a whole on one corrupt record, and the wait must skip those.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..handoff_adapters.base import Harness, SessionRef
from .handoff_cli import HandoffCli
from .proc import run_group

_HANDOFF_FILE = re.compile(r"handoff_[a-f0-9]{32}\.json")
_LIST_FIELDS = ("completed", "remaining", "decisions", "risks", "validation", "constraints", "next_commands",
                "files", "unverified")
_FALLBACK_NOTE = ("Fallback briefing: written by a second model call from the transcript tail, git state and PR list, "
                  "not by the session itself; verify it before relying on it.")
_UNKNOWN_GOAL = "unknown — fallback writer: the session's goal could not be read from the transcript"
_CALL_S = 60.0  # one herdr-handoff or git/gh call
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _matches(path: Path, since: datetime, pane_id: str) -> tuple[datetime, str] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        source = record["source"]
        created = _parse_time(record.get("created_at"))
        if source.get("pane_id") != pane_id or created is None or created < since:
            return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return created, path.stem


def wait_for_briefing(store_root: Path, since: str, pane_id: str, timeout_s: float, *, poll_s: float = 1.0) -> str | None:
    """The newest `handoff_<hex>` whose `source.pane_id` is `pane_id` and `created_at` is >= `since` (UTC ISO), else None
    after `timeout_s`. Unparsable records are ignored; never raises."""
    floor = _parse_time(since)
    if floor is None:
        return None
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            found = [hit for path in (store_root / "outbox").glob("handoff_*.json")
                     if _HANDOFF_FILE.fullmatch(path.name) and (hit := _matches(path, floor, pane_id))]
        except OSError:
            found = []
        if found:
            return max(found)[1]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_s, remaining))


class HarnessRunner(Protocol):
    """Give the harness a prompt, get the model's answer text back, or None when it could not answer."""

    def __call__(self, harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None: ...


def headless_argv(harness: Harness) -> list[str]:
    """Non-interactive argv per harness, prompt read from stdin. Flags are from each CLI's `--help`; see the Task 6 report
    for what is unverified without a real model call."""
    match harness:
        case Harness.CLAUDE:
            return ["claude", "-p", "--output-format", "text", "--no-session-persistence", "--tools", ""]
        case Harness.CODEX:
            return ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                    "--color", "never", "-"]
        case Harness.PI:
            return ["pi", "-p", "--no-session", "--no-tools", "--no-skills", "--no-context-files"]
        case Harness.OPENCODE:
            return ["opencode", "run"]


def run_model(argv: list[str], prompt: str, *, cwd: Path, timeout_s: float,
              env: Mapping[str, str] | None = None) -> str | None:
    """Run `argv` with `prompt` on stdin in its own process group; its stdout, or None on a timeout, a missing
    binary, a non-zero exit or empty output. Never raises."""
    done = run_group(argv, timeout_s, env=env, cwd=cwd, input_text=prompt)
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return done.stdout


def headless_runner(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float,
                    env: Mapping[str, str] | None = None) -> str | None:
    """The production `HarnessRunner`: the same harness the session used, headless, in the session's worktree."""
    return run_model(headless_argv(harness), prompt, cwd=cwd, timeout_s=timeout_s, env=env)


def _transcript_tail(ref: SessionRef, tail_bytes: int) -> str:
    path = ref.record_path
    if path is None or path.suffix in _DB_SUFFIXES:
        return "transcript: not available (the harness keeps its record in a database, which is not read)"
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - tail_bytes))
            data = handle.read()
    except OSError as exc:
        return f"transcript: not available ({exc.strerror or type(exc).__name__})"
    return "transcript tail (the last bytes of the session record, oldest first):\n" + data.decode("utf-8", "replace")


def _command_output(label: str, argv: list[str], cwd: Path) -> str:
    done = run_group(argv, _CALL_S, cwd=cwd)
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or ["no output"])[0]
        return f"{label}: not available (exit {done.returncode}: {first})"
    return f"{label}:\n{done.stdout.strip() or '(empty)'}"


def _prompt(ref: SessionRef, worktree: Path, tail_bytes: int, gh: tuple[str, ...]) -> str:
    sections = [
        _transcript_tail(ref, tail_bytes),
        _command_output("git status --short", ["git", "-C", str(worktree), "status", "--short"], worktree),
        _command_output("git log -n 20 --oneline", ["git", "-C", str(worktree), "log", "-n", "20", "--oneline"], worktree),
        _command_output("gh pr list", [*gh, "pr", "list", "--json", "number,title,headRefName"], worktree),
    ]
    return (
        "A coding-agent session ran out of context and did not write its own handoff briefing. Write it for the "
        "agent that takes over, using only the evidence below. Do not invent facts: put anything you cannot "
        "confirm under \"unverified\".\n\n"
        "Answer with one JSON object and nothing else. Keys (all but goal optional; each list is an array of "
        "strings): goal (string), completed, remaining, decisions, risks, validation, constraints, next_commands, "
        "files, unverified, resume_point (string), pull_requests (array of https URLs).\n\n"
        + "\n\n".join(sections) + "\n"
    )


def _parse_answer(text: str) -> dict[str, object] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        answer = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item if isinstance(item, str) else json.dumps(item) for item in value]
    return []


def _create_args(ref: SessionRef, worktree: Path, store_root: Path, answer: Mapping[str, object]) -> list[str]:
    goal = answer.get("goal")
    args = ["--root", str(store_root), "create",
            "--goal", goal if isinstance(goal, str) and goal.strip() else _UNKNOWN_GOAL,
            "--source-harness", ref.harness.value, "--target-harness", ref.harness.value,
            "--source-pane", ref.pane_id, "--worktree", str(worktree.resolve()),
            "--workspace-cwd", str(ref.cwd.resolve()), "--from-json", "-"]
    if ref.session_id:
        args += ["--session-id", ref.session_id, "--source-session", ref.session_id]
    return args


def _mark_fallback(store_root: Path, handoff_id: str) -> bool:
    """`herdr-handoff create` has no provenance option; set `provenance.fallback` in the record it just wrote."""
    path = store_root / "outbox" / f"{handoff_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["provenance"] = {**(record.get("provenance") or {}), "fallback": True}
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except (OSError, ValueError, AttributeError, TypeError):
        return False
    return True


def write_fallback_briefing(ref: SessionRef, worktree: Path, store_root: Path, harness_runner: HarnessRunner, *,
                            handoff: HandoffCli | None = None, gh: tuple[str, ...] = ("gh",),
                            transcript_tail_bytes: int = 200_000, timeout_s: float = 300.0) -> str | None:
    """Ask the session's own harness, headlessly, to write the briefing; the new `handoff_<hex>`, or None on any failure
    (no answer, unparsable answer, `herdr-handoff` refusing it). A missing transcript or `gh` is noted in the prompt."""
    cli = handoff or HandoffCli()
    try:
        prompt = _prompt(ref, worktree, transcript_tail_bytes, gh)
        text = harness_runner(ref.harness, prompt, cwd=worktree, timeout_s=timeout_s)
    except (OSError, ValueError):
        return None
    answer = _parse_answer(text) if text else None
    if answer is None:
        return None
    spec: dict[str, object] = {key: _strings(answer.get(key)) for key in _LIST_FIELDS}
    spec["unverified"] = [_FALLBACK_NOTE, *_strings(answer.get("unverified"))]
    spec["pull_requests"] = [url for url in _strings(answer.get("pull_requests")) if url.startswith("https://")]
    if isinstance(answer.get("resume_point"), str):
        spec["resume_point"] = answer["resume_point"]
    done = cli.run(_create_args(ref, worktree, store_root, answer), timeout_s=_CALL_S, input_text=json.dumps(spec))
    if done.returncode != 0:
        return None
    try:
        handoff_id = json.loads(done.stdout)["handoff_id"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(handoff_id, str):
        return None
    _mark_fallback(store_root, handoff_id)  # best effort: the record already says so in its first `unverified` entry
    return handoff_id
