"""LibraryStore folders and filing, on real PostgreSQL."""
import asyncio
import uuid

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_library import Folder, LibraryError, LibraryStore
from tests.control_plane.test_share_store import MIGRATIONS, share_postgres  # noqa: F401  (fixture)


@pytest.fixture
def dsn(share_postgres):  # noqa: F811
    async def go():
        conn = await asyncpg.connect(share_postgres)
        try:
            for name in ("0048_member_artifacts.sql", "0051_artifact_library.sql"):
                await conn.execute((MIGRATIONS / name).read_text())
        finally:
            await conn.close()
    asyncio.run(go())
    return share_postgres


@pytest.fixture(autouse=True)
def members():
    """Fresh principals per test: the database is shared across the module."""
    global ME, YOU
    ME, YOU = "member:a" + uuid.uuid4().hex[:8], "member:b" + uuid.uuid4().hex[:8]


def run(dsn, body):
    async def go():
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            return await body(LibraryStore(pool), pool)
        finally:
            await pool.close()
    return asyncio.run(go())


async def artifact(pool, owner=None, lifecycle="active"):
    owner = owner or ME
    aid = "artifact_" + uuid.uuid4().hex
    await pool.execute("INSERT INTO artifacts (artifact_id,created_by,lifecycle) VALUES ($1,$2,$3)", aid, owner, lifecycle)
    return aid


async def count_items(pool, folder_id):
    return await pool.fetchval("SELECT count(*) FROM artifact_library_items WHERE folder_id=$1", folder_id)


def test_create_rename_delete_folder(dsn):
    async def body(store, _pool):
        f = await store.create_folder(ME, "  Plans  ")
        assert isinstance(f, Folder) and f.name == "Plans" and f.owner == ME and f.item_count == 0
        assert f.folder_id.startswith("fld_") and len(f.folder_id) == 28
        r = await store.rename_folder(ME, f.folder_id, "Roadmaps")
        assert r.name == "Roadmaps" and r.folder_id == f.folder_id
        assert [x.name for x in await store.list_folders(ME)] == ["Roadmaps"]
        await store.delete_folder(ME, f.folder_id)
        assert await store.list_folders(ME) == []
        with pytest.raises(LibraryError) as e:
            await store.delete_folder(ME, f.folder_id)
        assert (e.value.status, e.value.code) == (404, "not_found")
    run(dsn, body)


def test_duplicate_name_refused_409(dsn):
    async def body(store, _pool):
        await store.create_folder(ME, "Dup")
        with pytest.raises(LibraryError) as e:
            await store.create_folder(ME, "dup")
        assert (e.value.status, e.value.code) == (409, "duplicate_name")
        other = await store.create_folder(ME, "Other")
        with pytest.raises(LibraryError) as e2:
            await store.rename_folder(ME, other.folder_id, "DUP")
        assert e2.value.status == 409
        assert (await store.create_folder(YOU, "Dup")).owner == YOU  # per owner
    run(dsn, body)


def test_one_artifact_in_two_folders(dsn):
    async def body(store, pool):
        a = await artifact(pool)
        f1, f2 = await store.create_folder(ME, "One"), await store.create_folder(ME, "Two")
        await store.file_artifact(ME, f1.folder_id, a)
        await store.file_artifact(ME, f2.folder_id, a, source="suggested")
        await store.file_artifact(ME, f1.folder_id, a)  # idempotent
        counts = {f.name: f.item_count for f in await store.list_folders(ME)}
        assert counts == {"One": 1, "Two": 1}
        await store.unfile_artifact(ME, f1.folder_id, a)
        assert await count_items(pool, f1.folder_id) == 0 and await count_items(pool, f2.folder_id) == 1
        await store.unfile_artifact(ME, f1.folder_id, a)  # idempotent
    run(dsn, body)


def test_cannot_file_an_artifact_the_member_cannot_read(dsn):
    async def body(store, pool):
        f = await store.create_folder(ME, "Mine")
        theirs = await artifact(pool, YOU)
        gone = await artifact(pool, ME, "tombstoned")
        errs = []
        for aid in (theirs, gone, "artifact_missing"):
            with pytest.raises(LibraryError) as e:
                await store.file_artifact(ME, f.folder_id, aid)
            errs.append((e.value.status, e.value.code, str(e.value)))
        assert errs[0] == errs[1] == errs[2] and errs[0][:2] == (404, "not_found")
        # an ACL read grant makes it fileable; expired does not
        await pool.execute("INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by) "
                           "VALUES ($1,$2,'[\"read\"]','x')", theirs, ME)
        await store.file_artifact(ME, f.folder_id, theirs)
        expired = await artifact(pool, YOU)
        await pool.execute("INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by,expires_at) "
                           "VALUES ($1,$2,'[\"read\"]','x', now() - interval '1 hour')", expired, ME)
        with pytest.raises(LibraryError) as e:
            await store.file_artifact(ME, f.folder_id, expired)
        assert e.value.status == 404
    run(dsn, body)


def test_other_members_folder_id_is_404(dsn):
    async def body(store, pool):
        f = await store.create_folder(YOU, "Private")
        a = await artifact(pool)
        calls = [store.rename_folder(ME, f.folder_id, "x"), store.delete_folder(ME, f.folder_id),
                 store.file_artifact(ME, f.folder_id, a), store.unfile_artifact(ME, f.folder_id, a),
                 store.rename_folder(ME, "fld_" + "0" * 24, "x")]
        seen = []
        for c in calls:
            with pytest.raises(LibraryError) as e:
                await c
            seen.append((e.value.status, e.value.code, str(e.value)))
        assert len(set(seen)) == 1 and seen[0][:2] == (404, "not_found")
        assert [x.name for x in await store.list_folders(YOU)] == ["Private"]
    run(dsn, body)


def test_delete_folder_keeps_artifacts(dsn):
    async def body(store, pool):
        a = await artifact(pool)
        f = await store.create_folder(ME, "Temp")
        await store.file_artifact(ME, f.folder_id, a)
        await store.delete_folder(ME, f.folder_id)
        assert await count_items(pool, f.folder_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM artifacts WHERE artifact_id=$1", a) == 1
    run(dsn, body)


def test_name_length_and_blank_refused(dsn):
    async def body(store, _pool):
        for bad in ("", "   ", "x" * 81):
            with pytest.raises(LibraryError) as e:
                await store.create_folder(ME, bad)
            assert (e.value.status, e.value.code) == (400, "invalid_name")
        ok = await store.create_folder(ME, "y" * 80)
        with pytest.raises(LibraryError) as e:
            await store.rename_folder(ME, ok.folder_id, " ")
        assert e.value.status == 400
    run(dsn, body)


def test_folders_per_member_and_items_per_folder_are_capped(dsn):
    from herdr_engineering.control_plane.artifact_library import MAX_FOLDERS, MAX_FOLDER_ITEMS

    async def body(store, pool):
        await pool.execute(
            "INSERT INTO artifact_library_folders (folder_id, owner, name) "
            "SELECT 'fld_' || substr(md5($1 || g::text), 1, 24), $1, 'f' || g FROM generate_series(1, $2) g", ME, MAX_FOLDERS)
        with pytest.raises(LibraryError) as e:
            await store.create_folder(ME, "one too many")
        assert (e.value.status, e.value.code) == (429, "folder_cap") and "200" in e.value.message
        await store.create_folder(YOU, "someone else is unaffected")
        await store.delete_folder(ME, "fld_" + __import__("hashlib").md5((ME + "1").encode()).hexdigest()[:24])
        folder = await store.create_folder(ME, "fits again")
        mine = await artifact(pool)
        await pool.execute(
            "INSERT INTO artifacts (artifact_id, created_by) SELECT 'artifact_cap' || md5($1 || g::text), $1 "
            "FROM generate_series(1, $2) g", ME, MAX_FOLDER_ITEMS)
        await pool.execute(
            "INSERT INTO artifact_library_items (folder_id, artifact_id, source) SELECT $1, artifact_id, 'manual' FROM artifacts "
            "WHERE artifact_id LIKE 'artifact_cap%' AND created_by=$2", folder.folder_id, ME)
        with pytest.raises(LibraryError) as e:
            await store.file_artifact(ME, folder.folder_id, mine)
        assert (e.value.status, e.value.code) == (429, "folder_items_cap")
        again = (await pool.fetchval("SELECT artifact_id FROM artifact_library_items WHERE folder_id=$1 LIMIT 1",
                                     folder.folder_id))
        await store.file_artifact(ME, folder.folder_id, again)       # already filed: idempotent, not a new item
    run(dsn, body)
