"""Pi adapter: context from the session record, drain and exit by typing into the pane."""
import glob
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path

from .base import ContextReading, ExitPlan, Harness, SessionRef, TypedInput
from .herdr_session import herdr_agent_session, is_safe_session_id
from .records import records_newest_first
from .screen import screen_rows
from .windows import lookup_window

EDITOR_RULE = "─"  # Pi draws its input editor as a blank line between two rows of these; live pane, Pi 1.3.0, 2026-11-18
_READY_STATUSES = frozenset({"idle", "done"})
_LINES_BELOW_EDITOR = 3  # the footer rows under the closing rule; anything more means the editor is not last
_UUID_AT_END = re.compile(r"_([0-9A-Fa-f-]{36})$")  # <timestamp>_<uuid>.jsonl


def _slug(cwd: Path) -> str:
    """Pi's session directory name: one leading separator dropped, / \\ : as "-", wrapped in "--"."""
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", str(cwd))) + "--"


def session_id_from_record_path(path: str) -> str | None:
    """The session uuid in a Pi record's file name (Herdr reports Pi sessions by path), else None."""
    match = _UUID_AT_END.search(Path(path).stem)
    return match.group(1) if match else None


def session_id_from_agent_session(kind: str, value: str) -> str | None:
    """The session uuid from Herdr's agent_session (kind, value): an id as is, a record path by its file name."""
    if kind == "id":
        return value
    return session_id_from_record_path(value) if kind == "path" else None


def _usage(record: object) -> tuple[int, str | None, str | None] | None:
    """(context tokens, provider, model) of an assistant message that carries usage, else None.

    totalTokens is input + output + cacheRead + cacheWrite of the latest reply: the context with
    that reply in it, which is what the next request starts from. Aborted and errored replies
    record all zeros.
    """
    message = record.get("message") if isinstance(record, dict) and record.get("type") == "message" else None
    usage = message.get("usage") if isinstance(message, dict) and message.get("role") == "assistant" else None
    total = usage.get("totalTokens") if isinstance(usage, dict) else None
    if not isinstance(total, int) or total <= 0:
        return None  # no usage recorded; never read as 0%
    provider, model = message.get("provider"), message.get("model")
    return total, provider if isinstance(provider, str) else None, model if isinstance(model, str) else None


def last_usage(path: Path) -> tuple[int, str | None, str | None] | None:
    """The latest usage entry; a half-written line is skipped.

    A compaction after it makes it stale (the context just shrank): no reading until the next reply.
    """
    for record in records_newest_first(path):
        if record.get("type") == "compaction":
            return None
        if found := _usage(record):
            return found
    return None


def _configured_window(models_json: Path, provider: str | None, model: str | None) -> int | None:
    """contextWindow Pi's models.json lists for the provider's model, else None."""
    try:
        providers = json.loads(models_json.read_text(encoding="utf-8"))["providers"]
        entries = providers[provider]["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:  # one malformed entry must not hide the others
        if isinstance(entry, dict) and entry.get("id") == model:
            window = entry.get("contextWindow")
            if isinstance(window, int) and window > 0:
                return window
    return None


class PiAdapter:
    harness = Harness.PI

    def __init__(self, home: Path | None = None, env: Mapping[str, str] | None = None) -> None:
        self._home = home if home is not None else Path.home()
        self._env = env  # environment for the herdr call; None inherits the process environment

    def locate_session(self, session_id: str | None, cwd: Path, pane_id: str) -> SessionRef | None:
        if not is_safe_session_id(session_id):
            return None  # the id comes from outside; never let it leave the sessions tree or act as a glob
        sessions = self._home / ".pi" / "agent" / "sessions"
        # the cwd's directory first and alone if it matches; else any one directory (the pane cwd may have moved)
        for pattern in (f"{glob.escape(_slug(cwd))}/*_{session_id}.jsonl", f"*/*_{session_id}.jsonl"):
            matches = sorted(sessions.glob(pattern))
            if len(matches) == 1:
                return SessionRef(self.harness, pane_id, cwd, session_id, matches[0])
        return None

    def locate(self, pane_id: str, cwd: Path) -> SessionRef | None:
        session = herdr_agent_session(pane_id, self._env)
        if session is None:
            return None
        return self.locate_session(session_id_from_agent_session(*session), cwd, pane_id)

    def read_context(self, ref: SessionRef) -> ContextReading | None:
        if ref.record_path is None:
            return None
        try:
            found = last_usage(ref.record_path)
        except OSError:
            return None
        if found is None:
            return None
        used, provider, model = found
        recorded = _configured_window(self._home / ".pi" / "agent" / "models.json", provider, model)
        if recorded is not None:  # Pi's own configuration for that model, not a figure written with the usage entry
            window, window_source = recorded, "recorded"
        else:
            window, window_source = lookup_window(model)
        return ContextReading(used, window, "pi-session", window_source, time.time())

    def drain_input(self, order: str) -> TypedInput:
        return TypedInput(order, ("Enter",))

    def is_ready_for_input(self, screen: str, agent_status: str) -> bool:
        if agent_status not in _READY_STATUSES:
            return False
        lines = [plain for _, plain in screen_rows(screen)]  # a pane screen is padded with blank rows and coloured
        for index in range(len(lines) - 1, 1, -1):
            if lines[index] and set(lines[index]) == {EDITOR_RULE}:  # the closing rule: a bare row of rule characters
                return (not lines[index - 1] and set(lines[index - 2]) == {EDITOR_RULE}
                        and len(lines) - 1 - index <= _LINES_BELOW_EDITOR)
        return False

    def exit_plan(self) -> ExitPlan:
        return ExitPlan("/quit", ("Enter",), 15.0)

    def compact_input(self) -> TypedInput:
        return TypedInput("/compact", ("Enter",))
