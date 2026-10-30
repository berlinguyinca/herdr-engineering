"""PostgreSQL fixture for the Work Graph tests: a private socket-only server (initdb) with every control-plane migration
applied, or -- when WG_TEST_PG_DSN names a server (the uw-stack) -- a scratch database inside it that is dropped afterwards."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.work_graph_store import init_connection

ROOT = Path(__file__).resolve().parents[2]


async def _migrate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await Migrator(conn).apply(str(ROOT / "deploy/control-plane/migrations"))
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def wg_pg():
    external = os.environ.get("WG_TEST_PG_DSN")
    if external:
        name = "wg_" + uuid.uuid4().hex[:10]

        async def admin(sql):
            c = await asyncpg.connect(external)
            try:
                await c.execute(sql)
            finally:
                await c.close()
        asyncio.run(admin(f'CREATE DATABASE "{name}"'))
        base, _, _ = external.rpartition("/")
        dsn = f"{base}/{name}"
        asyncio.run(_migrate(dsn))
        try:
            yield {"dsn": dsn}
        finally:
            asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        return
    if os.getuid() == 0:
        pytest.skip("isolated initdb cannot run as root")
    config = shutil.which("pg_config")
    candidates = []
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next((c for c in candidates if c.is_file()), None)
    if initdb is None:
        pytest.skip("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="wg-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)

        def pg_ctl(*args):
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), *args], check=True, capture_output=True)

        pg_ctl("-l", str(area / "server.log"), "-o", f"-c listen_addresses='' -k {area} -c fsync=off", "-w", "start")
        try:
            dsn = f"postgresql:///postgres?host={area}"
            asyncio.run(_migrate(dsn))
            yield {"dsn": dsn}
        finally:
            pg_ctl("-m", "fast", "-w", "stop")


class Runner:
    """One event loop for a test module's pool (asyncpg objects belong to the loop that made them)."""

    def __init__(self, dsn: str):
        self.loop = asyncio.new_event_loop()
        async def make():
            return await asyncpg.create_pool(dsn, min_size=1, max_size=6, init=init_connection)
        self.pool = self.loop.run_until_complete(make())

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def close(self):
        self.loop.run_until_complete(self.pool.close())
        self.loop.close()


@pytest.fixture()
def wg(wg_pg):
    r = Runner(wg_pg["dsn"])
    r.run(r.pool.execute("TRUNCATE wg_edge_interval, wg_alias, wg_edge, wg_fact, wg_entity, wg_assertion, wg_event, wg_cursor, "
                         "wg_gap, wg_layout RESTART IDENTITY CASCADE"))
    try:
        yield r
    finally:
        r.close()
