"""Ingesting ``claimed`` share events into member libraries, on real PostgreSQL."""
import asyncio
import json

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_members import claim_resolver
from tests.control_plane.test_share_store import (  # noqa: F401  (fixture)
    MIGRATIONS,
    index,
    kw,
    new_token,
    run,
    seed,
    share_postgres,
)

GROUP = "lab-members"
DIRECTORY = {
    "alice": {"status": "approved", "groups": [GROUP]},
    "bob": {"status": "disabled", "groups": [GROUP]},
    "carol": {"status": "approved", "groups": ["other"]},
    "dave": {"status": "unknown", "groups": [GROUP]},
}


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


def claimed(sid, actor, n=0):
    return json.dumps({"share_id": sid, "kind": "claimed", "actor": actor, "at": f"2099-01-01T10:00:{n:02d}Z",
                       "detail": {"share_id": sid}}) + "\n"


async def live_share(store, conn, **over):
    aid, vid = await seed(conn)
    sid = (await store.create(**kw(aid, vid, audience="public", allow_emails=[], allow_groups=[],
                                   expires_at=_soon(), **over)))["share_id"]
    await store.mark_live(sid, token_hash=new_token()[1], index=index(), approved_by="op")
    return sid


def _soon():
    from datetime import UTC, datetime, timedelta
    return datetime.now(UTC) + timedelta(days=5)


async def claim_rows(conn, sid):
    return await conn.fetch("SELECT member_id, title FROM artifact_share_claims WHERE share_id=$1 ORDER BY member_id", sid)


def test_approved_member_claim_is_stored_and_kept_in_the_library(dsn, tmp_path):
    async def body(store, conn):
        sid = await live_share(store, conn)
        f = tmp_path / "events.jsonl"
        f.write_text(claimed(sid, "alice"))
        assert await store.ingest_events_file(f, members=claim_resolver(DIRECTORY, GROUP)) == 1
        assert [(r["member_id"], r["title"]) for r in await claim_rows(conn, sid)] == [("alice", "T")]
        evs = [e for e in await store.events(sid) if e["kind"] == "claimed"]
        assert len(evs) == 1 and evs[0]["actor"] == "alice" and evs[0]["detail"] == {"share_id": sid}
    run(dsn, body)


def test_unknown_unapproved_or_ungrouped_actor_is_ignored_without_error(dsn, tmp_path):
    async def body(store, conn):
        sid = await live_share(store, conn)
        f = tmp_path / "events.jsonl"
        f.write_text("".join(claimed(sid, a, i) for i, a in enumerate(["nobody", "bob", "carol", "dave"])))
        assert await store.ingest_events_file(f, members=claim_resolver(DIRECTORY, GROUP)) == 0
        assert await claim_rows(conn, sid) == []
        assert [e for e in await store.events(sid) if e["kind"] == "claimed"] == []
    run(dsn, body)


def test_claims_are_dropped_without_a_resolver_or_with_a_missing_directory(dsn, tmp_path):
    async def body(store, conn):
        sid = await live_share(store, conn)
        f = tmp_path / "events.jsonl"
        f.write_text(claimed(sid, "alice"))
        assert await store.ingest_events_file(f) == 0
        g = tmp_path / "other.jsonl"
        g.write_text(claimed(sid, "alice"))
        assert await store.ingest_events_file(g, members=claim_resolver(None, GROUP)) == 0
        assert await claim_rows(conn, sid) == []
    run(dsn, body)


def test_replays_are_idempotent(dsn, tmp_path):
    async def body(store, conn):
        sid = await live_share(store, conn)
        members = claim_resolver(DIRECTORY, GROUP)
        f = tmp_path / "events.jsonl"
        f.write_text(claimed(sid, "alice"))
        assert await store.ingest_events_file(f, members=members) == 1
        assert await store.ingest_events_file(f, members=members) == 0
        for p in tmp_path.iterdir():                                # lose the offset marker: a full replay
            if p.name.startswith("events.jsonl.") and p != f:
                p.unlink()
        assert await store.ingest_events_file(f, members=members) == 0
        again = tmp_path / "again.jsonl"                            # the same claim from a second session
        again.write_text(claimed(sid, "alice", 7))
        await store.ingest_events_file(again, members=members)
        assert len(await claim_rows(conn, sid)) == 1
    run(dsn, body)


def test_claim_on_a_share_that_is_not_live_is_silently_refused(dsn, tmp_path):
    async def body(store, conn):
        sid = await live_share(store, conn)
        await store.mark_revoked(sid, actor="op")
        f = tmp_path / "events.jsonl"
        f.write_text(claimed(sid, "alice"))
        await store.ingest_events_file(f, members=claim_resolver(DIRECTORY, GROUP))   # must not raise
        assert await claim_rows(conn, sid) == []
    run(dsn, body)


def test_claim_on_a_retracted_artifact_is_silently_refused(dsn, tmp_path):
    async def body(store, conn):
        sid = await live_share(store, conn)
        await conn.execute("UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id="
                           "(SELECT artifact_id FROM artifact_shares WHERE share_id=$1)", sid)
        f = tmp_path / "events.jsonl"
        f.write_text(claimed(sid, "alice"))
        await store.ingest_events_file(f, members=claim_resolver(DIRECTORY, GROUP))
        assert await claim_rows(conn, sid) == []
    run(dsn, body)


def test_claim_resolver_follows_the_member_for_actor_rules():
    resolve = claim_resolver(DIRECTORY, GROUP)
    assert resolve("alice") == "alice"
    assert [resolve(a) for a in ("bob", "carol", "dave", "nobody", "")] == [None] * 5
    assert claim_resolver(None, GROUP)("alice") is None
