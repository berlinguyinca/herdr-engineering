# ruff: noqa: F811  (pytest fixture imported by name)
"""LibraryStore folders, filing, claims, pending intake and viewer isolation, on real PostgreSQL."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import asyncpg
import pytest

from herdr_artifact_host.library import (
    MAX_FOLDER_ITEMS,
    MAX_FOLDERS,
    OPERATOR_READ_ALL,
    PENDING_CAP,
    Folder,
    FolderSource,
    ItemRef,
    ItemStatus,
    LibraryError,
    LibraryStore,
    PendingCapExceeded,
    Source,
    Viewer,
)
from tests.artifact_host.library_support import NOW, Who, artifact, feed, grant, run, share, who  # noqa: F401  (fixture)


async def count_items(pool: asyncpg.Pool, folder_id: str) -> int:
    return int(
        await pool.fetchval("SELECT count(*) FROM library_items WHERE folder_id=$1", folder_id)
    )


def test_create_rename_delete_folder(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, _pool: asyncpg.Pool) -> None:
        f = await store.create_folder(who.me, "  Plans  ")
        assert (
            isinstance(f, Folder) and f.name == "Plans" and f.owner == who.me and f.item_count == 0
        )
        assert f.folder_id.startswith("fld_") and len(f.folder_id) == 28
        r = await store.rename_folder(who.me, f.folder_id, "Roadmaps")
        assert r.name == "Roadmaps" and r.folder_id == f.folder_id
        assert [x.name for x in await store.list_folders(who.me)] == ["Roadmaps"]
        await store.delete_folder(who.me, f.folder_id)
        assert await store.list_folders(who.me) == []
        with pytest.raises(LibraryError) as e:
            await store.delete_folder(who.me, f.folder_id)
        assert (e.value.status, e.value.code) == (404, "not_found")

    run(migrated_dsn, body)


def test_duplicate_name_refused_409(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, _pool: asyncpg.Pool) -> None:
        await store.create_folder(who.me, "Dup")
        with pytest.raises(LibraryError) as e:
            await store.create_folder(who.me, "dup")
        assert (e.value.status, e.value.code) == (409, "duplicate_name")
        other = await store.create_folder(who.me, "Other")
        with pytest.raises(LibraryError) as e2:
            await store.rename_folder(who.me, other.folder_id, "DUP")
        assert e2.value.status == 409
        assert (await store.create_folder(who.you, "Dup")).owner == who.you  # per owner

    run(migrated_dsn, body)


def test_one_artifact_in_two_folders(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.me)
        f1, f2 = await store.create_folder(who.me, "One"), await store.create_folder(who.me, "Two")
        await store.file_artifact(who.viewer, f1.folder_id, a)
        await store.file_artifact(who.viewer, f2.folder_id, a, source=FolderSource.SUGGESTED)
        await store.file_artifact(who.viewer, f1.folder_id, a)  # idempotent
        counts = {f.name: f.item_count for f in await store.list_folders(who.me)}
        assert counts == {"One": 1, "Two": 1}
        await store.unfile_artifact(who.me, f1.folder_id, a)
        assert (
            await count_items(pool, f1.folder_id) == 0
            and await count_items(pool, f2.folder_id) == 1
        )
        await store.unfile_artifact(who.me, f1.folder_id, a)  # idempotent

    run(migrated_dsn, body)


def test_cannot_file_an_artifact_the_member_cannot_read(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        f = await store.create_folder(who.me, "Mine")
        theirs = await artifact(pool, who.you)
        gone = await artifact(pool, who.me, lifecycle="tombstoned")
        errs = []
        for aid in (theirs, gone, "artifact_missing"):
            with pytest.raises(LibraryError) as e:
                await store.file_artifact(who.viewer, f.folder_id, aid)
            errs.append((e.value.status, e.value.code, str(e.value)))
        assert errs[0] == errs[1] == errs[2] and errs[0][:2] == (404, "not_found")
        # an ACL read grant makes it fileable; expired does not
        await grant(pool, theirs, who.me, who.you)
        await store.file_artifact(who.viewer, f.folder_id, theirs)
        expired = await artifact(pool, who.you)
        await pool.execute(
            "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by,expires_at) "
            "VALUES ($1,$2,'{read}','x', now() - interval '1 hour')",
            expired,
            who.me,
        )
        with pytest.raises(LibraryError) as e:
            await store.file_artifact(who.viewer, f.folder_id, expired)
        assert e.value.status == 404

    run(migrated_dsn, body)


def test_other_members_folder_id_is_404(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        f = await store.create_folder(who.you, "Private")
        a = await artifact(pool, who.me)
        calls = [
            store.rename_folder(who.me, f.folder_id, "x"),
            store.delete_folder(who.me, f.folder_id),
            store.file_artifact(who.viewer, f.folder_id, a),
            store.unfile_artifact(who.me, f.folder_id, a),
            store.rename_folder(who.me, "fld_" + "0" * 24, "x"),
        ]
        seen = []
        for c in calls:
            with pytest.raises(LibraryError) as e:
                await c
            seen.append((e.value.status, e.value.code, str(e.value)))
        assert len(set(seen)) == 1 and seen[0][:2] == (404, "not_found")
        assert [x.name for x in await store.list_folders(who.you)] == ["Private"]

    run(migrated_dsn, body)


def test_delete_folder_keeps_artifacts(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.me)
        f = await store.create_folder(who.me, "Temp")
        await store.file_artifact(who.viewer, f.folder_id, a)
        await store.delete_folder(who.me, f.folder_id)
        assert await count_items(pool, f.folder_id) == 0
        assert await pool.fetchval("SELECT count(*) FROM artifacts WHERE artifact_id=$1", a) == 1

    run(migrated_dsn, body)


def test_name_length_and_blank_refused(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, _pool: asyncpg.Pool) -> None:
        for bad in ("", "   ", "x" * 81):
            with pytest.raises(LibraryError) as e:
                await store.create_folder(who.me, bad)
            assert (e.value.status, e.value.code) == (400, "invalid_name")
        ok = await store.create_folder(who.me, "y" * 80)
        with pytest.raises(LibraryError) as e:
            await store.rename_folder(who.me, ok.folder_id, " ")
        assert e.value.status == 400

    run(migrated_dsn, body)


def test_folders_per_member_and_items_per_folder_are_capped(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        await pool.execute(
            "INSERT INTO library_folders (folder_id, owner, name) "
            "SELECT 'fld_' || substr(md5($1 || g::text), 1, 24), $1, 'f' || g FROM generate_series(1, $2) g",
            who.me,
            MAX_FOLDERS,
        )
        with pytest.raises(LibraryError) as e:
            await store.create_folder(who.me, "one too many")
        assert (e.value.status, e.value.code) == (429, "folder_cap") and "200" in e.value.message
        await store.create_folder(who.you, "someone else is unaffected")
        await store.delete_folder(
            who.me, "fld_" + hashlib.md5((who.me + "1").encode()).hexdigest()[:24]
        )  # noqa: S324
        folder = await store.create_folder(who.me, "fits again")
        mine = await artifact(pool, who.me)
        await pool.execute(
            "INSERT INTO artifacts (artifact_id, owner) SELECT 'artifact_cap' || md5($1 || g::text), $1 "
            "FROM generate_series(1, $2) g",
            who.me,
            MAX_FOLDER_ITEMS,
        )
        await pool.execute(
            "INSERT INTO library_items (folder_id, artifact_id, source) SELECT $1, artifact_id, 'manual' "
            "FROM artifacts WHERE artifact_id LIKE 'artifact_cap%' AND owner=$2",
            folder.folder_id,
            who.me,
        )
        with pytest.raises(LibraryError) as e:
            await store.file_artifact(who.viewer, folder.folder_id, mine)
        assert (e.value.status, e.value.code) == (429, "folder_items_cap")
        again = await pool.fetchval(
            "SELECT artifact_id FROM library_items WHERE folder_id=$1 LIMIT 1", folder.folder_id
        )
        await store.file_artifact(
            who.viewer, folder.folder_id, again
        )  # already filed: idempotent, not a new item

    run(migrated_dsn, body)


def test_filing_follows_the_feed_not_ownership_or_acl(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        folder = await store.create_folder(who.me, "Papers")
        viewer = who.viewer_in(who.g1)
        lab = await artifact(pool, who.you)
        await share(pool, lab, who.you, emails=(who.email,))
        grp = await artifact(pool, who.you)
        await share(pool, grp, who.you, groups=(who.g1,))
        opened = await artifact(pool, who.you)
        await store.record_claim(who.me, await share(pool, opened, who.you, audience="public"))
        mine_pending = await artifact(pool, who.me, intake="pending")
        for aid in (lab, grp, opened, mine_pending):
            await store.file_artifact(viewer, folder.folder_id, aid)
        page = await feed(store, viewer, sources=frozenset(Source), folder_id=folder.folder_id)
        assert {i.artifact_id for i in page.items} == {lab, grp, opened, mine_pending}
        await store.unfile_artifact(who.me, folder.folder_id, opened)

    run(migrated_dsn, body)


def test_filing_refuses_what_the_feed_hides_with_one_identical_404(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        folder = await store.create_folder(who.me, "Papers")
        viewer = who.viewer_in(who.g1)
        stranger = await artifact(pool, who.you)
        others_pending = await artifact(pool, who.you, intake="pending")
        await share(
            pool, others_pending, who.you, emails=(who.email,)
        )  # a lab share of a pending push is hidden
        wrong_group = await artifact(pool, who.you)
        await share(pool, wrong_group, who.you, groups=("not-mine",))
        revoked = await artifact(pool, who.you)
        sid = await share(pool, revoked, who.you, audience="public")
        await store.record_claim(who.me, sid)
        await pool.execute("UPDATE shares SET state='revoked' WHERE share_id=$1", sid)
        unclaimed = await artifact(pool, who.you)
        await share(
            pool, unclaimed, who.you, audience="public"
        )  # live public link never opened by me
        seen = []
        for aid in (stranger, others_pending, wrong_group, revoked, unclaimed, "artifact_missing"):
            with pytest.raises(LibraryError) as e:
                await store.file_artifact(viewer, folder.folder_id, aid)
            seen.append((e.value.status, e.value.code, str(e.value)))
        assert len(set(seen)) == 1 and seen[0][:2] == (404, "not_found"), seen

    run(migrated_dsn, body)


# --- claims -------------------------------------------------------------------------------------------------


def test_claim_of_pending_or_revoked_share_404(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        for state in ("pending", "revoked"):
            sid = await share(pool, a, who.you, state=state, audience="public")
            with pytest.raises(LibraryError) as e:
                await store.record_claim(who.me, sid)
            assert (e.value.status, e.value.code) == (404, "not_found")
        with pytest.raises(LibraryError) as e:
            await store.record_claim(who.me, "share_missing")
        assert e.value.status == 404
        gone = await share(pool, a, who.you, audience="public", expires=NOW - timedelta(hours=1))
        with pytest.raises(LibraryError):
            await store.record_claim(who.me, gone)
        assert (
            await pool.fetchval("SELECT count(*) FROM share_claims WHERE member_id=$1", who.me) == 0
        )

    run(migrated_dsn, body)


def test_lab_share_claim_refused(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        sid = await share(pool, a, who.you, audience="lab", emails=(who.email,))
        with pytest.raises(LibraryError) as e:
            await store.record_claim(who.me, sid)
        assert (e.value.status, e.value.code) == (404, "not_found")
        assert await pool.fetchval("SELECT count(*) FROM share_claims") == 0

    run(migrated_dsn, body)


def test_claim_is_idempotent_and_keeps_first_snapshot(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        sid = await share(pool, a, who.you, audience="public", title="First")
        await store.record_claim(who.me, sid)
        await pool.execute(
            "UPDATE shares SET title='Second', created_by='other' WHERE share_id=$1", sid
        )
        await store.record_claim(who.me, sid)
        rows = await pool.fetch("SELECT title, sharer FROM share_claims WHERE member_id=$1", who.me)
        assert [(r["title"], r["sharer"]) for r in rows] == [("First", who.you)]

    run(migrated_dsn, body)


def test_retracted_artifact_blocks_claim(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        sid = await share(pool, a, who.you, audience="public")
        await pool.execute("UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id=$1", a)
        with pytest.raises(LibraryError):
            await store.record_claim(who.me, sid)

    run(migrated_dsn, body)


def test_pending_artifact_with_live_share_not_claimable_or_opened(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you, intake="pending")
        sid = await share(pool, a, who.you, audience="public")
        with pytest.raises(LibraryError) as e:
            await store.record_claim(who.me, sid)
        assert e.value.status == 404
        await pool.execute(
            "INSERT INTO share_claims (member_id,share_id,title,sharer) VALUES ($1,$2,'t','s')",
            who.me,
            sid,
        )
        assert (
            await feed(store, who.viewer, sources=frozenset({Source.OPENED, Source.PENDING}))
        ).total == 0

    run(migrated_dsn, body)


# --- pending intake -----------------------------------------------------------------------------------------


def test_pending_cap_enforced_and_freed_by_accept_or_discard(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        assert PENDING_CAP == 25
        ids = [await artifact(pool, who.me, intake="pending") for _ in range(PENDING_CAP - 1)]
        await store.ensure_pending_capacity(who.me)  # 24 held: one more fits
        ids.append(await artifact(pool, who.me, intake="pending"))
        assert await store.count_pending(who.me) == PENDING_CAP
        with pytest.raises(PendingCapExceeded) as e:
            await store.ensure_pending_capacity(who.me)
        assert (e.value.status, e.value.code) == (429, "pending_cap") and str(
            PENDING_CAP
        ) in e.value.message
        await store.ensure_pending_capacity(who.you)  # per member
        await store.accept(ItemRef(who.me, ids[0]))
        await store.ensure_pending_capacity(who.me)
        ids.append(await artifact(pool, who.me, intake="pending"))
        with pytest.raises(PendingCapExceeded):
            await store.ensure_pending_capacity(who.me)
        await store.discard(ItemRef(who.me, ids[1]))
        assert await store.count_pending(who.me) == PENDING_CAP - 1
        retracted = await artifact(pool, who.me, intake="pending", lifecycle="retracted")
        assert (
            retracted and await store.count_pending(who.me) == PENDING_CAP - 1
        )  # retracted never counts

    run(migrated_dsn, body)


def test_accept_moves_to_mine_and_discard_removes(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        keep = await artifact(pool, who.me, name="Keep", intake="pending")
        drop = await artifact(pool, who.me, name="Drop", intake="pending")
        everything = frozenset(Source)
        assert {
            i.artifact_id: i.source
            for i in (await feed(store, who.viewer, sources=everything)).items
        } == {
            keep: Source.PENDING,
            drop: Source.PENDING,
        }
        await store.accept(ItemRef(who.me, keep))
        await store.discard(ItemRef(who.me, drop))
        items = (await feed(store, who.viewer, sources=everything)).items
        assert [(i.artifact_id, i.source, i.status) for i in items] == [
            (keep, Source.MINE, ItemStatus.ACTIVE)
        ]
        assert (
            await pool.fetchval("SELECT intake::text FROM artifacts WHERE artifact_id=$1", drop)
            == "discarded"
        )
        for op in (store.accept, store.discard):  # no longer pending
            for aid in (keep, drop):
                with pytest.raises(LibraryError) as e:
                    await op(ItemRef(who.me, aid))
                assert (e.value.status, e.value.code) == (404, "not_found")

    run(migrated_dsn, body)


def test_other_member_cannot_accept_or_discard_404(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        aid = await artifact(pool, who.me, intake="pending")
        for op in (store.accept, store.discard):
            with pytest.raises(LibraryError) as e:
                await op(ItemRef(who.you, aid))
            assert (e.value.status, e.value.code) == (404, "not_found")
        with pytest.raises(LibraryError):
            await store.accept(ItemRef(who.me, "artifact_" + "0" * 32))
        assert (
            await pool.fetchval("SELECT intake::text FROM artifacts WHERE artifact_id=$1", aid)
            == "pending"
        )

    run(migrated_dsn, body)


def test_can_read_matches_owner_and_unexpired_grant(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        assert not await store.can_read(who.me, a)
        await grant(pool, a, who.me, who.you)
        assert await store.can_read(who.me, a) and await store.can_read(who.you, a)
        await pool.execute("UPDATE artifact_acl SET expires_at=now() - interval '1 minute'")
        assert not await store.can_read(who.me, a)
        await pool.execute("UPDATE artifacts SET lifecycle='tombstoned' WHERE artifact_id=$1", a)
        assert not await store.can_read(who.you, a)

    run(migrated_dsn, body)


# --- viewer isolation ---------------------------------------------------------------------------------------


async def seed_world(pool: asyncpg.Pool, store: LibraryStore, who: Who) -> dict[str, str]:
    ids = {
        "me": await artifact(pool, who.me, name="Mine item"),
        "you": await artifact(pool, who.you, name="Yours item"),
        "you_pending": await artifact(pool, who.you, name="Yours pending", intake="pending"),
        "private": await artifact(pool, who.you, name="Private share"),
    }
    await share(pool, ids["private"], who.you, emails=("someone-else@x.org",))
    shared = await artifact(pool, who.you, name="Public link")
    sid = await share(pool, shared, who.you, audience="public")
    await store.record_claim(who.you, sid)  # claimed by YOU, not by ME
    ids["public"] = shared
    return ids


def test_operator_read_all_sees_every_members_items(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        ids = await seed_world(pool, store, who)
        page = await feed(store, OPERATOR_READ_ALL, limit=200)
        assert {i.artifact_id for i in page.items} == {
            ids["me"],
            ids["you"],
            ids["private"],
            ids["public"],
        }
        assert page.total == 4
        owners = {i.artifact_id: i.sharer for i in page.items}
        assert owners[ids["me"]] == who.me and owners[ids["you"]] == who.you
        with_pending = await feed(store, OPERATOR_READ_ALL, sources=frozenset(Source))
        assert with_pending.total == 5 and ids["you_pending"] in {
            i.artifact_id for i in with_pending.items
        }
        assert (
            await feed(store, OPERATOR_READ_ALL, q="yours item")
        ).total == 1  # search works for the operator
        assert (await feed(store, OPERATOR_READ_ALL, limit=2)).next_offset == 2

    run(migrated_dsn, body)


def test_operator_is_read_only_for_writes(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        f = await store.create_folder(who.me, "F")
        a = await artifact(pool, who.me)
        with pytest.raises(LibraryError) as e:
            await store.file_artifact(OPERATOR_READ_ALL, f.folder_id, a)
        assert e.value.status == 403
        assert await count_items(pool, f.folder_id) == 0

    run(migrated_dsn, body)


def test_member_never_sees_another_members_items_or_private_shares(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        ids = await seed_world(pool, store, who)
        everything = frozenset(Source)
        mine = await feed(store, who.viewer, sources=everything, limit=200)
        assert {i.artifact_id for i in mine.items} == {ids["me"]}
        theirs = await feed(store, Viewer(who.you), sources=everything, limit=200)
        assert {i.artifact_id for i in theirs.items} == {
            ids["you"],
            ids["you_pending"],
            ids["private"],
            ids["public"],
        }
        assert ids["me"] not in {i.artifact_id for i in theirs.items}
        stranger = await feed(store, Viewer("c" + who.me), sources=everything, limit=200)
        assert stranger.items == []
        # a private (lab) share is visible only to its allow-list
        listed = await feed(store, Viewer("c" + who.me, "someone-else@x.org"), sources=everything)
        assert [i.artifact_id for i in listed.items] == [ids["private"]]

    run(migrated_dsn, body)


def test_reads_leave_no_rows_changed(migrated_dsn: str, who: Who) -> None:
    tables = (
        "artifacts",
        "artifact_acl",
        "shares",
        "share_claims",
        "library_folders",
        "library_items",
    )

    async def snapshot(pool: asyncpg.Pool) -> list[tuple[str, int, str]]:
        out = []
        for t in tables:
            rows = await pool.fetch(f"SELECT t::text AS r FROM {t} t ORDER BY 1")  # noqa: S608  (fixed table names)
            out.append(
                (t, len(rows), hashlib.sha256("|".join(r["r"] for r in rows).encode()).hexdigest())
            )
        return out

    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        ids = await seed_world(pool, store, who)
        folder = await store.create_folder(who.me, "F")
        await store.file_artifact(who.viewer, folder.folder_id, ids["me"])
        before = await snapshot(pool)
        for viewer in (who.viewer, Viewer(who.you), OPERATOR_READ_ALL):
            await feed(store, viewer, sources=frozenset(Source), q="item", limit=1)
            await feed(store, viewer, folder_id=folder.folder_id)
        await store.list_folders(who.me)
        await store.count_pending(who.you)
        await store.can_read(who.me, ids["me"])
        assert await snapshot(pool) == before

    run(migrated_dsn, body)
