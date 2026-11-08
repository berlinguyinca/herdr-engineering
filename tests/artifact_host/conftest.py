"""Real PostgreSQL for the host tests: an isolated, Unix-socket-only server (skips when binaries are missing)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import asyncpg
import pytest

from herdr_artifact_host.db import Migrator, create_pool


@pytest.fixture(scope="session")
def host_postgres_server() -> Iterator[tuple[str, Path]]:
    strict = bool(os.environ.get("CI")) or os.environ.get("HERDR_REQUIRE_PG") == "1"

    def unavailable(reason: str) -> None:
        # In CI (or when forced) a missing database is a failure, never a silent skip.
        if strict:
            pytest.fail(reason)
        pytest.skip(reason)

    if os.getuid() == 0:
        unavailable("isolated initdb cannot run as root")
    candidates: list[Path] = []
    config = shutil.which("pg_config")
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next((c for c in candidates if c.is_file()), None)
    if initdb is None:
        unavailable("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="host-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "server.log"),
                        "-o", f"-c listen_addresses='' -k {area} -c dynamic_shared_memory_type=mmap", "-w", "start"],
                       check=True, capture_output=True)
        try:
            yield f"postgresql:///postgres?host={area}", area
        finally:
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True)


@pytest.fixture
def host_dsn_factory(host_postgres_server: tuple[str, Path]) -> Iterator[Callable[[], str]]:
    """Returns a function creating a fresh empty database and yielding its DSN."""
    admin_dsn, area = host_postgres_server

    def make() -> str:
        name = "h_" + uuid.uuid4().hex[:12]

        async def go() -> None:
            conn = await asyncpg.connect(admin_dsn)
            try:
                await conn.execute(f'CREATE DATABASE "{name}"')
            finally:
                await conn.close()

        asyncio.run(go())
        return f"postgresql:///{name}?host={area}"

    yield make


@pytest.fixture
def host_dsn(host_dsn_factory: Callable[[], str]) -> str:
    """A fresh, empty (unmigrated) database."""
    return host_dsn_factory()


@pytest.fixture
def migrated_dsn(host_dsn: str) -> str:
    async def go() -> None:
        pool = await create_pool(host_dsn)
        try:
            await Migrator(pool).migrate()
        finally:
            await pool.close()

    asyncio.run(go())
    return host_dsn
