# ruff: noqa: F811  (pytest fixture imported by name)
"""The unified library feed, on real PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest

from herdr_artifact_host.library import (
    FeedItem,
    FeedPage,
    ItemStatus,
    LibraryError,
    LibraryStore,
    Source,
)
from tests.artifact_host.library_support import NOW, Who, artifact, feed, grant, run, share, who  # noqa: F401  (fixture)

EVERYTHING = frozenset({Source.MINE, Source.ASSIGNED, Source.OPENED})


def test_empty_member_gets_empty_feed(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, _pool: asyncpg.Pool) -> None:
        assert await feed(store, who.viewer) == FeedPage([], 0, None)

    run(migrated_dsn, body)


def test_owned_direct_lab_and_claimed_all_appear(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        mine = await artifact(pool, who.me, name="Mine")
        direct = await artifact(pool, who.you, name="Direct")
        await grant(pool, direct, who.me, who.you)
        lab = await artifact(pool, who.you)
        await share(pool, lab, who.you, emails=(who.email,), title="Lab one")
        grp = await artifact(pool, who.you)
        await share(pool, grp, who.you, groups=(who.g1,), title="Grp one")
        opened = await artifact(pool, who.you)
        sid = await share(pool, opened, who.you, audience="public", title="Link")
        await store.record_claim(who.me, sid)
        page = await feed(store, who.viewer_in(who.g1))
        got = {i.artifact_id: i for i in page.items}
        assert page.total == 5 and page.next_offset is None
        assert isinstance(page.items[0], FeedItem)
        assert {k: v.source for k, v in got.items()} == {
            mine: Source.MINE,
            direct: Source.ASSIGNED,
            lab: Source.ASSIGNED,
            grp: Source.ASSIGNED,
            opened: Source.OPENED,
        }
        assert got[mine].title == "Mine" and got[mine].status == ItemStatus.ACTIVE
        assert (
            got[lab].title == "Lab one"
            and got[lab].sharer == who.you
            and got[lab].share_id is not None
        )
        assert got[direct].sharer == who.you
        assert got[opened].share_id == sid
        assert [i.shared_at for i in page.items] == sorted(
            (i.shared_at for i in page.items), reverse=True
        )

    run(migrated_dsn, body)


def test_lab_share_for_other_group_not_listed(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        await share(pool, a, who.you, emails=(f"o{who.me}@x.org",), groups=(who.g2,))
        b = await artifact(pool, who.you)
        await share(pool, b, who.you, audience="public")
        assert (await feed(store, who.viewer_in(who.g1))).total == 0
        assert (await feed(store, who.viewer_in(who.g2))).total == 1
        other = type(who.viewer)(who.me, f"O{who.me}@X.org")
        assert (await feed(store, other)).total == 1

    run(migrated_dsn, body)


def test_dedupe_one_row_badge_priority_mine_over_assigned_over_opened(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.me)
        await store.record_claim(who.me, await share(pool, a, who.me, audience="public"))
        b = await artifact(pool, who.you)
        await share(pool, b, who.you, emails=(who.email,))
        page = await feed(store, who.viewer)
        assert {i.artifact_id: i.source for i in page.items} == {a: Source.MINE, b: Source.ASSIGNED}
        assert page.total == 2

    run(migrated_dsn, body)


def test_revoked_share_stays_as_revoked_row_with_snapshot_title(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you, name="Display")
        sid = await share(pool, a, who.you, audience="public", title="Snap")
        await store.record_claim(who.me, sid)
        await pool.execute(
            "UPDATE shares SET state='revoked', title='Changed' WHERE share_id=$1", sid
        )
        (item,) = (await feed(store, who.viewer)).items
        assert (item.status, item.source, item.title, item.sharer) == (
            ItemStatus.REVOKED,
            Source.OPENED,
            "Snap",
            who.you,
        )

    run(migrated_dsn, body)


def test_expired_share_status_expired_read_live(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        sid = await share(pool, a, who.you, audience="public", expires=NOW + timedelta(hours=1))
        await store.record_claim(who.me, sid)
        assert (await feed(store, who.viewer)).items[0].status == ItemStatus.ACTIVE
        await pool.execute(
            "UPDATE shares SET expires_at=now() - interval '1 minute' WHERE share_id=$1", sid
        )
        (item,) = (await feed(store, who.viewer)).items
        assert item.status == ItemStatus.EXPIRED and item.expires_at is not None

    run(migrated_dsn, body)


def test_pending_only_visible_to_owner(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.me, name="Pushed", intake="pending")
        mine = await feed(store, who.viewer, sources=frozenset({Source.PENDING}))
        assert [(i.artifact_id, i.source, i.status) for i in mine.items] == [
            (a, Source.PENDING, ItemStatus.PENDING)
        ]
        theirs = await feed(store, type(who.viewer)(who.you), sources=frozenset(Source))
        assert theirs.items == [] and theirs.total == 0
        await grant(pool, a, who.you, who.me)
        granted = await feed(
            store, type(who.viewer)(who.you), sources=frozenset({Source.PENDING, Source.ASSIGNED})
        )
        assert granted.total == 0

    run(migrated_dsn, body)


def test_retracted_artifact_vanishes_for_grantee_lab_recipient_and_claimant(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you, name="Gone")
        await grant(pool, a, who.me, who.you)
        await share(pool, a, who.you, groups=(who.g1,))
        c = await artifact(pool, who.you, name="Gone link")
        sid = await share(pool, c, who.you, audience="public")
        await store.record_claim(who.me, sid)
        assert (await feed(store, who.viewer_in(who.g1), sources=EVERYTHING)).total == 2
        await pool.execute(
            "UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id IN ($1,$2)", a, c
        )
        for viewer in (who.viewer_in(who.g1), type(who.viewer)(who.you)):
            page = await feed(store, viewer, sources=EVERYTHING)
            assert page.items == [], viewer.member_id
        with pytest.raises(LibraryError):
            await store.record_claim(who.you, sid)

    run(migrated_dsn, body)


def test_pending_excluded_unless_source_includes_pending(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        await artifact(pool, who.me, name="Pushed", intake="pending")
        ok = await artifact(pool, who.me, name="Accepted")
        assert [i.artifact_id for i in (await feed(store, who.viewer)).items] == [ok]
        assert [
            i.artifact_id
            for i in (await feed(store, who.viewer, sources=frozenset({Source.MINE}))).items
        ] == [ok]
        assert (
            await feed(store, who.viewer, sources=frozenset({Source.PENDING, Source.MINE}))
        ).total == 2
        # filters apply before LIMIT and total counts the filtered set
        for n in range(3):
            await artifact(pool, who.me, name=f"Plan_{n}%")
        page = await feed(store, who.viewer, q="plan_", limit=2)
        assert page.total == 3 and len(page.items) == 2 and page.next_offset == 2
        page2 = await feed(store, who.viewer, q="PLAN_", limit=2, offset=2)
        assert len(page2.items) == 1 and page2.next_offset is None
        assert (await feed(store, who.viewer, q="%")).total == 3
        assert (await feed(store, who.viewer, q="x_y")).total == 0

    run(migrated_dsn, body)


def test_revoked_lab_share_not_listed_for_never_assigned_member(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        await share(pool, a, who.you, state="revoked", emails=(who.email,), groups=(who.g1,))
        assert (await feed(store, who.viewer_in(who.g1))).total == 0

    run(migrated_dsn, body)


def test_revoked_opened_row_has_blank_type_and_size(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        await pool.execute(
            "UPDATE artifacts SET artifact_type='report', size_bytes=99 WHERE artifact_id=$1", a
        )
        sid = await share(pool, a, who.you, audience="public")
        await store.record_claim(who.me, sid)
        (live,) = (await feed(store, who.viewer)).items
        assert (live.artifact_type, live.size_bytes) == ("report", 99)
        await pool.execute("UPDATE shares SET state='revoked' WHERE share_id=$1", sid)
        (item,) = (await feed(store, who.viewer)).items
        assert (item.status, item.artifact_type, item.size_bytes) == (ItemStatus.REVOKED, "", 0)

    run(migrated_dsn, body)


def test_opened_requires_public_audience(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you)
        sid = await share(pool, a, who.you, audience="lab", emails=("nobody@x.org",))
        await pool.execute(
            "INSERT INTO share_claims (member_id,share_id,title,sharer) VALUES ($1,$2,'t','s')",
            who.me,
            sid,
        )
        assert (await feed(store, who.viewer, sources=frozenset({Source.OPENED}))).total == 0

    run(migrated_dsn, body)
