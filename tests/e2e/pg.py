"""Throwaway PostgreSQL cluster on a unix socket for the restart/replay scenario (scenario 6).

Binaries come from tests/e2e/acceptance.yaml (postgres.bin_dirs). The cluster lives in a temp dir, listens on no TCP
port, and is stopped (fast shutdown) by `stop()`. Migrations are the repo's own deploy/control-plane/migrations."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from tests.e2e import harness


def _version_key(d: Path):
    v = d.parent.name
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def candidate_dirs(cfg: dict) -> list[Path]:
    """Configured dirs first, then every /usr/lib/postgresql/<ver>/bin (newest first), then wherever initdb is on PATH."""
    found = [Path(d) for d in (cfg.get("postgres") or {}).get("bin_dirs") or []]
    found += sorted(Path("/usr/lib/postgresql").glob("*/bin"), key=_version_key, reverse=True)
    on_path = shutil.which("initdb")
    if on_path:
        found.append(Path(on_path).resolve().parent)
    return found


def find_bins(cfg: dict) -> Path | None:
    for p in candidate_dirs(cfg):
        if (p / "initdb").exists() and (p / "pg_ctl").exists():
            return p
    return None


class ThrowawayPostgres:
    def __init__(self, bins: Path, workdir: Path, db: str = "herdr_e2e"):
        self.bins, self.root, self.db = bins, Path(workdir), db
        self.data = self.root / "pgdata"
        # unix socket paths are length-limited, so the socket dir is short and separate
        import tempfile
        self.sock = Path(tempfile.mkdtemp(prefix=f"t16pg-{os.getpid()}-"))
        self.started = False

    @property
    def dsn(self) -> str:
        return f"postgresql://postgres@/{self.db}?host={self.sock}"

    def start(self) -> ThrowawayPostgres:
        self.root.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(self.bins / "initdb"), "-D", str(self.data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                       check=True, capture_output=True)
        subprocess.run([str(self.bins / "pg_ctl"), "-D", str(self.data), "-w", "-l", str(self.root / "pg.log"),
                        "-o", f"-c listen_addresses='' -c unix_socket_directories='{self.sock}' -c fsync=off",
                        "start"], check=True, capture_output=True)
        self.started = True
        subprocess.run([str(self.bins / "createdb"), "-h", str(self.sock), "-U", "postgres", self.db],
                       check=True, capture_output=True)
        asyncio.run(self._migrate())
        return self

    def start_existing(self) -> ThrowawayPostgres:
        """Bring the same cluster (same data dir, same socket) back after `stop()`."""
        subprocess.run([str(self.bins / "pg_ctl"), "-D", str(self.data), "-w", "-l", str(self.root / "pg.log"),
                        "-o", f"-c listen_addresses='' -c unix_socket_directories='{self.sock}' -c fsync=off",
                        "start"], check=True, capture_output=True)
        self.started = True
        return self

    async def _migrate(self):
        import asyncpg

        from herdr_engineering.control_plane.db import Migrator
        conn = await asyncpg.connect(self.dsn)
        try:
            await Migrator(conn).apply(str(harness.ROOT / "deploy" / "control-plane" / "migrations"))
        finally:
            await conn.close()

    def query(self, sql: str, *args):
        async def run():
            import asyncpg
            conn = await asyncpg.connect(self.dsn)
            try:
                return await conn.fetch(sql, *args)
            finally:
                await conn.close()
        return asyncio.run(run())

    def stop(self) -> None:
        if self.started:
            subprocess.run([str(self.bins / "pg_ctl"), "-D", str(self.data), "-m", "fast", "-w", "stop"],
                           capture_output=True)
            self.started = False

    def close(self) -> None:
        """Final teardown: stop the cluster and remove the short socket dir this object created (and only that)."""
        self.stop()
        import shutil
        shutil.rmtree(self.sock, ignore_errors=True)
