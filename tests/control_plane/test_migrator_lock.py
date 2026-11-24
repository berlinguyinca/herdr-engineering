"""Migrator against a real Postgres (PA_PG_DSN): one migrator at a time, and a migration never hangs behind a lock."""
import asyncio
import os
import uuid

import asyncpg
import pytest

from herdr_engineering.control_plane.db import MIGRATION_LOCK_KEY, Migrator

pytestmark = pytest.mark.integration


@pytest.fixture
def pg():
    dsn = os.environ.get("PA_PG_DSN")
    if not dsn:
        pytest.skip("PA_PG_DSN not set")
    return dsn


async def _connect(dsn, schema):
    return await asyncpg.connect(dsn, server_settings={"search_path": schema})


@pytest.fixture
def schema(pg):
    name = f"migrator_lock_{uuid.uuid4().hex[:10]}"

    async def make():
        admin = await asyncpg.connect(pg)
        await admin.execute(f'CREATE SCHEMA "{name}"')
        await admin.close()
    asyncio.run(make())
    yield name

    async def drop():
        admin = await asyncpg.connect(pg)
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()
    asyncio.run(drop())


def test_two_starters_never_migrate_at_once(pg, schema, tmp_path):
    (tmp_path / "0001_slow.sql").write_text("CREATE TABLE once_only(id int); SELECT pg_sleep(1.5);")

    async def go():
        a, b = await _connect(pg, schema), await _connect(pg, schema)
        try:
            return await asyncio.gather(Migrator(a).apply(str(tmp_path)), Migrator(b).apply(str(tmp_path)))
        finally:
            await a.close()
            await b.close()
    results = asyncio.run(go())
    assert sorted(results) == [[], ["0001_slow.sql"]]        # exactly one applied it; the other found it done


def test_a_migration_behind_a_long_transaction_fails_fast_and_cleans_up(pg, schema, tmp_path):
    (tmp_path / "0001_seed.sql").write_text("CREATE TABLE held(id int);")

    async def go():
        holder, mig = await _connect(pg, schema), await _connect(pg, schema)
        assert await Migrator(mig).apply(str(tmp_path)) == ["0001_seed.sql"]
        (tmp_path / "0002_alter.sql").write_text("ALTER TABLE held ADD COLUMN extra int;")
        tx = holder.transaction()
        await tx.start()
        await holder.execute("LOCK TABLE held IN ACCESS EXCLUSIVE MODE")
        try:
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await asyncio.wait_for(Migrator(mig, lock_timeout_ms=300).apply(str(tmp_path)), timeout=10)
        finally:
            await tx.rollback()
        # the connection is usable, the session lock was released and lock_timeout no longer applies to the app
        assert await mig.fetchval("SELECT pg_try_advisory_lock($1)", MIGRATION_LOCK_KEY) is True
        await mig.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        assert await mig.fetchval("SHOW lock_timeout") == "0"
        # the failed migration was not recorded, so the next start retries it
        assert await Migrator(mig).applied() == ["0001_seed.sql"]
        assert await Migrator(mig).apply(str(tmp_path)) == ["0002_alter.sql"]
        await holder.close()
        await mig.close()
    asyncio.run(go())


def test_waiting_for_another_migrators_lock_times_out_instead_of_hanging(pg, schema, tmp_path):
    (tmp_path / "0001_x.sql").write_text("SELECT 1;")

    async def go():
        owner, waiter = await _connect(pg, schema), await _connect(pg, schema)
        await owner.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        try:
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await asyncio.wait_for(Migrator(waiter, lock_timeout_ms=300).apply(str(tmp_path)), timeout=10)
        finally:
            await owner.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        assert await Migrator(waiter).apply(str(tmp_path)) == ["0001_x.sql"]    # free again: applies normally
        await owner.close()
        await waiter.close()
    asyncio.run(go())
