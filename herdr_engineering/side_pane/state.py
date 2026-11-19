"""The side pane's per-tab state under the engineering state directory: a marker, a dismissal and a lock.

- marker `<tab>.marker.json`: the pane that is this tab's side pane. `ensure` writes it when it opens the pane and
  `side-pane run` rewrites it with its pid and removes it on exit, so a pane is recognised by this file and not by its title.
- dismissal `<tab>.dismissed.json`: terminal ids of the sessions that were in the tab when the user closed the pane;
  the pane is not reopened for those sessions, only for a new one (or by `side-pane ensure`).
- lock `<tab>.lock`: `flock`, so two processes never both create the pane of one tab.
Pane ids are reused after a Herdr restart; terminal ids are not, so every check that matters compares terminal ids.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..handoff_controller.herdr_cli import AgentPane
from ..handoff_subagent_registry import sanitize, state_root

STARTUP_GRACE_S = 30.0  # a marker without a pid (written by `ensure`) counts as live this long while the runner starts


@dataclass(frozen=True)
class Marker:
    tab_id: str
    pane_id: str
    terminal_id: str
    session_terminal: str | None  # terminal of the session pane the side pane was opened beside
    pid: int | None  # the runner's pid; None until the runner has started
    created: float


def state_dir(env: Mapping[str, str]) -> Path:
    return state_root(env) / "side-pane"


def _path(root: Path, tab_id: str, kind: str) -> Path:
    return root / f"{sanitize(tab_id)}.{kind}"


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_marker(root: Path, tab_id: str) -> Marker | None:
    raw = _read(_path(root, tab_id, "marker.json"))
    if not isinstance(raw, dict):
        return None
    try:
        pid, created, session = raw.get("pid"), raw["created"], raw.get("session_terminal")
        if not isinstance(created, (int, float)) or not (pid is None or isinstance(pid, int)):
            return None
        if not all(isinstance(raw[k], str) for k in ("tab_id", "pane_id", "terminal_id")):
            return None
        return Marker(raw["tab_id"], raw["pane_id"], raw["terminal_id"], session if isinstance(session, str) else None,
                      pid, float(created))
    except KeyError:
        return None


def write_marker(root: Path, marker: Marker) -> None:
    _write(_path(root, marker.tab_id, "marker.json"), asdict(marker))


def clear_marker(root: Path, tab_id: str, pane_id: str | None = None) -> None:
    """Remove the tab's marker; with `pane_id`, only when it is that pane's (another process's marker stays)."""
    current = read_marker(root, tab_id)
    if current is not None and pane_id is not None and current.pane_id != pane_id:
        return
    _path(root, tab_id, "marker.json").unlink(missing_ok=True)


def read_dismissed(root: Path, tab_id: str) -> frozenset[str]:
    raw = _read(_path(root, tab_id, "dismissed.json"))
    return frozenset(t for t in raw if isinstance(t, str)) if isinstance(raw, list) else frozenset()


def dismiss(root: Path, tab_id: str, terminals: list[str]) -> None:
    _write(_path(root, tab_id, "dismissed.json"), sorted(read_dismissed(root, tab_id) | set(terminals)))


def clear_dismissed(root: Path, tab_id: str) -> None:
    _path(root, tab_id, "dismissed.json").unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def marker_live(marker: Marker, now: float) -> bool:
    """Whether the marker's runner is alive, or has not started yet (a marker `ensure` wrote, within the startup grace)."""
    if marker.pid is None:
        return 0 <= now - marker.created < STARTUP_GRACE_S
    return _alive(marker.pid)


def is_marked(root: Path, pane: AgentPane, now: float) -> bool:
    """Whether `pane` is the live side pane of its tab: the marker names it and its runner is alive (or has not started yet)."""
    if pane.tab_id is None:
        return False
    marker = read_marker(root, pane.tab_id)
    return marker is not None and marker.pane_id == pane.pane_id and marker_live(marker, now)


@contextmanager
def tab_lock(root: Path, tab_id: str, timeout_s: float) -> Iterator[bool]:
    """Hold the tab's lock; yields False (and holds nothing) when it could not be had within `timeout_s`."""
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(_path(root, tab_id, "lock"), os.O_RDWR | os.O_CREAT, 0o644)
    got = False
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
