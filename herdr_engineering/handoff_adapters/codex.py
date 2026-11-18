"""Codex adapter: context from the session rollout, drain and exit by typing into the pane."""
import json
import time
from collections.abc import Mapping
from pathlib import Path

from .base import ContextReading, ExitPlan, Harness, SessionRef, TypedInput
from .herdr_session import herdr_session_id
from .windows import lookup_window

PROMPT_MARKER = "›"  # the empty Codex composer; unconfirmed against a live pane, measured in Task 7
_READY_STATUSES = frozenset({"idle", "done"})
_LINES_BELOW_PROMPT = 4  # the status line(s) under the composer; anything more means it is not last
_FIRST_CHUNK = 1_048_576


def _token_count(record: object) -> tuple[int, int | None] | None:
    """(context tokens, recorded window) of a token_count event that carries usage, else None.

    last_token_usage is the size of the latest request, i.e. the context; total_token_usage is the
    cumulative bill and keeps growing past the window, so it is not used.
    """
    payload = record.get("payload") if isinstance(record, dict) and record.get("type") == "event_msg" else None
    info = payload.get("info") if isinstance(payload, dict) and payload.get("type") == "token_count" else None
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    used = last.get("total_tokens") if isinstance(last, dict) else None
    if not isinstance(used, int) or used <= 0:
        return None  # no usage recorded yet; never read as 0%
    window = info.get("model_context_window")
    return used, window if isinstance(window, int) and window > 0 else None


def _turn_model(record: object) -> str | None:
    payload = record.get("payload") if isinstance(record, dict) and record.get("type") == "turn_context" else None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model if isinstance(model, str) and model else None


def _last_usage(path: Path) -> tuple[int, int | None, str | None] | None:
    """(tokens, recorded window, model) of the latest usage event; scans from the end in doubling chunks.

    The model is read only when the event records no window: the turn_context before it. A partial
    line at a chunk start or end is skipped.
    """
    size = path.stat().st_size
    chunk = _FIRST_CHUNK
    while True:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - chunk))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        found: tuple[int, int | None] | None = None
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if found is None:
                found = _token_count(record)
                if found and found[1] is not None:
                    return found[0], found[1], None
            elif (model := _turn_model(record)) is not None:
                return found[0], None, model
        if chunk >= size:
            return (found[0], None, None) if found else None
        chunk *= 2


class CodexAdapter:
    harness = Harness.CODEX

    def __init__(self, home: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self._home = home if home is not None else Path.home()
        self._env = env  # environment for the herdr call; None inherits the process environment

    def locate_session(self, session_id: str | None, cwd: Path, pane_id: str) -> SessionRef | None:
        if not session_id or any(c in session_id for c in "/\\*?[") or session_id == "..":
            return None  # the id comes from outside; never let it leave the sessions tree or act as a glob
        matches = sorted((self._home / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
        if len(matches) != 1:
            return None
        return SessionRef(self.harness, pane_id, cwd, session_id, matches[0])

    def locate(self, pane_id: str, cwd: Path) -> SessionRef | None:
        return self.locate_session(herdr_session_id(pane_id, self._env), cwd, pane_id)

    def read_context(self, ref: SessionRef) -> ContextReading | None:
        if ref.record_path is None:
            return None
        try:
            found = _last_usage(ref.record_path)
        except OSError:
            return None
        if found is None:
            return None
        used, recorded, model = found
        if recorded is not None:
            window, window_source = recorded, "recorded"
        else:
            window, window_source = lookup_window(model)
        return ContextReading(used, window, "codex-rollout", window_source, time.time())

    def drain_input(self, order: str) -> TypedInput:
        return TypedInput(order, ("Enter",))

    def is_ready_for_input(self, screen: str, agent_status: str) -> bool:
        if agent_status not in _READY_STATUSES:
            return False
        lines = [line.strip() for line in screen.rstrip().splitlines()]  # a pane screen is padded with blank rows
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
