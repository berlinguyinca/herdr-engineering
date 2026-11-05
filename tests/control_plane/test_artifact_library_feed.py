"""Claims and the unified library feed, on real PostgreSQL."""

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_library import (
    FeedItem,
    FeedPage,
    LibraryError,
    LibraryStore,
)
from tests.control_plane.test_share_store import MIGRATIONS, share_postgres  # noqa: F401  (fixture)

NOW = datetime.now(UTC)
GROUPS: frozenset[str] = frozenset()


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
    """Fresh member ids per test: the database is shared across the module."""
    global ME, YOU, ME_P, YOU_P, EMAIL, G1, G2
    ME, YOU = "a" + uuid.uuid4().hex[:8], "b" + uuid.uuid4().hex[:8]
    G1, G2 = "g1-" + ME, "g2-" + ME
    ME_P, YOU_P, EMAIL = f"member:{ME}", f"member:{YOU}", f"{ME}@x.org"


def run(dsn, body):
    async def go():
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            return await body(LibraryStore(pool), pool)
        finally:
            await pool.close()

    return asyncio.run(go())


async def artifact(pool, owner, *, name=None, intake="accepted"):
    aid = "artifact_" + uuid.uuid4().hex
    await pool.execute(
        "INSERT INTO artifacts (artifact_id,created_by,display_name,intake) VALUES ($1,$2,$3,$4)",
        aid,
        owner,
        name,
        intake,
    )
    return aid


async def share(
    pool,
    aid,
    *,
    state="live",
    audience="lab",
    emails=(),
    groups=(),
    expires=None,
    title="Shared",
    by=None,
):
    sid, vid = "share_" + uuid.uuid4().hex[:24], "version_" + uuid.uuid4().hex
    await pool.execute(
        "INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
        "VALUES ($1,$2,$3,'ready')",
        vid,
        aid,
        secrets.randbelow(10**9) + 1,
    )
    await pool.execute(
        "INSERT INTO artifact_shares (share_id,artifact_id,version_id,audience,allow_emails,allow_groups,state,"
        "expires_at,title,created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        sid,
        aid,
        vid,
        audience,
        list(emails),
        list(groups),
        state,
        expires,
        title,
        by or YOU_P,
    )
    return sid


async def feed(store, member=None, email=None, groups=GROUPS, **kw):
    return await store.feed(member or ME, email or EMAIL, groups, **kw)


def test_empty_member_gets_empty_feed(dsn):
    async def body(store, _pool):
        page = await feed(store)
        assert page == FeedPage([], 0, None)

    run(dsn, body)


def test_owned_direct_lab_and_claimed_all_appear(dsn):
    async def body(store, pool):
        mine = await artifact(pool, ME_P, name="Mine")
        direct = await artifact(pool, YOU_P, name="Direct")
        await pool.execute(
            "INSERT INTO artifact_acl (artifact_id,principal_id,granted_by) VALUES ($1,$2,$3)",
            direct,
            ME_P,
            YOU_P,
        )
        lab = await artifact(pool, YOU_P)
        await share(pool, lab, emails=[EMAIL], title="Lab one")
        grp = await artifact(pool, YOU_P)
        await share(pool, grp, groups=[G1], title="Grp one")
        opened = await artifact(pool, YOU_P)
        sid = await share(pool, opened, audience="public", title="Link")
        await store.record_claim(ME, sid)
        page = await feed(store, groups=frozenset({G1}))
        got = {i.artifact_id: i for i in page.items}
        assert page.total == 5 and page.next_offset is None
        assert isinstance(page.items[0], FeedItem)
        assert {k: v.source for k, v in got.items()} == {
            mine: "mine",
            direct: "assigned",
            lab: "assigned",
            grp: "assigned",
            opened: "opened",
        }
        assert got[mine].title == "Mine" and got[mine].status == "active"
        assert (
            got[lab].title == "Lab one"
            and got[lab].sharer == YOU_P
            and got[lab].share_id is not None
        )
        assert got[direct].sharer == YOU_P
        assert got[opened].share_id == sid
        assert [i.shared_at for i in page.items] == sorted(
            (i.shared_at for i in page.items), reverse=True
        )

    run(dsn, body)


def test_lab_share_for_other_group_not_listed(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P)
        await share(pool, a, emails=[f"o{ME}@x.org"], groups=[G2])
        b = await artifact(pool, YOU_P)
        await share(pool, b, audience="public")
        assert (await feed(store, groups=frozenset({G1}))).total == 0
        assert (await feed(store, groups=frozenset({G2}))).total == 1
        assert (await feed(store, email=f"O{ME}@X.org")).total == 1

    run(dsn, body)


def test_dedupe_one_row_badge_priority_mine_over_assigned_over_opened(dsn):
    async def body(store, pool):
        a = await artifact(pool, ME_P)
        sid = await share(pool, a, emails=[EMAIL])
        await store.record_claim(ME, sid)
        b = await artifact(pool, YOU_P)
        sid_b = await share(pool, b, emails=[EMAIL])
        await store.record_claim(ME, sid_b)
        page = await feed(store)
        assert {i.artifact_id: i.source for i in page.items} == {a: "mine", b: "assigned"}
        assert page.total == 2

    run(dsn, body)


def test_revoked_share_stays_as_revoked_row_with_snapshot_title(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P, name="Display")
        sid = await share(pool, a, audience="public", title="Snap")
        await store.record_claim(ME, sid)
        await pool.execute(
            "UPDATE artifact_shares SET state='revoked', title='Changed' WHERE share_id=$1", sid
        )
        (item,) = (await feed(store)).items
        assert (item.status, item.source, item.title, item.sharer) == (
            "revoked",
            "opened",
            "Snap",
            YOU_P,
        )

    run(dsn, body)


def test_expired_share_status_expired_read_live(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P)
        sid = await share(pool, a, audience="public", expires=NOW + timedelta(hours=1))
        await store.record_claim(ME, sid)
        assert (await feed(store)).items[0].status == "active"
        await pool.execute(
            "UPDATE artifact_shares SET expires_at=now() - interval '1 minute' WHERE share_id=$1",
            sid,
        )
        (item,) = (await feed(store)).items
        assert item.status == "expired" and item.expires_at is not None

    run(dsn, body)


def test_pending_only_visible_to_owner(dsn):
    async def body(store, pool):
        a = await artifact(pool, ME_P, name="Pushed", intake="pending")
        mine = await feed(store, sources=frozenset({"pending"}))
        assert [(i.artifact_id, i.source, i.status) for i in mine.items] == [
            (a, "pending", "pending")
        ]
        theirs = await feed(
            store, member=YOU, sources=frozenset({"pending", "mine", "assigned", "opened"})
        )
        assert theirs.items == [] and theirs.total == 0
        await pool.execute(
            "INSERT INTO artifact_acl (artifact_id,principal_id,granted_by) VALUES ($1,$2,$3)",
            a,
            YOU_P,
            ME_P,
        )
        assert (
            await feed(store, member=YOU, sources=frozenset({"pending", "assigned"}))
        ).total == 0

    run(dsn, body)


def test_retracted_artifact_vanishes_for_grantee_lab_recipient_and_claimant(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P, name="Gone")
        await pool.execute(
            "INSERT INTO artifact_acl (artifact_id,principal_id,granted_by) VALUES ($1,$2,$3)",
            a,
            ME_P,
            YOU_P,
        )
        sid = await share(pool, a, groups=[G1])
        await store.record_claim(ME, sid)
        everything = frozenset({"mine", "assigned", "opened"})
        assert (await feed(store, groups=frozenset({G1}), sources=everything)).total == 1
        await pool.execute("UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id=$1", a)
        for who, kw in ((ME, {"groups": frozenset({G1})}), (YOU, {})):
            page = await feed(store, member=who, sources=everything, **kw)
            assert page.items == [], who
        with pytest.raises(LibraryError):
            await store.record_claim(YOU, sid)

    run(dsn, body)


def test_pending_excluded_unless_source_includes_pending(dsn):
    async def body(store, pool):
        await artifact(pool, ME_P, name="Pushed", intake="pending")
        ok = await artifact(pool, ME_P, name="Accepted")
        assert [i.artifact_id for i in (await feed(store)).items] == [ok]
        assert [i.artifact_id for i in (await feed(store, sources=frozenset({"mine"}))).items] == [
            ok
        ]
        assert (await feed(store, sources=frozenset({"pending", "mine"}))).total == 2
        # filters apply before LIMIT and total counts the filtered set
        for n in range(3):
            await artifact(pool, ME_P, name=f"Plan_{n}%")
        page = await feed(store, q="plan_", limit=2)
        assert page.total == 3 and len(page.items) == 2 and page.next_offset == 2
        page2 = await feed(store, q="PLAN_", limit=2, offset=2)
        assert len(page2.items) == 1 and page2.next_offset is None
        assert (await feed(store, q="%")).total == 3
        assert (await feed(store, q="x_y")).total == 0

    run(dsn, body)


def test_claim_of_pending_or_revoked_share_404(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P)
        for state in ("pending", "revoked"):
            sid = await share(pool, a, state=state, audience="public")
            with pytest.raises(LibraryError) as e:
                await store.record_claim(ME, sid)
            assert (e.value.status, e.value.code) == (404, "not_found")
        with pytest.raises(LibraryError) as e:
            await store.record_claim(ME, "share_missing")
        assert e.value.status == 404
        gone = await share(pool, a, audience="public", expires=NOW - timedelta(hours=1))
        with pytest.raises(LibraryError):
            await store.record_claim(ME, gone)
        assert (
            await pool.fetchval("SELECT count(*) FROM artifact_share_claims WHERE member_id=$1", ME)
            == 0
        )

    run(dsn, body)


def test_claim_is_idempotent_and_keeps_first_snapshot(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P)
        sid = await share(pool, a, audience="public", title="First")
        await store.record_claim(ME, sid)
        await pool.execute(
            "UPDATE artifact_shares SET title='Second', created_by='member:other' WHERE share_id=$1",
            sid,
        )
        await store.record_claim(ME, sid)
        rows = await pool.fetch(
            "SELECT title, sharer FROM artifact_share_claims WHERE member_id=$1", ME
        )
        assert [(r["title"], r["sharer"]) for r in rows] == [("First", YOU_P)]

    run(dsn, body)


def test_revoked_lab_share_not_listed_for_never_assigned_member(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P)
        await share(pool, a, state="revoked", emails=[EMAIL], groups=[G1])
        assert (await feed(store, groups=frozenset({G1}))).total == 0

    run(dsn, body)


def test_q_percent_and_underscore_are_literal(dsn):
    async def body(store, pool):
        await artifact(pool, ME_P, name="a_b")
        await artifact(pool, ME_P, name="axb")
        await artifact(pool, ME_P, name="50% off")
        await artifact(pool, ME_P, name="500 off")
        assert [i.title for i in (await feed(store, q="a_b")).items] == ["a_b"]
        assert [i.title for i in (await feed(store, q="50%")).items] == ["50% off"]

    run(dsn, body)


def test_pending_artifact_with_live_share_not_claimable_or_opened(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P, intake="pending")
        sid = await share(pool, a, audience="public")
        with pytest.raises(LibraryError) as e:
            await store.record_claim(ME, sid)
        assert e.value.status == 404
        await pool.execute(
            "INSERT INTO artifact_share_claims (member_id,share_id,title,sharer) VALUES ($1,$2,'t','s')",
            ME,
            sid,
        )
        assert (await feed(store, sources=frozenset({"opened", "pending"}))).total == 0

    run(dsn, body)


def test_revoked_opened_row_has_blank_type_and_size(dsn):
    async def body(store, pool):
        a = await artifact(pool, YOU_P)
        await pool.execute(
            "UPDATE artifacts SET artifact_type='report', size_bytes=99 WHERE artifact_id=$1", a
        )
        sid = await share(pool, a, audience="public")
        await store.record_claim(ME, sid)
        (live,) = (await feed(store)).items
        assert (live.artifact_type, live.size_bytes) == ("report", 99)
        await pool.execute("UPDATE artifact_shares SET state='revoked' WHERE share_id=$1", sid)
        (item,) = (await feed(store)).items
        assert (item.status, item.artifact_type, item.size_bytes) == ("revoked", "", 0)

    run(dsn, body)
