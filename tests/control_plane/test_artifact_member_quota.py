"""Member usage accounting and optional limits against real PostgreSQL rows (no fakes)."""
import uuid

import pytest

from herdr_engineering.control_plane.artifact_member_quota import (
    QuotaExceeded,
    check_new_artifact,
    check_publish,
    settle,
)
from herdr_engineering.control_plane.artifact_members import MemberLimits, MemberStore
from herdr_engineering.control_plane.artifacts import ArtifactError
from tests.control_plane.test_artifact_members import migrated  # noqa: F401  (fixture)
from tests.control_plane.test_share_store import run, share_postgres  # noqa: F401  (fixture)

MiB, TiB = 1 << 20, 1 << 40


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
        await conn.execute("UPDATE artifacts SET lifecycle='purged' WHERE artifact_id="
                           "(SELECT artifact_id FROM artifacts WHERE created_by=$1 LIMIT 1)", ALICE)
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
        await make_artifact(conn, ALICE, blobs=[("1" * 64, 10), ("2" * 64, 20)])  # all versions' blobs, one row per digest
        shared = ("3" * 64, 5)
        await make_artifact(conn, ALICE, blobs=[shared])
        await make_artifact(conn, ALICE, blobs=[shared])  # dedup is within an artifact only: counts again
        await make_artifact(conn, ALICE, lifecycle="retracted", blobs=[("4" * 64, 100)])
        await make_artifact(conn, ALICE, lifecycle="purged", blobs=[("5" * 64, 1000)])
        await make_artifact(conn, "member:bob", blobs=[("6" * 64, 7)])
        assert await settle(conn, ALICE) == {"bytes_used": 140, "bytes_retracted": 100}
        await conn.execute("UPDATE artifacts SET lifecycle='purged' WHERE lifecycle='retracted'")
        assert await settle(conn, ALICE) == {"bytes_used": 40, "bytes_retracted": 0}
        assert await settle(conn, fresh()) == {"bytes_used": 0, "bytes_retracted": 0}
    run(migrated, body)


def test_dedup_within_artifact_via_manifest_versions(migrated):  # noqa: F811
    """Two versions of one artifact reusing a digest share one blob row, so the digest counts once."""
    async def body(_s, conn):
        ALICE = fresh()
        aid = await make_artifact(conn, ALICE, blobs=[("7" * 64, 50)])
        for n in (1, 2):
            await conn.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number, state,"
                               " output_manifest) VALUES ($1,$2,$3,'committed',$4::jsonb)",
                               f"v{n}_{aid}", aid, n, '[{"blob_sha256": "%s", "size_bytes": 50}]' % ("7" * 64))
        assert (await settle(conn, ALICE))["bytes_used"] == 50
    run(migrated, body)


def test_check_publish_takes_the_usage_row_lock(migrated):  # noqa: F811
    async def body(_s, conn):
        ALICE = fresh()
        import asyncpg
        other = await asyncpg.connect(migrated)
        try:
            await set_used(conn, ALICE, 0)
            async with conn.transaction():
                await check_publish(conn, ALICE, MemberLimits(max_member_bytes=10), new_bytes=1, output_manifest=[])
                with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                    await other.fetchrow("SELECT 1 FROM artifact_member_usage WHERE member_principal=$1"
                                         " FOR UPDATE NOWAIT", ALICE)
        finally:
            await other.close()
    run(migrated, body)
