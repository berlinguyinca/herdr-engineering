"""Claude Code adapter: context from the session transcript, drain and exit by typing into the pane."""
import json
import shutil
import subprocess
import time
from pathlib import Path

from .base import ContextReading, ExitPlan, Harness, SessionRef, TypedInput
from .windows import lookup_window

PROMPT_MARKER = "❯"  # the empty input box of Claude Code, on a line of its own between two rules
_READY_STATUSES = frozenset({"idle", "done"})
_LINES_BELOW_PROMPT = 4  # the closing rule and the status line(s); anything more means the box is not last
_SYNTHETIC_MODEL = "<synthetic>"  # zero-usage placeholder entries Claude Code writes for local events
_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")  # as handoff_context_hook
_FIRST_CHUNK = 1_048_576


def _slug(cwd: Path) -> str:
    return str(cwd).replace("/", "-").replace(".", "-")


def _main_chain_usage(record: object) -> tuple[int, str | None] | None:
    """(context tokens, model) of a main-session assistant entry that carries usage, else None."""
    if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain") is True:
        return None
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict) or message.get("model") == _SYNTHETIC_MODEL:
        return None
    model = message.get("model")
    return sum(v for k in _USAGE_KEYS if isinstance(v := usage.get(k), int)), model if isinstance(model, str) else None


def _last_usage(path: Path) -> tuple[int, str | None] | None:
    """Scan the transcript from the end in doubling chunks; a partial line at a chunk start or end is skipped."""
    size = path.stat().st_size
    chunk = _FIRST_CHUNK
    while True:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - chunk))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                found = _main_chain_usage(json.loads(line))
            except ValueError:
                continue
            if found:
                return found
        if chunk >= size:
            return None
        chunk *= 2


class ClaudeAdapter:
    harness = Harness.CLAUDE

    def __init__(self, home: Path | None = None) -> None:
        self._home = home if home is not None else Path.home()

    def locate_session(self, session_id: str | None, cwd: Path, pane_id: str) -> SessionRef | None:
        directory = self._home / ".claude" / "projects" / _slug(cwd)
        if session_id:
            path = directory / f"{session_id}.jsonl"
            return SessionRef(self.harness, pane_id, cwd, session_id, path) if path.is_file() else None
        transcripts = sorted(directory.glob("*.jsonl"))
        if len(transcripts) != 1:
            return None
        return SessionRef(self.harness, pane_id, cwd, transcripts[0].stem, transcripts[0])

    def locate(self, pane_id: str, cwd: Path) -> SessionRef | None:
        return self.locate_session(_herdr_session_id(pane_id), cwd, pane_id)

    def read_context(self, ref: SessionRef) -> ContextReading | None:
        if ref.record_path is None:
            return None
        try:
            found = _last_usage(ref.record_path)
        except OSError:
            return None
        if found is None:
            return None
        used, model = found
        window, window_source = lookup_window(model)  # the transcript records no window
        return ContextReading(used, window, "claude-transcript", window_source, time.time())

    def drain_input(self, order: str) -> TypedInput:
        return TypedInput(order, ("Enter",))

    def is_ready_for_input(self, screen: str, agent_status: str) -> bool:
        if agent_status not in _READY_STATUSES:
            return False
        lines = [line.strip() for line in screen.splitlines()]
        for index in range(len(lines) - 1, -1, -1):
            if lines[index] == PROMPT_MARKER:
                return len(lines) - 1 - index <= _LINES_BELOW_PROMPT
            if lines[index].startswith(PROMPT_MARKER):
                return False  # input already typed
        return False

    def exit_plan(self) -> ExitPlan:
        return ExitPlan("/exit", ("Enter",), 15.0)

    def compact_input(self) -> TypedInput:
        return TypedInput("/compact", ("Enter",))


def _herdr_session_id(pane_id: str) -> str | None:
    """The conversation id Herdr reports for the pane's agent (``herdr agent get``), or None."""
    exe = shutil.which("herdr")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "agent", "get", pane_id], capture_output=True, text=True, timeout=10)
        session = json.loads(out.stdout)["result"]["agent"].get("agent_session")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    value = session.get("value") if isinstance(session, dict) and session.get("kind") == "id" else None
    return value if isinstance(value, str) and value else None
