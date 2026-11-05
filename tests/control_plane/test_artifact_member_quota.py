"""Member usage accounting and optional limits against real PostgreSQL rows (no fakes)."""
import asyncio
import json
import uuid

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_member_quota import (
    QuotaExceeded,
    check_new_artifact,
    check_publish,
    settle,
)
from herdr_engineering.control_plane.artifact_members import MemberLimits, MemberStore
from herdr_engineering.control_plane.artifacts import ArtifactError
from tests.control_plane.test_artifact_members import migrated as _migrated  # noqa: F401  (fixture base)
from tests.control_plane.test_share_store import MIGRATIONS, run, share_postgres  # noqa: F401  (fixture)

MiB, TiB = 1 << 20, 1 << 40


@pytest.fixture
def migrated(_migrated):  # noqa: F811
    """0048 plus 0031 (purged_at column and the lifecycle CHECK), as production has them."""
    async def go():
        conn = await asyncpg.connect(_migrated)
        try:
            await conn.execute((MIGRATIONS / "0031_artifact_registry_extensions.sql").read_text())
        finally:
            await conn.close()
    asyncio.run(go())
    return _migrated


def fresh():
    return "member:" + uuid.uuid4().hex


async def make_artifact(conn, principal, *, lifecycle="active", blobs=()):
    """A real artifact row with one scoped ``artifact_blobs`` row per ``(sha, size)``."""
    aid = "artifact_" + uuid.uuid4().hex
    await conn.execute("INSERT INTO artifacts (artifact_id, created_by, lifecycle) VALUES ($1,$2,$3)",
                       aid, principal, lifecycle)
    for sha, size in blobs:
        await conn.execute(
            "INSERT INTO artifact_blobs (scope, blob_sha256, storage_key, size_bytes) VALUES ($1,$2,$3,$4)",
            aid, sha, f"{aid}/{sha}", size)
    return aid


async def add_version(conn, aid, n, manifest):
    await conn.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest)"
                       " VALUES ($1,$2,$3,'committed',$4::jsonb)", f"v{n}_{aid}", aid, n, json.dumps(manifest))


async def purge_like_purge_due(conn, aid):
    """The exact end state ``ArtifactLifecycleMixin.purge_due`` leaves (needs no object store): tombstoned, every
    version ``state='purged'`` with ``purged_at`` set, ``artifact_blobs`` rows deleted."""
    await conn.execute("UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now() WHERE artifact_id=$1", aid)
    await conn.execute("UPDATE artifact_versions SET state='purged', purged_at=now() WHERE artifact_id=$1", aid)
    await conn.execute("DELETE FROM artifact_blobs WHERE scope=$1", aid)


async def set_used(conn, principal, used):
    await MemberStore(conn).usage(principal)
    await conn.execute("UPDATE artifact_member_usage SET bytes_used=$2 WHERE member_principal=$1", principal, used)


def test_unlimited_by_default_and_settle_reports(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        none = MemberLimits()
        await check_new_artifact(conn, ALICE, none)
        await check_publish(conn, ALICE, none, new_bytes=TiB, output_manifest=[{"size_bytes": TiB}])
        await make_artifact(conn, ALICE, blobs=[("a" * 64, TiB)])
        assert await settle(conn, ALICE) == {"bytes_used": TiB, "bytes_retracted": 0}
        row = await conn.fetchrow("SELECT bytes_used FROM artifact_member_usage WHERE member_principal=$1", ALICE)
        assert row["bytes_used"] == TiB
    run(migrated, body)


def test_member_bytes_limit(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        lim = MemberLimits(max_member_bytes=200 * MiB)
        await set_used(conn, ALICE, 100 * MiB)
        with pytest.raises(QuotaExceeded) as exc:
            await check_publish(conn, ALICE, lim, new_bytes=120 * MiB, output_manifest=[])
        assert exc.value.code == "member_bytes" and isinstance(exc.value, ArtifactError)
        await check_publish(conn, ALICE, lim, new_bytes=100 * MiB, output_manifest=[])  # exactly at the limit
    run(migrated, body)


def test_member_artifacts_limit(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        lim = MemberLimits(max_artifacts=20)
        for _ in range(19):
            await make_artifact(conn, ALICE)
        await check_new_artifact(conn, ALICE, lim)  # the 20th
        await make_artifact(conn, ALICE)
        await make_artifact(conn, "member:bob")  # someone else's does not count
        with pytest.raises(QuotaExceeded) as exc:
            await check_new_artifact(conn, ALICE, lim)  # the 21st
        assert exc.value.code == "member_artifacts"
        first = await conn.fetchval("SELECT artifact_id FROM artifacts WHERE created_by=$1 LIMIT 1", ALICE)
        await add_version(conn, first, 1, [])
        await conn.execute("UPDATE artifacts SET lifecycle='tombstoned' WHERE artifact_id=$1", first)
        with pytest.raises(QuotaExceeded):  # tombstoned but not yet purged still counts
            await check_new_artifact(conn, ALICE, lim)
        await purge_like_purge_due(conn, first)
        await check_new_artifact(conn, ALICE, lim)  # a purged artifact no longer counts
    run(migrated, body)


def test_artifact_bytes_limit(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        lim = MemberLimits(max_artifact_bytes=25 * MiB)
        ok = [{"size_bytes": 20 * MiB}, {"size_bytes": 5 * MiB}]
        await check_publish(conn, ALICE, lim, new_bytes=0, output_manifest=ok)
        with pytest.raises(QuotaExceeded) as exc:
            await check_publish(conn, ALICE, lim, new_bytes=0, output_manifest=ok + [{"size_bytes": 1}])
        assert exc.value.code == "artifact_bytes"
        await check_publish(conn, ALICE, MemberLimits(), new_bytes=0, output_manifest=ok + [{"size_bytes": 1}])
    run(migrated, body)


def test_settle_counts_dedups_and_splits_retracted(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        await make_artifact(conn, ALICE, blobs=[("1" * 64, 10), ("2" * 64, 20)])
        shared = ("3" * 64, 5)
        await make_artifact(conn, ALICE, blobs=[shared])
        await make_artifact(conn, ALICE, blobs=[shared])  # dedup is within an artifact only: counts again
        retracted = await make_artifact(conn, ALICE, lifecycle="retracted", blobs=[("4" * 64, 100)])
        gone = await make_artifact(conn, ALICE, lifecycle="tombstoned", blobs=[("5" * 64, 1000)])
        await add_version(conn, gone, 1, [])
        await conn.execute("UPDATE artifact_versions SET state='purged', purged_at=now() WHERE artifact_id=$1", gone)
        await make_artifact(conn, "member:bob", blobs=[("6" * 64, 7)])
        # purged versions count nothing even if a blob row were still around
        assert await settle(conn, ALICE) == {"bytes_used": 140, "bytes_retracted": 100}
        await add_version(conn, retracted, 1, [])
        await purge_like_purge_due(conn, retracted)
        assert await settle(conn, ALICE) == {"bytes_used": 40, "bytes_retracted": 0}
        assert await settle(conn, fresh()) == {"bytes_used": 0, "bytes_retracted": 0}
    run(migrated, body)


def test_dedup_within_artifact_across_versions(migrated):  # noqa: F811
    """Two versions of one artifact reuse a digest; the scoped blob is stored once (as commit does), so it counts once."""
    async def body(_s, conn):
        ALICE = fresh()
        aid = await make_artifact(conn, ALICE)
        for n, files in ((1, [("a" * 64, 50), ("b" * 64, 7)]), (2, [("a" * 64, 50), ("c" * 64, 3)])):
            await add_version(conn, aid, n, [{"blob_sha256": sha, "size_bytes": size} for sha, size in files])
            for sha, size in files:
                await conn.execute(
                    "INSERT INTO artifact_blobs (scope, blob_sha256, storage_key, size_bytes) VALUES ($1,$2,$3,$4)"
                    " ON CONFLICT (scope, blob_sha256) DO NOTHING", aid, sha, f"{aid}/{sha}", size)
        assert await conn.fetchval("SELECT count(*) FROM artifact_blobs WHERE scope=$1", aid) == 3
        assert (await settle(conn, ALICE))["bytes_used"] == 60  # 50 + 7 + 3, the shared 50 once
    run(migrated, body)


def test_input_validation(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        lim = MemberLimits()
        for bad in (-1, 1.5, True, None, "7"):
            with pytest.raises(ValueError):
                await check_publish(conn, ALICE, lim, new_bytes=bad, output_manifest=[])
        for entry in ({}, {"size_bytes": None}, {"size_bytes": -1}, {"size_bytes": "5"}, {"size_bytes": 1.5},
                      {"size_bytes": True}):
            with pytest.raises(ValueError):
                await check_publish(conn, ALICE, lim, new_bytes=0, output_manifest=[{"size_bytes": 1}, entry])
        with pytest.raises(ValueError):
            await check_publish(conn, "collab:x", lim, new_bytes=0, output_manifest=[])
        await check_publish(conn, ALICE, lim, new_bytes=0, output_manifest=[{"size_bytes": 0}])
    run(migrated, body)


def test_usage_row_lock_holds_from_check_through_settle(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        other = await asyncpg.connect(migrated)
        try:
            await MemberStore(conn).usage(ALICE)  # committed row; a first-ever row is invisible to others until commit
            async with conn.transaction():
                await check_publish(conn, ALICE, MemberLimits(max_member_bytes=10), new_bytes=1, output_manifest=[])
                await make_artifact(conn, ALICE, blobs=[("9" * 64, 4)])  # the "publish"
                await settle(conn, ALICE)
                waiter = asyncio.ensure_future(other.fetchrow(
                    "SELECT bytes_used FROM artifact_member_usage WHERE member_principal=$1 FOR UPDATE", ALICE))
                await asyncio.sleep(0.5)
                assert not waiter.done(), "second connection must block until the first transaction commits"
            row = await asyncio.wait_for(waiter, 5)
            assert row["bytes_used"] == 4  # it sees the settled value, not a stale one
        finally:
            await other.close()
    run(migrated, body)


def test_settle_alone_locks_the_usage_row(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        other = await asyncpg.connect(migrated)
        try:
            await MemberStore(conn).usage(ALICE)
            async with conn.transaction():
                await settle(conn, ALICE)  # e.g. after a delete or purge
                with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                    await other.fetchrow("SELECT 1 FROM artifact_member_usage WHERE member_principal=$1"
                                         " FOR UPDATE NOWAIT", ALICE)
        finally:
            await other.close()
    run(migrated, body)


@pytest.mark.parametrize("which", ["publish", "new_artifact"])
def test_checks_alone_lock_the_usage_row(migrated, which):  # noqa: F811
    """Before settle runs, the check itself must already hold the row (two racing publishes cannot both pass)."""
    async def body(_s, conn):
        ALICE = fresh()
        other = await asyncpg.connect(migrated)
        try:
            await MemberStore(conn).usage(ALICE)
            async with conn.transaction():
                if which == "publish":
                    await check_publish(conn, ALICE, MemberLimits(), new_bytes=1, output_manifest=[])
                else:
                    await check_new_artifact(conn, ALICE, MemberLimits())
                with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                    await other.fetchrow("SELECT 1 FROM artifact_member_usage WHERE member_principal=$1"
                                         " FOR UPDATE NOWAIT", ALICE)
        finally:
            await other.close()
    run(migrated, body)
