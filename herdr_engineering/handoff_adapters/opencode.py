"""OpenCode adapter: context from OpenCode's SQLite database, drain and exit by typing into the pane."""
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import closing
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
from .screen import screen_rows
from .windows import lookup_window

# The prompt box (OpenCode 1.18.23, live panes, 2026-11-18): rows starting with PROMPT_BAR, the last of them naming the
# agent and model, closed by a BOX_BOTTOM row. A fresh session's empty box shows the placeholder `Ask anything... "<example>"`
# in it; after a turn the empty box is bare PROMPT_BAR rows.
PROMPT_PLACEHOLDER = "Ask anything"
PROMPT_BAR = "┃"
BOX_BOTTOM = "╹▀"
UPDATE_DIALOG = "Skip  Confirm"  # the "Update Available" modal: drawn over the screen, the box stays visible behind it
_ROWS_BELOW_BOX = 4  # non-blank rows under the box: hints, tip and the status bar (1 after a turn, 3 in a fresh session)
_BUSY_TIMEOUT_S = 0.25  # a busy database must not hold the controller up; the next poll asks again
_USAGE_FIELDS = ("input", "output", "reasoning")


def _xdg(env: Mapping[str, str], name: str, default: Path) -> Path:
    value = env.get(name)
    return Path(value) if value else default


def connect(db: Path) -> sqlite3.Connection:
    """A read-only connection: never writes, never creates a missing database."""
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)


def _count(value: object) -> int:
    """A token count, else 0 (a bool is not a count)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _context_tokens(tokens: object) -> int | None:
    """Tokens in the context after this reply, else None when none were recorded.

    `total` is input + output + reasoning + cache read + cache write of the reply (checked on real rows);
    a reply that never completed records all zeros and no total. When total is absent or 0 the parts are summed.
    """
    if not isinstance(tokens, dict):
        return None
    total = _count(tokens.get("total"))
    if total <= 0:
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        total = sum(_count(tokens.get(key)) for key in _USAGE_FIELDS) + _count(cache.get("read")) + _count(cache.get("write"))
    return total if total > 0 else None


def _assistant_messages(con: sqlite3.Connection, session_id: str) -> Iterator[dict]:
    """The session's assistant message payloads, newest first; an unreadable payload is skipped."""
    rows = con.execute("select data from message where session_id = ? order by time_created desc, id desc", (session_id,))
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("role") == "assistant":
            yield data


def last_usage(db: Path, session_id: str) -> tuple[int, str | None, str | None] | None:
    """(context tokens, provider, model) of the session's latest reply with usage, else None.

    A compaction summary after it makes it stale (the context just shrank): no reading until the next reply.
    """
    with closing(connect(db)) as con:
        for data in _assistant_messages(con, session_id):
            # a summary reply is `{"mode": "compaction", "summary": true}` in the real database (142 rows seen)
            if data.get("mode") == "compaction" or data.get("summary") is True:
                return None
            used = _context_tokens(data.get("tokens"))
            if used is not None:
                provider, model = data.get("providerID"), data.get("modelID")
                return used, provider if isinstance(provider, str) else None, model if isinstance(model, str) else None
    return None


def _models_window(path: Path, provider: str | None, model: str | None, *, config: bool) -> int | None:
    """limit.context for provider/model in OpenCode's config (provider.<p>.models) or model catalogue (<p>.models), else None."""
    try:
        providers = json.loads(path.read_text(encoding="utf-8"))
        models = (providers["provider"] if config else providers)[provider]["models"]
        window = models[model]["limit"]["context"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return window if isinstance(window, int) and window > 0 else None


class OpenCodeAdapter:
    harness = Harness.OPENCODE

    def __init__(self, home: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self._home = home if home is not None else Path.home()
        self._env = env  # environment for the herdr call and the XDG directories; None inherits the process environment

    def _xdg(self, name: str, default: str) -> Path:
        return _xdg(os.environ if self._env is None else self._env, name, self._home / default)

    def _db_path(self) -> Path:
        return self._xdg("XDG_DATA_HOME", ".local/share") / "opencode" / "opencode.db"

    def locate_session(self, session_id: str | None, cwd: Path, pane_id: str) -> SessionRef | None:
        if session_id is None or not is_safe_session_id(session_id):
            return None  # no id from Herdr means unknown: a lone session in the cwd may be an older one
        # An id is matched on its own, without comparing the session's directory with cwd: a pane's cwd can differ
        # from where the session was started. That is only safe because the id comes from Herdr for this pane.
        db = self._db_path()
        try:
            with closing(connect(db)) as con:
                found = con.execute("select id from session where id = ?", (session_id,)).fetchall()
        except sqlite3.Error:
            return None
        if len(found) != 1:
            return None
        return SessionRef(self.harness, pane_id, cwd, found[0][0], db)

    def locate(self, pane_id: str, cwd: Path) -> SessionRef | None:
        return self.locate_session(herdr_session_id(pane_id, self._env), cwd, pane_id)

    def _window(self, provider: str | None, model: str | None) -> tuple[int, WindowSource]:
        """The context window OpenCode's own configuration or cached catalogue lists, else the model table."""
        config = self._xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "opencode.json"
        catalogue = self._xdg("XDG_CACHE_HOME", ".cache") / "opencode" / "models.json"
        for path, is_config in ((config, True), (catalogue, False)):
            recorded = _models_window(path, provider, model, config=is_config)
            if recorded is not None:
                return recorded, "recorded"
        return lookup_window(model)

    def read_context(self, ref: SessionRef) -> ContextReading | None:
        if ref.record_path is None or ref.session_id is None:
            return None
        try:
            found = last_usage(ref.record_path, ref.session_id)
        except sqlite3.Error:  # locked, busy, unreadable or not a database: no reading this time
            return None
        if found is None:
            return None
        used, provider, model = found
        window, window_source = self._window(provider, model)
        return ContextReading(used, window, "opencode-db", window_source, time.time())

    def drain_input(self, order: str) -> TypedInput:
        return TypedInput(order, ("Enter",))

    def is_ready_for_input(self, screen: str, agent_status: str) -> bool:
        if agent_status not in READY_STATUSES:
            return False
        rows = [plain for _, plain in screen_rows(screen)]  # coloured, with blank rows inside the layout
        if any(UPDATE_DIALOG in row for row in rows):
            return False  # a modal has the keys, whatever the box behind it shows
        bottom = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].startswith(BOX_BOTTOM)), None)
        if bottom is None or bottom == 0 or sum(1 for row in rows[bottom + 1:] if row) > _ROWS_BELOW_BOX:
            return False
        if not rows[bottom - 1].startswith(PROMPT_BAR) or rows[bottom - 1] == PROMPT_BAR:
            return False  # the row above the bottom edge names the agent and model
        inputs: list[str] = []
        for row in reversed(rows[:bottom - 1]):  # the box's input rows: contiguous, up to the first row without the bar
            if not row.startswith(PROMPT_BAR):
                break
            inputs.append(row[len(PROMPT_BAR):].strip())
        return bool(inputs) and all(not text or text.startswith(PROMPT_PLACEHOLDER) for text in inputs)

    def exit_plan(self) -> ExitPlan:
        # measured in a live pane (OpenCode 1.18.23, 2026-11-18): /exit then Enter ended the process in 0.4 s; the 15 s is margin
        return ExitPlan("/exit", ("Enter",), 15.0)

    def compact_input(self) -> TypedInput:
        # measured live (OpenCode 1.18.23, 2026-11-18): once a session exists the slash menu lists `/compact  Compact session`
        # (typed, not run); a session-less pane does not list it
        return TypedInput("/compact", ("Enter",))
