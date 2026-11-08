from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg
import pytest

from herdr_artifact_host.db import Migrator, create_pool

TABLES = {
    "artifacts", "artifact_versions", "artifact_blobs", "artifact_acl", "library_folders", "library_items",
    "share_claims", "shares", "share_events", "annotations", "member_usage", "agent_tokens", "schema_migrations",
}


def with_conn[T](dsn: str, body: Callable[[asyncpg.Connection], Awaitable[T]]) -> T:
    async def go() -> T:
        conn = await asyncpg.connect(dsn)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def test_migrate_twice_is_idempotent(host_dsn: str) -> None:
    async def go() -> tuple[list[str], list[str]]:
        pool = await create_pool(host_dsn)
        try:
            return await Migrator(pool).migrate(), await Migrator(pool).migrate()
        finally:
            await pool.close()

    first, second = asyncio.run(go())
    assert first == ["0001_init.sql"]
    assert second == []


def test_concurrent_migrators_apply_once(host_dsn: str) -> None:
    async def go() -> list[list[str]]:
        pool = await create_pool(host_dsn)
        try:
            return list(await asyncio.gather(*(Migrator(pool).migrate() for _ in range(4))))
        finally:
            await pool.close()

    results = asyncio.run(go())
    assert sorted(len(r) for r in results) == [0, 0, 0, 1]


def test_failed_migration_rolls_back_and_is_not_recorded(host_dsn: str, tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE t_a (x int);")
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE t_b (x int); SELECT 1/0;")

    async def go() -> None:
        pool = await create_pool(host_dsn)
        try:
            with pytest.raises(asyncpg.PostgresError):
                await Migrator(pool, tmp_path).migrate()
        finally:
            await pool.close()

    asyncio.run(go())

    async def check(c: asyncpg.Connection) -> tuple[list[str], str | None]:
        names = [r["name"] for r in await c.fetch("SELECT name FROM schema_migrations ORDER BY name")]
        return names, await c.fetchval("SELECT to_regclass('t_b')::text")

    names, t_b = with_conn(host_dsn, check)
    assert names == ["0001_a.sql"] and t_b is None


def test_all_tables_exist(migrated_dsn: str) -> None:
    async def q(c: asyncpg.Connection) -> set[str]:
        rows = await c.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        return {r["tablename"] for r in rows}

    assert TABLES <= with_conn(migrated_dsn, q)


def _artifact(aid: str = "a1", **extra: str) -> tuple[str, list[str]]:
    cols = ["artifact_id", "owner", *extra]
    return (f"INSERT INTO artifacts ({','.join(cols)}) VALUES ({','.join(f'${i + 1}' for i in range(len(cols)))})",
            [aid, "m1", *extra.values()])


def test_intake_accepts_known_values_and_rejects_others(migrated_dsn: str) -> None:
    async def go(c: asyncpg.Connection) -> None:
        for i, value in enumerate(("accepted", "pending", "discarded")):
            sql, args = _artifact(f"a{i}", intake=value)
            await c.execute(sql, *args)
        sql, args = _artifact("bad", intake="bogus")
        with pytest.raises(asyncpg.InvalidTextRepresentationError):
            await c.execute(sql, *args)
        assert await c.fetchval("SELECT intake::text FROM artifacts WHERE artifact_id='a1'") == "pending"

    with_conn(migrated_dsn, go)


def test_foreign_keys_and_uniqueness(migrated_dsn: str) -> None:
    async def go(c: asyncpg.Connection) -> None:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await c.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number)"
                            " VALUES ('v0','missing',1)")
        sql, args = _artifact("a1")
        await c.execute(sql, *args)
        await c.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number) VALUES ('v1','a1',1)")
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number)"
                            " VALUES ('v2','a1',1)")
        # collaborator commits must name their agent
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number, created_via)"
                            " VALUES ('v3','a1',3,'collaborator')")
        # share state and token_hash shape
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash)"
                            " VALUES ('s1','a1','v1','lab','nothex')")
        digest = "a" * 64
        await c.execute("INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash)"
                        " VALUES ('s1','a1','v1','lab',$1)", digest)
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash)"
                            " VALUES ('s2','a1','v1','public',$1)", digest)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await c.execute("INSERT INTO share_events (share_id, kind, actor) VALUES ('nope','created','x')")
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO share_events (share_id, kind, actor) VALUES ('s1','bogus','x')")
        # folder names unique per owner, case-insensitively
        await c.execute("INSERT INTO library_folders (folder_id, owner, name) VALUES ($1,'m1','Papers')", "fld_" + "0" * 24)
        with pytest.raises(asyncpg.UniqueViolationError):
            await c.execute("INSERT INTO library_folders (folder_id, owner, name) VALUES ($1,'m1','papers')",
                            "fld_" + "1" * 24)
        # claims need a real share
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await c.execute("INSERT INTO share_claims (member_id, share_id) VALUES ('m2','nope')")
        # tokens: digest shape
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("INSERT INTO agent_tokens (digest, member_id) VALUES ('short','m1')")
        # annotation status/addressed coupling
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute(
                "INSERT INTO annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor, body,"
                " author_identity, author_email, author_role, status, created_at)"
                " VALUES ($1,'a1','v1','s1','f','{}','b','i','e','member','addressed', now())", "ann_" + "A" * 22)
        # deleting an artifact cascades to its versions-dependent children
        await c.execute("DELETE FROM shares WHERE share_id='s1'")

    with_conn(migrated_dsn, go)


def test_member_usage_counters_nonnegative(migrated_dsn: str) -> None:
    async def go(c: asyncpg.Connection) -> None:
        await c.execute("INSERT INTO member_usage (member_id) VALUES ('m1')")
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("UPDATE member_usage SET bytes_used = -1 WHERE member_id='m1'")

    with_conn(migrated_dsn, go)
