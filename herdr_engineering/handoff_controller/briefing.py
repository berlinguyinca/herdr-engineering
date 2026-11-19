"""Wait for the briefing a drained session writes, and write one with a second model call when it does not.

Secrets: the transcript tail goes only to the same harness the session already used (same provider). It is not
written to disk anywhere except inside the briefing the model returns, which `herdr-handoff create` redacts and
stores like any other briefing.

Isolation: the headless call is a model session with no tools, started in a fresh empty directory under
`<store_root>/fallback-work/`, with every `HERDR_*` variable and the harness's session-identity variables removed (it
must never look like, or act on, the Herdr pane and session it is writing about). A harness is enabled only when its
own `--help` shows a way to turn every tool off (nothing is run to check beyond `--help`; no real model call was made):

| Harness | Enabled | Flags | Unverified without a real call |
| --- | --- | --- | --- |
| claude | yes | `-p --output-format text --no-session-persistence --setting-sources "" --strict-mcp-config --disable-slash-commands --tools ""` | that an empty `--setting-sources` and `--tools` value are accepted and drop user/project hooks; prompt on stdin |
| pi | yes | `-p --no-session --no-tools --no-extensions --no-mcp --no-skills --no-prompt-templates --no-context-files` | prompt on stdin with no message argument; that `--no-extensions` leaves its provider login usable |
| codex | no | `codex exec` has only a read-only sandbox, no switch that disables tools | not enabled: tools cannot be shown off |
| opencode | no | `opencode run` has `--agent` and `--auto` but no way to deny tools from the command line | not enabled: tools cannot be shown off |

`wait_for_briefing` reads the store's on-disk layout (`<root>/outbox/handoff_<32 hex>.json`) rather than the CLI's
`list`: `list` fails as a whole on one corrupt record, and the wait must skip those.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
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
_LOG = logging.getLogger(__name__)
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


_HEADLESS: dict[Harness, list[str]] = {
    Harness.CLAUDE: ["claude", "-p", "--output-format", "text", "--no-session-persistence", "--setting-sources", "",
                     "--strict-mcp-config", "--disable-slash-commands", "--tools", ""],
    Harness.PI: ["pi", "-p", "--no-session", "--no-tools", "--no-extensions", "--no-mcp", "--no-skills",
                 "--no-prompt-templates", "--no-context-files"],
}
# What the adapters and `handoff_harness.py` read to tell which session they are in (HERDR_* is stripped by prefix).
# Auth and configuration (ANTHROPIC_*, OPENAI_*, HOME, PATH, CLAUDE_CODE_OAUTH_TOKEN, ...) are kept so the CLI can log in.
_SESSION_VARS = frozenset({
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_MANAGED_BY_NPM",
    "PI_CODING_AGENT", "PI_SESSION_ID", "OPENCODE", "OPENCODE_PID", "OPENCODE_SESSION_ID",
})


def fallback_supported(harness: Harness) -> bool:
    """True only for a harness whose `--help` shows how to disable every tool (see the module docstring)."""
    return harness in _HEADLESS


def headless_argv(harness: Harness) -> list[str] | None:
    """Non-interactive, tool-less argv, prompt on stdin; None when the harness is not supported."""
    argv = _HEADLESS.get(harness)
    return list(argv) if argv else None


def isolated_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """`base` (default: this process's environment) without the Herdr and harness-session identity variables."""
    return {k: v for k, v in (os.environ if base is None else base).items()
            if not k.startswith("HERDR_") and k not in _SESSION_VARS}


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
    """The production `HarnessRunner`: the session's own harness, headless, tool-less, without the session's identity."""
    argv = headless_argv(harness)
    if argv is None:
        _LOG.warning("handoff fallback writer: %s is not supported (no way to turn its tools off)", harness.value)
        return None
    return run_model(argv, prompt, cwd=cwd, timeout_s=timeout_s, env=isolated_env(env))


def _transcript_tail(ref: SessionRef, tail_bytes: int) -> str:
    path = ref.record_path
    if path is None or path.suffix in _DB_SUFFIXES:
        return "transcript: not available (the harness keeps its record in a database, which is not read)"
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):  # follows symlinks: never open a FIFO or a device
            return "transcript: not available (not a regular file)"
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - tail_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError as exc:
        return f"transcript: not available ({exc.strerror or type(exc).__name__})"
    if start > 0:  # the first line is cut in the middle
        data = data.partition(b"\n")[2]
    return "transcript tail (the last bytes of the session record, oldest first):\n" + data.decode("utf-8", "replace")


def _command_output(label: str, argv: list[str], cwd: Path, timeout_s: float) -> str:
    done = run_group(argv, timeout_s, cwd=cwd)
    if done.returncode != 0:  # stderr is not quoted: it can carry paths and tokens
        return f"{label}: not available ({Path(argv[0]).name if label.startswith('gh') else 'git'} failed, exit {done.returncode})"
    return f"{label}:\n{done.stdout.strip() or '(empty)'}"


def _prompt(ref: SessionRef, worktree: Path, tail_bytes: int, gh: tuple[str, ...], call_s: Callable[[], float]) -> str:
    sections = [
        _transcript_tail(ref, tail_bytes),
        _command_output("git status --short", ["git", "-C", str(worktree), "status", "--short"], worktree, call_s()),
        _command_output("git log -n 20 --oneline", ["git", "-C", str(worktree), "log", "-n", "20", "--oneline"], worktree, call_s()),
        _command_output("gh pr list", [*gh, "pr", "list", "--json", "number,title,headRefName"], worktree, call_s()),
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


def _create_args(ref: SessionRef, worktree: Path, store_root: Path, answer: Mapping[str, object],
                 native_fallback: bool) -> list[str]:
    """`--name=value` throughout: a model-supplied value that starts with `-` must not be read as a flag."""
    goal = answer.get("goal")
    args = ["--root", str(store_root), "create",
            "--goal=" + (goal if isinstance(goal, str) and goal.strip() else _UNKNOWN_GOAL),
            f"--source-harness={ref.harness.value}", f"--target-harness={ref.harness.value}",
            f"--source-pane={ref.pane_id}", f"--worktree={worktree.resolve()}",
            f"--workspace-cwd={ref.cwd.resolve()}", "--from-json", "-"]
    if ref.session_id:
        args += [f"--session-id={ref.session_id}", f"--source-session={ref.session_id}"]
    if native_fallback:
        args.append("--fallback")
    return args


def _mark_fallback(store_root: Path, handoff_id: str) -> bool:
    """Until `herdr-handoff create --fallback` exists (0.5.0 will have it) there is no provenance option; set `provenance.fallback` in the record it just wrote."""
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
    (unsupported harness, no answer, unparsable answer, `herdr-handoff` refusing it). `timeout_s` bounds the whole call,
    each step getting what is left. A missing transcript or `gh` is noted in the prompt."""
    if not fallback_supported(ref.harness):
        _LOG.warning("handoff fallback writer: %s is not supported", ref.harness.value)
        return None
    cli = handoff or HandoffCli()
    deadline = time.monotonic() + timeout_s

    def left() -> float:
        return max(0.0, deadline - time.monotonic())

    def call_s() -> float:
        return min(_CALL_S, left())

    work_parent = store_root / "fallback-work"
    try:
        work_parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(dir=work_parent))
    except OSError:
        return None
    try:
        prompt = _prompt(ref, worktree, transcript_tail_bytes, gh, call_s)
        if left() <= 0:
            return None
        try:
            text = harness_runner(ref.harness, prompt, cwd=work, timeout_s=left())
        except Exception:  # noqa: BLE001 - the runner is injected code; whatever it raises must not escape the controller
            _LOG.exception("handoff fallback writer: the harness runner raised")
            return None
        answer = _parse_answer(text) if text else None
        if answer is None or left() <= 0:
            return None
        spec: dict[str, object] = {key: _strings(answer.get(key)) for key in _LIST_FIELDS}
        spec["unverified"] = [_FALLBACK_NOTE, *_strings(answer.get("unverified"))]
        spec["pull_requests"] = [url for url in _strings(answer.get("pull_requests")) if url.startswith("https://")]
        if isinstance(answer.get("resume_point"), str):
            spec["resume_point"] = answer["resume_point"]
        helped = cli.run(["create", "--help"], timeout_s=call_s())
        native = helped.returncode == 0 and "--fallback" in helped.stdout
        done = cli.run(_create_args(ref, worktree, store_root, answer, native), timeout_s=call_s(),
                       input_text=json.dumps(spec))
        if done.returncode != 0:
            return None
        try:
            handoff_id = json.loads(done.stdout)["handoff_id"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(handoff_id, str):
            return None
        if not native:
            _mark_fallback(store_root, handoff_id)  # best effort: the first `unverified` entry says so as well
        return handoff_id
    finally:
        shutil.rmtree(work, ignore_errors=True)
