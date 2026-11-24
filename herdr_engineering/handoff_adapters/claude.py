"""Claude Code adapter: context from the session transcript, drain and exit by typing into the pane."""
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path

from .base import (
    READY_STATUSES,
    ContextReading,
    ExitPlan,
    Harness,
    SessionRef,
    TypedInput,
    WindowSource,
)
from .herdr_session import herdr_session_id, is_safe_session_id
from .records import records_newest_first
from .screen import composer_is_empty, last_marker_row, screen_rows
from .windows import lookup_window, strip_date_suffix

PROMPT_MARKER = "❯"  # the input box row of Claude Code between two rules, a faint suggestion after it when empty; live pane, 2.1.306, 2026-11-18
_RULE = "─"  # the box is closed by a row of these
_LINES_BELOW_CLOSING_RULE = 3  # the status line(s); anything more means the box is not last
_SYNTHETIC_MODEL = "<synthetic>"  # zero-usage placeholder entries Claude Code writes for local events
_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _slug(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))  # e.g. m_/x.y -> m--x-y, as in the real ~/.claude/projects names


def _main_chain_usage(record: object) -> tuple[int, str | None] | None:
    """(context tokens, model) of a main-session assistant entry that carries usage, else None."""
    if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain") is True:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or message.get("model") == _SYNTHETIC_MODEL:
        return None
    model = message.get("model")
    used = sum(v for k in _USAGE_KEYS if isinstance(v := usage.get(k), int))
    if used <= 0:
        return None  # no usage recorded yet; never read as 0%
    return used, model if isinstance(model, str) else None


def last_usage(path: Path) -> tuple[int, str | None] | None:
    """The latest main-chain usage; a half-written line is skipped.

    A compact_boundary after it makes it stale (the context just shrank): no reading until the next reply.
    """
    for record in records_newest_first(path):
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary" and record.get("isSidechain") is not True:
            return None
        if found := _main_chain_usage(record):
            return found
    return None


def _catalog_window(home: Path, model: str | None) -> int | None:
    """runtime.max_input_tokens Claude Code's cached model catalogue lists for the model, else None.

    The catalogue (~/.claude/cache/model-catalog/<org>-<hash>-cc.json, one file per account) is Claude Code's own
    record of each model's limits. The newest fetchedAt across the files that list the model wins; every call
    globs and parses every file (one per account, about 20 KB each, so well under a millisecond each), which is
    cheaper than the invalidation a cache would need. A dated and an undated spelling of an id are the same model.
    """
    wanted = strip_date_suffix(model) if model else None
    best: tuple[int, int] | None = None  # (fetchedAt, window)
    for path in (home / ".claude" / "cache" / "model-catalog").glob("*-cc.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fetched = data["fetchedAt"]
            models = data["catalog"]["config"]["models"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(fetched, int) or not isinstance(models, list):
            continue
        for entry in models:  # one malformed entry must not hide the others
            runtime = entry.get("runtime") if isinstance(entry, dict) and isinstance(entry.get("id"), str) and strip_date_suffix(entry["id"]) == wanted else None
            window = runtime.get("max_input_tokens") if isinstance(runtime, dict) else None
            if isinstance(window, int) and not isinstance(window, bool) and window > 0 and (best is None or fetched > best[0]):
                best = (fetched, window)
    return best[1] if best else None


def _resolve_window(recorded: int | None, used: int, model: str | None) -> tuple[int, WindowSource]:
    """Window from the catalogue (`recorded`) and the table; they can disagree and nobody has measured which is right.

    A window that is too large makes a nearly full session look empty and a handoff never fires; one that is too small
    only hands off early. So on a conflict the smaller wins, unless the session's own usage already exceeds it, which
    proves the smaller one wrong for this session.
    """
    table, table_source = lookup_window(model)
    if recorded is None:
        return table, table_source
    if table_source == "fallback" or recorded == table:
        return recorded, "recorded"
    smaller, larger = sorted((recorded, table))
    if used > smaller:
        return larger, "recorded" if larger == recorded else "model_table"
    return smaller, "recorded" if smaller == recorded else "model_table"


class ClaudeAdapter:
    harness = Harness.CLAUDE

    def __init__(self, home: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self._home = home if home is not None else Path.home()
        self._env = env  # environment for the herdr call; None inherits the process environment

    def locate_session(self, session_id: str | None, cwd: Path, pane_id: str) -> SessionRef | None:
        if not is_safe_session_id(session_id):
            return None  # no id from Herdr means unknown (a lone transcript may be an older session); the id comes from outside, never let it leave the project directory
        path = self._home / ".claude" / "projects" / _slug(cwd) / f"{session_id}.jsonl"
        return SessionRef(self.harness, pane_id, cwd, session_id, path) if path.is_file() else None

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
        used, model = found
        window, window_source = _resolve_window(_catalog_window(self._home, model), used, model)  # transcripts record none
        return ContextReading(used, window, "claude-transcript", window_source, time.time())

    def drain_input(self, order: str) -> TypedInput:
        return TypedInput(order, ("Enter",))

    def is_ready_for_input(self, screen: str, agent_status: str) -> bool:
        if agent_status not in READY_STATUSES:
            return False
        rows = screen_rows(screen)  # Herdr's screen carries colour codes; the prompt's faint suggestion is not typed text
        marker = last_marker_row(rows, PROMPT_MARKER)
        if marker is None:
            return False
        index, typed = marker
        closing = next((i for i in range(index + 1, len(rows)) if set(rows[i][1]) == {_RULE}), None)
        return (closing is not None and composer_is_empty(rows, index, typed, closing)
                and len(rows) - 1 - closing <= _LINES_BELOW_CLOSING_RULE)

    def exit_plan(self) -> ExitPlan:
        return ExitPlan("/exit", ("Enter",), 15.0)

    def compact_input(self) -> TypedInput:
        return TypedInput("/compact", ("Enter",))
