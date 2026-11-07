"""Library search and filters, on real PostgreSQL."""

from datetime import timedelta

import pytest

import tests.control_plane.test_artifact_library_feed as base
from herdr_engineering.control_plane.artifact_library import LibraryError
from tests.control_plane.test_artifact_library_feed import (  # noqa: F401  F811 (fixtures)
    NOW,
    artifact,
    dsn,
    feed,
    members,
    run,
    share,
    share_postgres,
)


async def mine(pool, name, **cols):
    aid = await artifact(pool, base.ME_P, name=name)
    for col, val in cols.items():
        await pool.execute(f"UPDATE artifacts SET {col}=$1 WHERE artifact_id=$2", val, aid)  # noqa: S608
    return aid


def test_q_matches_title_filename_sharer_and_folder_name(dsn):  # noqa: F811
    async def body(store, pool):
        by_title = await mine(pool, "Quarterly Budget")
        by_file = await mine(pool, None, original_filename="roadmap-final.pdf")
        by_type = await mine(pool, "Plain", artifact_type="spreadsheet")
        by_folder = await mine(pool, "Plain two")
        folder = await store.create_folder(base.ME_P, "Onboarding")
        await store.file_artifact(base.ME_P, folder.folder_id, by_folder)
        sharer = await artifact(pool, base.YOU_P, name="Zed")
        await share(pool, sharer, emails=[base.EMAIL], title="Zed")

        async def ids(q):
            return {i.artifact_id for i in (await feed(store, q=q)).items}

        assert await ids("QUARTERLY") == {by_title}
        assert await ids("ROADMAP-final") == {by_file}
        assert await ids("spreadsheet") == {by_type}
        assert await ids("onboard") == {by_folder}
        assert await ids(base.YOU) == {sharer}

    run(dsn, body)


def test_percent_and_underscore_are_literal(dsn):  # noqa: F811
    async def body(store, pool):
        pct = await mine(pool, "100% done")
        us = await mine(pool, "a_b")
        await mine(pool, "axb")
        bs = await mine(pool, "back\\slash")

        async def ids(q):
            return {i.artifact_id for i in (await feed(store, q=q)).items}

        assert await ids("%") == {pct}
        assert await ids("a_b") == {us}
        assert await ids("\\") == {bs}

    run(dsn, body)


def test_quote_and_semicolon_do_not_break_query(dsn):  # noqa: F811
    async def body(store, pool):
        a = await mine(pool, "it's; DROP TABLE artifacts; --")
        assert [i.artifact_id for i in (await feed(store, q="it's; DROP")).items] == [a]
        assert (await feed(store, q='"); --')).total == 0
        assert (await feed(store)).total == 1

    run(dsn, body)


def test_q_longer_than_200_refused(dsn):  # noqa: F811
    async def body(store, pool):
        await feed(store, q="x" * 200)
        with pytest.raises(LibraryError) as e:
            await feed(store, q="x" * 201)
        assert (e.value.status, e.value.code) == (400, "query_too_long")

    run(dsn, body)


def test_combined_filters_are_anded(dsn):  # noqa: F811
    async def body(store, pool):
        folder = await store.create_folder(base.ME_P, "F")
        old = NOW - timedelta(days=30)
        hit = await mine(pool, "Hit", artifact_type="report")
        await store.file_artifact(base.ME_P, folder.folder_id, hit)
        wrong_type = await mine(pool, "Hit", artifact_type="code")
        await store.file_artifact(base.ME_P, folder.folder_id, wrong_type)
        no_folder = await mine(pool, "Hit", artifact_type="report")
        too_old = await mine(pool, "Hit", artifact_type="report", created_at=old)
        await store.file_artifact(base.ME_P, folder.folder_id, too_old)
        other_source = await artifact(pool, base.YOU_P, name="Hit")
        await pool.execute(
            "UPDATE artifacts SET artifact_type='report' WHERE artifact_id=$1", other_source
        )
        await share(pool, other_source, emails=[base.EMAIL], title="Hit")
        assert no_folder and other_source
        page = await feed(
            store,
            q="hit",
            sources=frozenset({"mine"}),
            folder_id=folder.folder_id,
            artifact_type="report",
            since=NOW - timedelta(days=1),
            until=NOW + timedelta(days=1),
        )
        assert [i.artifact_id for i in page.items] == [hit] and page.total == 1

    run(dsn, body)


def test_paging_stable_and_total_correct_across_pages(dsn):  # noqa: F811
    async def body(store, pool):
        ids = []
        for n in range(120):
            ids.append(await mine(pool, f"Row {n}", created_at=NOW - timedelta(seconds=n // 3)))
        seen: list[str] = []
        offset = 0
        while offset is not None:
            page = await feed(store, q="row", limit=50, offset=offset)
            assert page.total == 120
            seen += [i.artifact_id for i in page.items]
            offset = page.next_offset
        assert len(seen) == 120 and set(seen) == set(ids)
        again = [i.artifact_id for i in (await feed(store, q="row", limit=120)).items]
        assert again == seen

    run(dsn, body)


def test_search_never_returns_artifacts_member_cannot_see(dsn):  # noqa: F811
    async def body(store, pool):
        secret = await artifact(pool, base.YOU_P, name="secret plans")
        await share(pool, secret, emails=["nobody@x.org"], title="secret plans")
        theirs = await store.create_folder(base.YOU_P, "secret folder")
        await store.file_artifact(base.YOU_P, theirs.folder_id, secret)
        visible = await mine(pool, "visible")
        assert (await feed(store, q="secret")).total == 0
        assert (await feed(store, folder_id=theirs.folder_id)).total == 0  # empty page, not 404
        # another member's folder name never matches my artifacts
        mine_in_theirs = await store.create_folder(base.YOU_P, "visible-folder-xyz")
        await pool.execute(
            "INSERT INTO artifact_library_items (folder_id, artifact_id, source) VALUES ($1,$2,'manual')",
            mine_in_theirs.folder_id,
            visible,
        )
        assert (await feed(store, q="visible-folder-xyz")).total == 0

    run(dsn, body)
