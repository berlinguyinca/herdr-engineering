"""Codex adapter: context from the session rollout, drain and exit by typing into the pane."""
import time
from collections.abc import Mapping
from pathlib import Path

from .base import ContextReading, ExitPlan, Harness, SessionRef, TypedInput
from .herdr_session import herdr_session_id, is_safe_session_id
from .records import records_newest_first
from .screen import screen_rows, typed_text
from .windows import lookup_window

PROMPT_MARKER = "›"  # the Codex composer row, faint "Ask Codex to do anything" after it when empty; live pane, 0.169.0, 2026-11-18
_READY_STATUSES = frozenset({"idle", "done"})
_LINES_BELOW_PROMPT = 4  # the status line(s) under the composer; anything more means it is not last


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


def turn_model(record: object) -> str | None:
    payload = record.get("payload") if isinstance(record, dict) and record.get("type") == "turn_context" else None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model if isinstance(model, str) and model else None


def last_usage(path: Path) -> tuple[int, int | None, str | None] | None:
    """(tokens, recorded window, model) of the latest usage event, scanning from the end.

    A top-level ``compacted`` record after it makes it stale: no reading until the next token_count.
    The model is read only when the event records no window: the turn_context before it. A half-written
    line is skipped.
    """
    found: tuple[int, int | None] | None = None
    for record in records_newest_first(path):
        if found is None:
            if record.get("type") == "compacted":
                return None  # the usage before a compaction is the context that was just discarded
            found = _token_count(record)
            if found and found[1] is not None:
                return found[0], found[1], None
        elif (model := turn_model(record)) is not None:
            return found[0], None, model
    return (found[0], None, None) if found else None


class CodexAdapter:
    harness = Harness.CODEX

    def __init__(self, home: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self._home = home if home is not None else Path.home()
        self._env = env  # environment for the herdr call; None inherits the process environment

    def locate_session(self, session_id: str | None, cwd: Path, pane_id: str) -> SessionRef | None:
        if not is_safe_session_id(session_id):
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
            found = last_usage(ref.record_path)
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
        rows = screen_rows(screen)  # Herdr's screen carries colour codes; the prompt's faint suggestion is not typed text
        for index in range(len(rows) - 1, -1, -1):
            typed = typed_text(rows[index][0], PROMPT_MARKER)
            if typed is not None:
                return not typed and len(rows) - 1 - index <= _LINES_BELOW_PROMPT
        return False

    def exit_plan(self) -> ExitPlan:
        return ExitPlan("/exit", ("Enter",), 15.0)

    def compact_input(self) -> TypedInput:
        return TypedInput("/compact", ("Enter",))
