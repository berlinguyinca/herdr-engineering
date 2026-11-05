"""Durable watcher state (spec section 5): `<XDG_STATE_HOME>/herdr-engineering/fleet-watch/state.json`.

Same location convention as `fleet_autoupdate.state_path`; private (0700 dir, 0600 file), written
atomically, and guarded by a flock so only one watcher pass runs at a time.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

SCHEMA_VERSION = 1


class WatcherBusy(RuntimeError):
    """Another watcher pass holds the single-instance lock."""


def state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "herdr-engineering" / "fleet-watch"


def empty_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "repos": {}, "converged": {}, "rollouts": {}}


def load(directory: Path | None = None) -> dict:
    """The saved state; a missing file is empty, a corrupt one is set aside and replaced."""
    path = (directory or state_dir()) / "state.json"
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return empty_state()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unexpected shape")
    except ValueError:
        with contextlib.suppress(OSError):
            os.replace(path, path.with_name(f"state.json.corrupt-{int(time.time())}"))
        return empty_state()
    for key, default in empty_state().items():
        data.setdefault(key, default)
    return data


def save(state: dict, directory: Path | None = None) -> Path:
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "state.json"
    tmp = path.with_name("state.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(state, sort_keys=True, indent=1) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


@contextlib.contextmanager
def single_instance(directory: Path | None = None) -> Iterator[None]:
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / "lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WatcherBusy("another fleet-watch pass holds the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
