# ruff: noqa: F811  (pytest fixture imported by name)
"""Library search and filters, on real PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest

from herdr_artifact_host.library import FeedQuery, LibraryError, LibraryStore, Source, Viewer
from tests.artifact_host.library_support import NOW, Who, artifact, feed, run, share, who  # noqa: F401  (fixture)


async def mine(pool: asyncpg.Pool, who: Who, name: str | None, **cols: object) -> str:
    aid = await artifact(pool, who.me, name=name)
    for col, val in cols.items():
        await pool.execute(f"UPDATE artifacts SET {col}=$1 WHERE artifact_id=$2", val, aid)  # noqa: S608  (test column names)
    return aid


async def ids(store: LibraryStore, viewer: Viewer, q: str) -> set[str]:
    return {i.artifact_id for i in (await feed(store, viewer, q=q)).items}


def test_q_matches_title_filename_sharer_folder_name_and_share_title(
    migrated_dsn: str, who: Who
) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        by_title = await mine(pool, who, "Quarterly Budget")
        by_file = await mine(pool, who, None, original_filename="roadmap-final.pdf")
        by_type = await mine(pool, who, "Plain", artifact_type="spreadsheet")
        by_folder = await mine(pool, who, "Plain two")
        folder = await store.create_folder(who.me, "Onboarding")
        await store.file_artifact(who.viewer, folder.folder_id, by_folder)
        sharer = await artifact(pool, who.you, name="Zed")
        await share(pool, sharer, who.you, emails=(who.email,), title="Zed")
        titled = await artifact(pool, who.you, name="Plain three")
        await share(pool, titled, who.you, emails=(who.email,), title="Handbook")
        v = who.viewer
        assert await ids(store, v, "QUARTERLY") == {by_title}
        assert await ids(store, v, "ROADMAP-final") == {by_file}
        assert await ids(store, v, "spreadsheet") == {by_type}
        assert await ids(store, v, "onboard") == {by_folder}
        assert await ids(store, v, who.you) == {sharer, titled}
        assert await ids(store, v, "handbook") == {titled}  # share title is searchable
        assert await ids(store, v, "uarterly Bud") == {by_title}  # substring, not prefix

    run(migrated_dsn, body)


def test_search_falls_back_to_artifact_id_when_unnamed(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await mine(pool, who, None)
        assert await ids(store, who.viewer, a[-12:]) == {a}

    run(migrated_dsn, body)


def test_percent_and_underscore_are_literal(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        pct = await mine(pool, who, "100% done")
        us = await mine(pool, who, "a_b")
        await mine(pool, who, "axb")
        bs = await mine(pool, who, "back\\slash")
        assert await ids(store, who.viewer, "%") == {pct}
        assert await ids(store, who.viewer, "a_b") == {us}
        assert await ids(store, who.viewer, "\\") == {bs}
        assert await ids(store, who.viewer, "_") == {us}
        assert [i.title for i in (await feed(store, who.viewer, q="50%")).items] == []

    run(migrated_dsn, body)


def test_quote_and_semicolon_do_not_break_query(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await mine(pool, who, "it's; DROP TABLE artifacts; --")
        assert [i.artifact_id for i in (await feed(store, who.viewer, q="it's; DROP")).items] == [a]
        assert (await feed(store, who.viewer, q='"); --')).total == 0
        assert (await feed(store, who.viewer)).total == 1

    run(migrated_dsn, body)


def test_q_longer_than_200_refused(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, _pool: asyncpg.Pool) -> None:
        await feed(store, who.viewer, q="x" * 200)
        with pytest.raises(LibraryError) as e:
            await feed(store, who.viewer, q="x" * 201)
        assert (e.value.status, e.value.code) == (400, "query_too_long")

    run(migrated_dsn, body)


def test_revoked_row_does_not_leak_filename(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        a = await artifact(pool, who.you, name="Pub")
        await pool.execute(
            "UPDATE artifacts SET original_filename='secret-name.pdf' WHERE artifact_id=$1", a
        )
        sid = await share(pool, a, who.you, audience="public", title="Pub")
        await store.record_claim(who.me, sid)
        assert await ids(store, who.viewer, "secret-name") == {a}
        await pool.execute("UPDATE shares SET state='revoked' WHERE share_id=$1", sid)
        assert await ids(store, who.viewer, "secret-name") == set()

    run(migrated_dsn, body)


def test_combined_filters_are_anded(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        folder = await store.create_folder(who.me, "F")
        old = NOW - timedelta(days=30)
        hit = await mine(pool, who, "Hit", artifact_type="report")
        await store.file_artifact(who.viewer, folder.folder_id, hit)
        wrong_type = await mine(pool, who, "Hit", artifact_type="code")
        await store.file_artifact(who.viewer, folder.folder_id, wrong_type)
        await mine(pool, who, "Hit", artifact_type="report")  # not in the folder
        too_old = await mine(pool, who, "Hit", artifact_type="report", created_at=old)
        await store.file_artifact(who.viewer, folder.folder_id, too_old)
        other_source = await artifact(pool, who.you, name="Hit")
        await pool.execute(
            "UPDATE artifacts SET artifact_type='report' WHERE artifact_id=$1", other_source
        )
        await share(pool, other_source, who.you, emails=(who.email,), title="Hit")
        page = await store.feed(
            who.viewer,
            FeedQuery(
                q="hit",
                sources=frozenset({Source.MINE}),
                folder_id=folder.folder_id,
                artifact_type="report",
                since=NOW - timedelta(days=1),
                until=NOW + timedelta(days=1),
            ),
        )
        assert [i.artifact_id for i in page.items] == [hit] and page.total == 1

    run(migrated_dsn, body)


def test_paging_stable_and_total_correct_across_pages(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        made = [
            await mine(pool, who, f"Row {n}", created_at=NOW - timedelta(seconds=n // 3))
            for n in range(120)
        ]
        seen: list[str] = []
        offset: int | None = 0
        while offset is not None:
            page = await feed(store, who.viewer, q="row", limit=50, offset=offset)
            assert page.total == 120
            seen += [i.artifact_id for i in page.items]
            offset = page.next_offset
        assert len(seen) == 120 and set(seen) == set(made)
        again = [i.artifact_id for i in (await feed(store, who.viewer, q="row", limit=120)).items]
        assert again == seen

    run(migrated_dsn, body)


def test_search_never_returns_artifacts_member_cannot_see(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        secret = await artifact(pool, who.you, name="secret plans")
        await share(pool, secret, who.you, emails=("nobody@x.org",), title="secret plans")
        theirs = await store.create_folder(who.you, "secret folder")
        await store.file_artifact(Viewer(who.you), theirs.folder_id, secret)
        visible = await mine(pool, who, "visible")
        assert (await feed(store, who.viewer, q="secret")).total == 0
        assert (
            await feed(store, who.viewer, folder_id=theirs.folder_id)
        ).total == 0  # empty page, not 404
        # another member's folder name never matches my artifacts
        other = await store.create_folder(who.you, "visible-folder-xyz")
        await pool.execute(
            "INSERT INTO library_items (folder_id, artifact_id, source) VALUES ($1,$2,'manual')",
            other.folder_id,
            visible,
        )
        assert (await feed(store, who.viewer, q="visible-folder-xyz")).total == 0

    run(migrated_dsn, body)


def test_search_uses_the_trigram_index(migrated_dsn: str, who: Who) -> None:
    async def body(store: LibraryStore, pool: asyncpg.Pool) -> None:
        assert store
        async with pool.acquire() as conn:
            await conn.execute("SET enable_seqscan = off")
            plan = "\n".join(
                r[0]
                for r in await conn.fetch(
                    "EXPLAIN SELECT 1 FROM artifacts WHERE search_text ILIKE $1", "%abc%"
                )
            )
        assert "ix_artifacts_search_trgm" in plan

    run(migrated_dsn, body)
