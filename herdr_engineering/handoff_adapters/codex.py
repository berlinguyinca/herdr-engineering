"""Codex adapter: context from the session rollout, drain and exit by typing into the pane."""
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .base import ContextReading, ExitPlan, Harness, SessionRef, TypedInput
from .herdr_session import herdr_session_id, is_safe_session_id
from .records import records_newest_first
from .screen import screen_rows, typed_text
from .windows import lookup_window

PROMPT_MARKER = "›"  # the Codex composer row, faint "Ask Codex to do anything" after it when empty; live pane, 0.169.0, 2026-11-18
_READY_STATUSES = frozenset({"idle", "done"})
_LINES_BELOW_PROMPT = 4  # the status line(s) under the composer; anything more means it is not last


_INTERACTIVE_ORIGINATOR = "codex-tui"  # session_meta.originator of a TUI session; `codex exec` writes "codex_exec" (0.169.0)
_ROLLOUT_NAME = re.compile(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(?P<id>.+)\.jsonl")


_ETIME = re.compile(r"(?:(?:(?P<d>\d+)-)?(?:(?P<h>\d+):)?)?(?P<m>\d+):(?P<s>\d+)")


def parse_etime(text: str) -> float | None:
    """Seconds in a `ps -o etime=` value (`[[dd-]hh:]mm:ss`), else None; a localised or odd value is not a time."""
    match = _ETIME.fullmatch(text.strip())
    if not match:
        return None
    d, h, m, s = (int(match[k] or 0) for k in ("d", "h", "m", "s"))
    return float(((d * 24 + h) * 60 + m) * 60 + s)


def process_start(pid: int, env: Mapping[str, str] | None) -> float | None:
    """Epoch seconds when the process started: now minus its elapsed time, which has no locale or time zone in it."""
    run_env = {**(os.environ if env is None else env), "LC_ALL": "C", "LANG": "C"}
    ps = shutil.which("ps", path=run_env.get("PATH", ""))
    if not ps:
        return None
    try:
        out = subprocess.run([ps, "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                             env=run_env).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    elapsed = parse_etime(out)
    return None if elapsed is None else time.time() - elapsed


def _session_meta(path: Path) -> tuple[str, str] | None:
    """(cwd, originator) from the session_meta record that opens a rollout, else None."""
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.loads(handle.readline()).get("payload")
        cwd, originator = payload.get("cwd"), payload.get("originator")
        return (cwd, originator) if isinstance(cwd, str) and isinstance(originator, str) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def select_rollout(pane_cwd: Path, process_start: float, open_paths: Sequence[Path], home: Path) -> Path | None:
    """The rollout of a Codex pane when Herdr reports no session id, or None when it cannot be told apart.

    A rollout the pane's process holds open is the answer. Otherwise it is the only interactive (`codex-tui`)
    rollout that records the pane's cwd and was written since the process started; `codex exec` runs are not
    sessions of a pane. The session's own start is not compared: a resumed session appends to its old rollout.
    Zero or several candidates give None; the newest file is never chosen.
    """
    root = home / ".codex" / "sessions"
    held = {p for p in open_paths if _ROLLOUT_NAME.fullmatch(p.name) and root in p.parents}
    if held:
        return next(iter(held)) if len(held) == 1 else None
    wanted = pane_cwd.resolve()
    found = []
    for path in sorted(root.glob("*/*/*/rollout-*.jsonl")):
        try:
            if path.stat().st_mtime < process_start:
                continue  # last written before the process existed; cannot be its session
        except OSError:
            continue
        meta = _session_meta(path)
        if meta is not None and meta[1] == _INTERACTIVE_ORIGINATOR and Path(meta[0]).resolve() == wanted:
            found.append(path)
    return found[0] if len(found) == 1 else None


def _process_signals(pane_id: str, env: Mapping[str, str] | None) -> tuple[float, list[Path], Path | None] | None:
    """(start time, open file paths, cwd) of the pane's one foreground `codex` process, else None.

    Open files are optional: no `lsof`, or a failing one, means none are known, not that there is no session.
    """
    herdr = shutil.which("herdr", path=None if env is None else env.get("PATH", ""))
    if not herdr:
        return None
    run_env = None if env is None else dict(env)
    try:
        info = subprocess.run([herdr, "pane", "process-info", "--pane", pane_id], capture_output=True, text=True,
                              timeout=10, env=run_env)
        procs = [p for p in json.loads(info.stdout)["result"]["process_info"]["foreground_processes"]
                 if p.get("name") == "codex" and isinstance(p.get("pid"), int)]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if len(procs) != 1:
        return None
    started = process_start(procs[0]["pid"], env)
    cwd = procs[0].get("cwd")
    if started is None:
        return None
    return started, _open_files(procs[0]["pid"], env), Path(cwd) if isinstance(cwd, str) and cwd else None


def _open_files(pid: int, env: Mapping[str, str] | None) -> list[Path]:
    lsof = shutil.which("lsof", path=None if env is None else env.get("PATH", ""))
    if not lsof:
        return []
    try:
        out = subprocess.run([lsof, "-p", str(pid), "-Fn"], capture_output=True, text=True, timeout=20,
                             env=None if env is None else dict(env)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(line[1:]) for line in out.splitlines() if line.startswith("n/")]


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
        session_id = herdr_session_id(pane_id, self._env)
        if session_id is not None:
            return self.locate_session(session_id, cwd, pane_id)  # Herdr's id wins
        signals = _process_signals(pane_id, self._env)  # Herdr reports no id for Codex; see the capabilities doc
        rollout = select_rollout(signals[2] or cwd, signals[0], signals[1], self._home) if signals else None
        named = _ROLLOUT_NAME.fullmatch(rollout.name) if rollout else None
        return self.locate_session(named["id"], cwd, pane_id) if named else None

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
