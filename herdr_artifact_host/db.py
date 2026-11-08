"""Host database: asyncpg pool and the forward-only SQL migrator."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable

import asyncpg

_LOCK_KEY = 0x48415F4D4947  # advisory lock id for migrations ("HA_MIG")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)


def packaged_migrations() -> Traversable:
    return resources.files("herdr_artifact_host") / "migrations"


class Migrator:
    """Apply ``*.sql`` files in name order, once each, one transaction per file."""

    def __init__(self, pool: asyncpg.Pool, directory: Traversable | None = None) -> None:
        self._pool = pool
        self._dir = directory if directory is not None else packaged_migrations()

    def _files(self) -> list[tuple[str, str]]:
        found = [(p.name, p.read_text(encoding="utf-8")) for p in self._dir.iterdir() if p.name.endswith(".sql")]
        return sorted(found)

    async def migrate(self) -> list[str]:
        """Returns the names applied by this call."""
        applied: list[str] = []
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
            try:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
                for name, sql in self._files():
                    if name in done:
                        continue
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
                    applied.append(name)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
        return applied
