"""Complete, private-safe event history from a disposable PostgreSQL journal."""
import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.durable_history import DurableHistoryReader
from tests.control_plane.test_postgres_recovery import _insert, _row, _snapshot
from tests.control_plane.test_recovery_checkpoint import _build
from tests.control_plane.test_recovery_checkpoint import pg as checkpoint_pg

asyncpg = pytest.importorskip("asyncpg")
pg = checkpoint_pg


@pytest.fixture
def db(pg):
    pg.query("TRUNCATE fabric_events")
    pg.query("TRUNCATE recovery_checkpoint_manifests CASCADE")
    return pg


def run(db, action):
    async def exercise():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await action(DurableHistoryReader(conn), conn)
        finally:
            await conn.close()
    return asyncio.run(exercise())


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("kind", ["host", "session", "service"])
def test_equivalent_snapshots_all_survive_pages_and_legacy_stream(db, kind):
    event_type, payload = {
        "host": ("HostHeartbeat", dict(host_name="node", tailnet_ip="100.64.0.1", cpu=1, mem=2)),
        "session": ("SessionUpdated", dict(host_id="h", mission_id=None, agent_role="pi", model="m", status="active")),
        "service": ("ServiceUpdated", _snapshot()),
    }[kind]
    rows = [_row(kind, "h|'quoted", event_type, payload, n) for n in range(123)]
    _insert(db, rows)
    report = _build(db)
    assert report["covered_events"] == 123 and report["selected_events"] == 2

    async def exercise(reader, conn):
        page = await reader.page(kind, "h|'quoted")
        assert len(page["events"]) == 50
        assert page["latest_sequence"] == 122 and page["last_event_id"] == rows[-1]["event_id"]
        groups = [page["events"]]
        while page["has_more"]:
            page = await reader.page(kind, "h|'quoted", limit=17, before=page["next_cursor"])
            groups.append(page["events"])
        assert page["next_cursor"] is None
        events = [event for group in reversed(groups) for event in group]
        assert [event["event_id"] for event in events] == [row["event_id"] for row in rows]
        assert await reader.stream(kind, "h|'quoted") == events
        assert all(event["payload"] == payload for event in events)
        assert all("recovery_instance" not in event and "history_actor_domain" not in event for event in events)
        assert await reader.page(kind, "missing") == {
            "events": [], "has_more": False, "next_cursor": None,
            "latest_sequence": None, "last_event_id": None,
        }
        assert await reader.stream(kind, "missing") == []
    run(db, exercise)


def test_ties_gaps_actor_domains_and_append_keep_older_pages_stable(db):
    stamp = datetime(2026, 10, 28, tzinfo=UTC)
    specs = [(0, 3, "a", None), (7, 2, "same", {"id": "a"}),
             (7, 2, "same", {"id": "b"}), (7, 2, "z", {"id": 12}),
             (7, 4, "later", None), (99, 1, "a", {"id": "last"})]
    rows = []
    for index, (sequence, arrival, event_id, actor) in enumerate(specs):
        row = _row("session", "s", "Snapshot", {"index": index}, index, actor=actor)
        row.update(sequence=sequence, ingest_timestamp=stamp + timedelta(seconds=arrival), event_id=event_id)
        rows.append(row)
    _insert(db, rows)

    async def exercise(reader, conn):
        page = await reader.page("session", "s", limit=3)
        assert [e["payload"]["index"] for e in page["events"]] == [3, 4, 5]
        cursor = page["next_cursor"]
        older = await reader.page("session", "s", limit=3, before=cursor)
        assert [e["payload"]["index"] for e in older["events"]] == [0, 1, 2]
        await conn.execute("""INSERT INTO fabric_events(event_id, entity_type, entity_id, event_type,
            sequence, source_timestamp, ingest_timestamp, payload)
            VALUES('appended', 'session', 's', 'Snapshot', 100, $1, $1, '{}')""", stamp)
        again = await reader.page("session", "s", limit=3, before=cursor)
        assert again["events"] == older["events"]
        assert again["latest_sequence"] == 100 and again["last_event_id"] == "appended"
        assert [e["payload"].get("index") for e in await reader.stream("session", "s")] == [0, 1, 2, 3, 4, 5, None]
    run(db, exercise)


@pytest.mark.parametrize("mutation", ["delete", "update", "same_update", "reinsert", "content_only"])
def test_anchor_mutations_are_stale_even_with_same_identity_and_content(db, mutation):
    _insert(db, [_row("host", "h", "Snapshot", {"unchanged": True}, n) for n in range(4)])

    async def exercise(reader, conn):
        cursor = (await reader.page("host", "h", limit=2))["next_cursor"]
        if mutation == "content_only":
            async with conn.transaction():
                await conn.execute("ALTER TABLE fabric_events DISABLE TRIGGER fabric_event_recovery_instance")
                await conn.execute("UPDATE fabric_events SET payload='{}' WHERE event_id='host-h-2'")
                await conn.execute("ALTER TABLE fabric_events ENABLE TRIGGER fabric_event_recovery_instance")
        elif mutation == "update":
            await conn.execute("UPDATE fabric_events SET payload='{}' WHERE event_id='host-h-2'")
        elif mutation == "same_update":
            await conn.execute("UPDATE fabric_events SET payload=payload WHERE event_id='host-h-2'")
        elif mutation == "reinsert":
            await conn.execute("CREATE TEMP TABLE held AS SELECT * FROM fabric_events WHERE event_id='host-h-2'")
            await conn.execute("DELETE FROM fabric_events WHERE event_id='host-h-2'")
            await conn.execute("INSERT INTO fabric_events SELECT * FROM held")
        else:
            await conn.execute("DELETE FROM fabric_events WHERE event_id='host-h-2'")
        with pytest.raises(ValueError, match="^staleHistoryCursor$"):
            await reader.page("host", "h", before=cursor)
    run(db, exercise)


def test_cursor_hides_secret_identifiers_payload_and_actor_domain(db):
    secret = "token-private-event"
    rows = [_row("service", "secret-entity", "Snapshot", {"credential": "payload-secret"}, n,
                 actor={"id": "actor-secret"}) for n in range(4)]
    for index, row in enumerate(rows):
        row["event_id"] = f"{secret}-{index}"
    _insert(db, rows)

    async def exercise(reader, conn):
        cursor = (await reader.page("service", "secret-entity", limit=2))["next_cursor"]
        assert len(cursor) <= 512
        data = decode(cursor)
        assert len(data) == 6 and data[0] == 2
        for sensitive in (secret, "actor-secret", "payload-secret", "secret-entity"):
            assert sensitive not in json.dumps(data)
        for entity_type, entity_id in (("host", "secret-entity"), ("service", "different")):
            with pytest.raises(ValueError, match="^invalid history cursor$"):
                await reader.page(entity_type, entity_id, before=cursor)
        # Memory offsets cannot be used against a compacted/durable stream.
        legacy = encode([1, 2, digest(["service", "secret-entity"]), "a" * 64])
        with pytest.raises(ValueError, match="^staleHistoryCursor$"):
            await reader.page("service", "secret-entity", before=legacy)
    run(db, exercise)


@pytest.mark.parametrize("kind", ["mission", "agent_identity", "collab", "ci_fix", "", None])
def test_private_and_noncompacted_stream_types_are_refused(kind):
    reader = DurableHistoryReader(None)
    with pytest.raises(ValueError, match="entity"):
        asyncio.run(reader.page(kind, "id"))
    with pytest.raises(ValueError, match="entity"):
        asyncio.run(reader.stream(kind, "id"))


@pytest.mark.parametrize("table", ["fabric_events;DROP TABLE x", "public.fabric_events", 'x"', "", None])
def test_table_name_requires_one_trusted_identifier(table):
    with pytest.raises(ValueError, match="table"):
        DurableHistoryReader(None, events_table=table)


@pytest.mark.parametrize("limit", [True, 0, -1, 201, 1.5, "5", None])
def test_invalid_limits_fail_before_database_access(limit):
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(DurableHistoryReader(None).page("host", "h", limit=limit))


@pytest.mark.parametrize("index,value", [
    (0, True), (0, 3), (1, "a" * 63), (1, "b" * 64),
    (2, True), (2, 1.5), (2, -(2**63) - 1), (2, 2**63),
    (3, None), (3, "2026-10-28"), (3, "2026-10-28T12:00:00"), (3, "bad+00:00"),
    (4, "x" * 64), (4, False), (5, "a" * 63), (5, None),
])
def test_cursor_structure_is_validated_before_database_access(index, value):
    data = [2, digest(["host", "h"]), 7, "2026-10-28T12:00:00+00:00", "a" * 64, "b" * 64]
    data[index] = value
    with pytest.raises(ValueError, match="^invalid history cursor$"):
        asyncio.run(DurableHistoryReader(None).page("host", "h", before=encode(data)))


@pytest.mark.parametrize("cursor", [True, 1, "", "!", "a" * 513, "not/base64", encode({}), encode([1, True, "a" * 64, "b" * 64])])
def test_invalid_cursor_encoding(cursor):
    with pytest.raises(ValueError, match="^invalid history cursor$"):
        asyncio.run(DurableHistoryReader(None).page("host", "h", before=cursor))


def test_database_failure_propagates_without_memory_fallback(db):
    async def exercise(reader, conn):
        broken = DurableHistoryReader(conn, events_table="absent_history")
        for action in (broken.page, broken.stream):
            with pytest.raises(asyncpg.UndefinedTableError):
                await action("host", "h")
    run(db, exercise)


def test_latest_metadata_and_page_share_one_readonly_repeatable_read_snapshot(db):
    _insert(db, [_row("host", "h", "Snapshot", {}, n) for n in range(3)])

    async def exercise(reader, conn):
        writer = await asyncpg.connect(db.dsn)
        reached, resume = asyncio.Event(), asyncio.Event()

        class PausedConnection:
            def transaction(self, **kwargs):
                return conn.transaction(**kwargs)

            async def fetchrow(self, query, *args):
                result = await conn.fetchrow(query, *args)
                reached.set()
                await resume.wait()
                return result

            async def fetch(self, query, *args):
                return await conn.fetch(query, *args)

        task = asyncio.create_task(DurableHistoryReader(PausedConnection()).page("host", "h", limit=2))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            assert await conn.fetchval("SHOW transaction_isolation") == "repeatable read"
            assert await conn.fetchval("SHOW transaction_read_only") == "on"
            await writer.execute("""INSERT INTO fabric_events(event_id, entity_type, entity_id, event_type,
                sequence, source_timestamp, payload) VALUES('new', 'host', 'h', 'Snapshot', 3, now(), '{}')""")
            resume.set()
            page = await asyncio.wait_for(task, 5)
            assert page["latest_sequence"] == 2 and page["last_event_id"] == "host-h-2"
            assert [event["sequence"] for event in page["events"]] == [1, 2]
            assert (await reader.page("host", "h"))["last_event_id"] == "new"
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await writer.close()
    run(db, exercise)
