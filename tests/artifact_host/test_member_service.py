"""MemberService and LibraryStore against real Postgres: actor rules and the atomic pending cap."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from herdr_artifact_host.blobs import BlobStore
from herdr_artifact_host.library import (
    PENDING_CAP,
    LibraryStore,
    PendingCapExceeded,
    Viewer,
)
from herdr_artifact_host.member_api import (
    Actor,
    ActorKind,
    MemberError,
    MemberLimits,
    MemberService,
)


def seed_share(dsn: str, owner: str) -> tuple[str, str]:
    async def go() -> tuple[str, str]:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            aid, vid, sid = (
                "art_" + uuid.uuid4().hex[:20],
                "ver_" + uuid.uuid4().hex[:20],
                "shr_" + uuid.uuid4().hex[:20],
            )
            await pool.execute(
                "INSERT INTO artifacts (artifact_id, owner) VALUES ($1,$2)", aid, owner
            )
            await pool.execute(
                "INSERT INTO artifact_versions (version_id, artifact_id, version_number, output_manifest) "
                "VALUES ($1,$2,1,$3::jsonb)",
                vid,
                aid,
                json.dumps([]),
            )
            await pool.execute(
                "INSERT INTO shares (share_id, artifact_id, version_id, audience, state, created_by) "
                "VALUES ($1,$2,$3,'public','pending',$4)",
                sid,
                aid,
                vid,
                owner,
            )
            return aid, sid
        finally:
            await pool.close()

    return asyncio.run(go())


@pytest.mark.parametrize("kind", [ActorKind.AGENT, ActorKind.TOKEN, ActorKind.OPERATOR])
def test_only_the_signed_in_user_can_approve(
    migrated_dsn: str, tmp_path: Path, kind: ActorKind
) -> None:
    aid, sid = seed_share(migrated_dsn, "alice")

    async def go() -> None:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=1, max_size=3)
        try:
            svc = MemberService(
                pool,
                BlobStore(tmp_path / "b"),
                LibraryStore(pool),
                MemberLimits(),
                "https://share.example",
            )
            with pytest.raises(MemberError) as err:
                await svc.share_approve(Actor(kind, "alice"), aid, sid, True)
            assert err.value.status == 403
            assert (
                await pool.fetchval("SELECT state FROM shares WHERE share_id=$1", sid) == "pending"
            )
            ok = await svc.share_approve(Actor(ActorKind.USER, "alice"), aid, sid, True)
            assert ok["state"] == "live"
        finally:
            await pool.close()

    asyncio.run(go())


def test_pending_cap_is_atomic_under_concurrency(migrated_dsn: str) -> None:
    async def go() -> tuple[int, int, int]:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=4, max_size=16)
        try:
            lib = LibraryStore(pool)
            viewer = Viewer(member_id="racer")

            async def one(i: int) -> bool:
                try:
                    await lib.insert_artifact(
                        viewer,
                        artifact_id=f"p{i}",
                        display_name="p",
                        filename="a.html",
                        intake="pending",
                    )
                    return True
                except PendingCapExceeded:
                    return False

            results = await asyncio.gather(*(one(i) for i in range(60)))
            held = await pool.fetchval(
                "SELECT count(*) FROM artifacts WHERE owner='racer' AND intake='pending'"
            )
            return sum(results), int(held), len(results)
        finally:
            await pool.close()

    won, held, total = asyncio.run(go())
    assert won == held == PENDING_CAP and total == 60


def test_accepted_inserts_are_not_capped_and_roll_back_with_the_caller(migrated_dsn: str) -> None:
    async def go() -> int:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=1, max_size=4)
        try:
            lib = LibraryStore(pool)
            viewer = Viewer(member_id="solo")
            for i in range(PENDING_CAP + 3):
                await lib.insert_artifact(
                    viewer, artifact_id=f"a{i}", display_name="a", filename="a.html"
                )
            with pytest.raises(RuntimeError):
                async with pool.acquire() as conn, conn.transaction():
                    await lib.insert_artifact(
                        viewer, artifact_id="gone", display_name="a", filename="a.html", conn=conn
                    )
                    raise RuntimeError("fail after insert")
            return int(await pool.fetchval("SELECT count(*) FROM artifacts WHERE owner='solo'"))
        finally:
            await pool.close()

    assert asyncio.run(go()) == PENDING_CAP + 3


def test_read_all_viewer_cannot_insert(migrated_dsn: str) -> None:
    from herdr_artifact_host.library import OPERATOR_READ_ALL, LibraryError

    async def go() -> None:
        pool = await asyncpg.create_pool(migrated_dsn, min_size=1, max_size=2)
        try:
            with pytest.raises(LibraryError):
                await LibraryStore(pool).insert_artifact(
                    OPERATOR_READ_ALL, artifact_id="x", display_name="x", filename="x"
                )
        finally:
            await pool.close()

    asyncio.run(go())


def test_blob_store_tmp_dir(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "b")
    assert store.tmp_dir.is_dir() and store.put(io.BytesIO(b"x"))
