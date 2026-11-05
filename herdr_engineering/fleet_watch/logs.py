"""Host-side log store (decision 2026-11-05): rollout log text never leaves the host.

Events carry only `{host, path_id, sha256, bytes, lines}`; the text lives here, under the private (0700/0600) state
directory, and is read back by `herdr-eng fleet-watch log show`. Only the last `MAX_BYTES` of a log are kept, and
logs older than `RETENTION_DAYS` are deleted when a new one is written.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..control_plane.fleet_rollout import HOST_ORDER, LOG_KINDS
from . import state

MAX_BYTES = 1_000_000
RETENTION_DAYS = 30
_ID = re.compile(r"[0-9a-f]{16}")


class LogNotFound(LookupError):
    """No stored log for that rollout, host and kind."""


class LogStore:
    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory else state.state_dir() / "logs"

    def _path(self, rollout_id: str, host: str, path_id: str) -> Path:
        if not _ID.fullmatch(rollout_id) or host not in HOST_ORDER or path_id not in LOG_KINDS:
            raise ValueError("rollout_id, host and path_id must be a 16-hex id, a fleet host and a log kind")
        return self.directory / rollout_id / f"{host}-{path_id}.log"

    def write(self, rollout_id: str, host: str, path_id: str, text: str, *, now: datetime | None = None) -> dict:
        """Store `text` (its last 1 MB) and return the event reference for exactly the bytes stored."""
        path = self._path(rollout_id, host, path_id)
        data = text.encode("utf-8", "replace")[-MAX_BYTES:]
        state.private_dir(path.parent)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        self.prune(now or datetime.now(UTC))
        return {"host": host, "path_id": path_id, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "lines": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)}

    def read(self, rollout_id: str, host: str, path_id: str) -> str:
        try:
            return self._path(rollout_id, host, path_id).read_text(errors="replace")
        except FileNotFoundError:
            raise LogNotFound(f"no {path_id} log for {host} in rollout {rollout_id}") from None

    def kinds(self, rollout_id: str, host: str) -> list[str]:
        return [k for k in LOG_KINDS if self._path(rollout_id, host, k).exists()]

    def prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=RETENTION_DAYS)).timestamp()
        if not self.directory.exists():
            return
        for folder in self.directory.iterdir():
            if not folder.is_dir():
                continue
            for log in folder.glob("*.log"):
                if log.stat().st_mtime < cutoff:
                    log.unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
