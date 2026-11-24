"""Migration 0051: library folders, items, share claims and the pending-intake column, on real PostgreSQL."""
import asyncio
import uuid

import asyncpg
import pytest

from tests.control_plane.test_share_store import MIGRATIONS, run, share_postgres  # noqa: F401  (fixture)


@pytest.fixture
def migrated(share_postgres):  # noqa: F811
    async def go():
        conn = await asyncpg.connect(share_postgres)
        try:
            await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ('pre_existing','o')"
                               " ON CONFLICT DO NOTHING")  # a row from before the column existed
            for _ in range(2):  # applying twice is a no-op
                await conn.execute((MIGRATIONS / "0048_member_artifacts.sql").read_text())
                await conn.execute((MIGRATIONS / "0051_artifact_library.sql").read_text())
        finally:
            await conn.close()
    asyncio.run(go())
    return share_postgres


def fid():
    return "fld_" + uuid.uuid4().hex[:24]


def test_tables_exist_after_all_migrations(migrated):
    async def body(_store, conn):
        for table in ("artifact_library_folders", "artifact_library_items", "artifact_share_claims"):
            assert await conn.fetchval("SELECT to_regclass($1)", table) is not None
        col = await conn.fetchrow("SELECT column_default, is_nullable FROM information_schema.columns"
                                  " WHERE table_name='artifacts' AND column_name='intake'")
        assert col["is_nullable"] == "NO" and "accepted" in col["column_default"]
    run(migrated, body)


def test_source_check_rejects_unknown_value(migrated):
    async def body(_store, conn):
        f = fid()
        await conn.execute("INSERT INTO artifact_library_folders (folder_id,owner,name) VALUES ($1,'o','src')", f)
        for ok in ("manual", "suggested", "auto"):
            await conn.execute("INSERT INTO artifact_library_items (folder_id,artifact_id,source) VALUES ($1,$2,$3)",
                               f, "a_" + ok, ok)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO artifact_library_items (folder_id,artifact_id,source) VALUES ($1,'x','bogus')", f)
    run(migrated, body)


def test_deleting_folder_cascades_items_not_artifacts(migrated):
    async def body(_store, conn):
        aid = "artifact_" + uuid.uuid4().hex
        await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,'o')", aid)
        f = fid()
        await conn.execute("INSERT INTO artifact_library_folders (folder_id,owner,name) VALUES ($1,'o','casc')", f)
        await conn.execute("INSERT INTO artifact_library_items (folder_id,artifact_id,source) VALUES ($1,$2,'manual')",
                           f, aid)
        await conn.execute("DELETE FROM artifact_library_folders WHERE folder_id=$1", f)
        assert await conn.fetchval("SELECT count(*) FROM artifact_library_items WHERE folder_id=$1", f) == 0
        assert await conn.fetchval("SELECT count(*) FROM artifacts WHERE artifact_id=$1", aid) == 1
    run(migrated, body)


def test_intake_defaults_to_accepted_for_existing_rows(migrated):
    async def body(_store, conn):
        assert await conn.fetchval("SELECT intake FROM artifacts WHERE artifact_id='pre_existing'") == "accepted"
        aid = "artifact_" + uuid.uuid4().hex
        await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,'o')", aid)
        assert await conn.fetchval("SELECT intake FROM artifacts WHERE artifact_id=$1", aid) == "accepted"
        await conn.execute("UPDATE artifacts SET intake='pending' WHERE artifact_id=$1", aid)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE artifacts SET intake='bogus' WHERE artifact_id=$1", aid)
    run(migrated, body)


def test_folder_name_unique_per_owner_case_insensitive(migrated):
    async def body(_store, conn):
        await conn.execute("INSERT INTO artifact_library_folders (folder_id,owner,name) VALUES ($1,'u1','Papers')", fid())
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("INSERT INTO artifact_library_folders (folder_id,owner,name) VALUES ($1,'u1','PAPERS')", fid())
        await conn.execute("INSERT INTO artifact_library_folders (folder_id,owner,name) VALUES ($1,'u2','Papers')", fid())
    run(migrated, body)


def test_share_events_accept_claimed_kind(migrated):
    async def body(_store, conn):
        aid, vid = "artifact_" + uuid.uuid4().hex, "version_" + uuid.uuid4().hex
        await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,'o')", aid)
        await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state)"
                           " VALUES ($1,$2,1,'ready')", vid, aid)
        sid = "share_" + uuid.uuid4().hex
        await conn.execute("INSERT INTO artifact_shares (share_id,artifact_id,version_id,audience,created_by)"
                           " VALUES ($1,$2,$3,'lab','o')", sid, aid, vid)
        for kind in ("claimed", "created", "opened"):
            await conn.execute("INSERT INTO artifact_share_events (share_id,kind,actor) VALUES ($1,$2,'a')", sid, kind)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO artifact_share_events (share_id,kind,actor) VALUES ($1,'bogus','a')", sid)
    run(migrated, body)
