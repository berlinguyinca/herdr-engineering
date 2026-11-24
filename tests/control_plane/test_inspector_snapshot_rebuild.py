"""The snapshot rebuild pages through the inspector rows with bounded memory and an index, against a real Postgres."""
import asyncio
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_engineering.control_plane import inspector_snapshot as snap
from herdr_engineering.control_plane.bridge import _rebuild_events
from herdr_engineering.control_plane.inspector import InspectorStore
from tests.control_plane._private_pg import private_postgres
from tests.control_plane.test_inspector_snapshot import LIMITS, T0, canon, stream
from tests.control_plane.test_inspector_snapshot_postgres import FABRIC

MIGRATIONS = Path(__file__).parents[2] / "deploy/control-plane/migrations"
INDEX_MIGRATION = MIGRATIONS / "0054_fabric_events_inspector_sequence_index.sql"
ARCHIVE_INDEX_MIGRATION = MIGRATIONS / "0056_fabric_events_archive_inspector_sequence_index.sql"
INSERT = ("INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
          " source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$7)")


class RecordingStore(InspectorStore):
    """A real store that remembers how many events each hydrate call received."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.batches: list[int] = []

    def hydrate(self, events):
        self.batches.append(len(events))
        return super().hydrate(events)


def make_store():
    return RecordingStore(limits=LIMITS, clock=lambda: T0 + 10_000)


def ingest_of(rows, skew=None):
    def pick(e):
        return datetime.fromisoformat(e["source_timestamp"]) if skew is None else \
            datetime.fromtimestamp(T0 + skew[e["sequence"]], UTC)
    return pick


async def insert(conn, rows, ingest):
    for e in rows:
        await conn.execute(INSERT, e.get("event_id", f"id-{e['sequence']}"), e["sequence"], e["event_type"], e["entity_id"],
                           json.dumps(e["payload"]), datetime.fromisoformat(e["source_timestamp"]), ingest(e))


ARCHIVE_SQL = "CREATE TABLE fabric_events_archive (LIKE fabric_events INCLUDING DEFAULTS, archived_at timestamptz NOT NULL DEFAULT now());"


def tied(rows, width=2):
    """The rows with ``width`` consecutive rows sharing one fabric sequence (the journal stamps it in milliseconds, so
    rows written in the same millisecond tie) and a distinct event_id each."""
    out = []
    for i, e in enumerate(rows):
        out.append({**e, "sequence": i // width + 1, "event_id": f"ev-{i}"})
    return out


def run_rebuild(tmp_path, rows, page, ingest, *, archived=0, copied=0):
    """Rebuild into a real Postgres. The first ``archived`` rows (by sequence) are moved to ``fabric_events_archive``;
    ``copied`` more rows are copied there and left in ``fabric_events`` (what the archive job's copy-then-delete leaves
    behind when it is interrupted)."""
    asyncpg = pytest.importorskip("asyncpg")
    fabric, archive = tmp_path / "fabric.sql", tmp_path / "archive.sql"
    fabric.write_text(FABRIC)
    archive.write_text(ARCHIVE_SQL)
    with private_postgres(fabric, archive, MIGRATIONS / "0030_inspector_snapshots.sql", INDEX_MIGRATION,
                          ARCHIVE_INDEX_MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def scenario():
            conn = await asyncpg.connect(dsn)
            try:
                await insert(conn, rows, ingest)
                if archived or copied:
                    await conn.execute("INSERT INTO fabric_events_archive SELECT *, now() FROM fabric_events "
                                       "WHERE sequence <= $1", archived + copied)
                    await conn.execute("DELETE FROM fabric_events WHERE sequence <= $1", archived)
                old = snap._PAGE
                snap._PAGE = page
                try:
                    writer = snap.SnapshotWriter(make_store, interval=0, settle=-10**9)
                    outcome = await writer._rebuild(conn)
                finally:
                    snap._PAGE = old
                row = await conn.fetchrow("SELECT watermark, event_count FROM inspector_snapshots WHERE id = 1")
                return writer, outcome, dict(row) if row else None
            finally:
                await conn.close()

        return asyncio.run(scenario())


def full_replay(rows, ingest):
    streams, _ = _rebuild_events([{**r, "entity_type": "inspector", "ingest_timestamp": ingest(r)} for r in rows])
    reference = make_store()
    reference.hydrate([e for s in streams.values() for e in s])
    return reference


def explain_writer_queries(tmp_path, migrations):
    """EXPLAIN the writer's own page, key-pass and lookup SQL over fabric_events UNION ALL fabric_events_archive."""
    asyncpg = pytest.importorskip("asyncpg")
    fabric, archive = tmp_path / "fabric.sql", tmp_path / "archive.sql"
    fabric.write_text(FABRIC)
    archive.write_text(ARCHIVE_SQL)
    with private_postgres(fabric, archive, *migrations) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def plans():
            conn = await asyncpg.connect(dsn)
            try:
                await insert(conn, stream(400, seed=3), ingest_of(None))
                await conn.execute("INSERT INTO fabric_events_archive SELECT *, now() FROM fabric_events WHERE sequence % 2 = 0")
                await conn.execute("DELETE FROM fabric_events WHERE sequence % 2 = 0")
                await conn.execute("ANALYZE fabric_events; ANALYZE fabric_events_archive; SET enable_seqscan = off")
                cutoff = await conn.fetchval("SELECT now() - make_interval(secs => 60.0)")
                out = {}
                for name, sql, args in (
                        ("live page", snap._page_sql(snap._COLUMNS, snap._LIVE, snap._SETTLED_BY_AGE), (-1, "", 60.0, 50)),
                        ("merged page", snap._page_sql(snap._COLUMNS, snap._WITH_ARCHIVE, snap._SETTLED_BY_AGE), (-1, "", 60.0, 50)),
                        ("key pass", snap._page_sql(snap._KEY_COLUMNS, snap._WITH_ARCHIVE, snap._SETTLED_AT), (0, "", cutoff, 50)),
                        ("lookup", snap._lookup_sql(snap._WITH_ARCHIVE), ([2, 4, 6], cutoff))):
                    lines = await conn.fetch("EXPLAIN " + sql, *args)
                    out[name] = "\n".join(r[0] for r in lines)
                return out
            finally:
                await conn.close()

        return asyncio.run(plans())


@pytest.mark.integration
def test_index_migrations_serve_the_writers_own_queries_on_both_tables(tmp_path):
    plans = explain_writer_queries(tmp_path, [INDEX_MIGRATION, ARCHIVE_INDEX_MIGRATION])
    for name, text in plans.items():
        assert "Seq Scan" not in text, f"{name}:\n{text}"
    for name in ("merged page", "key pass", "lookup"):
        assert "idx_fabric_events_archive_inspector_sequence" in plans[name], plans[name]
        assert "idx_fabric_events_inspector_sequence" in plans[name], plans[name]
    assert "idx_fabric_events_inspector_sequence" in plans["live page"]


@pytest.mark.integration
def test_without_the_archive_index_the_writers_queries_scan_the_archive(tmp_path):
    """Control for the test above: the plan check does notice a missing archive index."""
    plans = explain_writer_queries(tmp_path, [INDEX_MIGRATION])
    assert all("Seq Scan on fabric_events_archive" in plans[n] for n in ("merged page", "key pass", "lookup"))


@pytest.mark.integration
def test_index_migrations_are_idempotent_and_tolerate_a_hand_built_index(tmp_path):
    asyncpg = pytest.importorskip("asyncpg")
    fabric, manual = tmp_path / "fabric.sql", tmp_path / "manual.sql"
    fabric.write_text(FABRIC)
    manual.write_text(ARCHIVE_SQL + "CREATE INDEX idx_fabric_events_archive_inspector_sequence ON fabric_events_archive "
                      "(sequence, event_id) WHERE entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> '';")
    with private_postgres(fabric, manual, INDEX_MIGRATION, INDEX_MIGRATION,
                          ARCHIVE_INDEX_MIGRATION, ARCHIVE_INDEX_MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def names():
            conn = await asyncpg.connect(dsn)
            try:
                return {r[0] for r in await conn.fetch("SELECT indexname FROM pg_indexes WHERE indexname LIKE '%inspector_sequence'")}
            finally:
                await conn.close()

        assert asyncio.run(names()) == {"idx_fabric_events_inspector_sequence", "idx_fabric_events_archive_inspector_sequence"}


@pytest.mark.integration
def test_the_archive_index_migration_is_a_no_op_without_an_archive_table(tmp_path):
    fabric = tmp_path / "fabric.sql"
    fabric.write_text(FABRIC)
    with private_postgres(fabric, ARCHIVE_INDEX_MIGRATION) as dsn:      # must not raise
        if dsn is None:
            pytest.skip("no postgres binaries")


@pytest.mark.integration
@pytest.mark.parametrize("page", [1_000, 70, 17])
def test_paged_rebuild_equals_the_full_replay_and_never_hydrates_more_than_a_page(tmp_path, page):
    rows = stream(400, seed=11)
    ingest = ingest_of(rows)
    writer, outcome, written = run_rebuild(tmp_path, rows, page, ingest)
    assert outcome.startswith("written")
    reference = full_replay(rows, ingest)
    assert canon(writer._shadow.export_state()) == canon(reference.export_state())
    assert written == {"watermark": reference.replay_watermark, "event_count": reference.replay_count}
    if page < len(rows):
        assert max(writer._shadow.batches) <= page and len(writer._shadow.batches) > 1


@pytest.mark.integration
def test_rebuild_with_rows_out_of_replay_order_across_pages_still_equals_the_full_replay(tmp_path):
    """Rows whose replay order (no _received_at, so ingest_timestamp) does not follow sequence defeat page-wise
    hydration; the rebuild must fall back to one full hydrate and still match."""
    rows = stream(300, seed=5)
    for e in rows:
        e["payload"].pop("_received_at", None)
    rnd = random.Random(1)
    skew = {e["sequence"]: rnd.randint(0, 3000) for e in rows}
    ingest = ingest_of(rows, skew)
    writer, outcome, _ = run_rebuild(tmp_path, rows, 40, ingest)
    assert outcome.startswith("written")
    reference = full_replay(rows, ingest)
    assert canon(writer._shadow.export_state()) == canon(reference.export_state())


class Clock:
    """A strictly increasing fake clock for the live store that writes the journal."""

    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += 0.01
        return self.now


async def _insert_journal(conn, journal, start):
    for i, ev in enumerate(journal[start:], start=start):
        stamp = datetime.fromtimestamp(ev["payload"].get("_received_at", T0), UTC)
        await conn.execute(INSERT, f"jid-{i}", i + 1, ev["event_type"], ev["entity_id"], json.dumps(ev["payload"]),
                           datetime.fromisoformat(ev["source_timestamp"]), stamp)


def _tool(call, phase, **extra):
    return {"events": [{"kind": "tool", "call_id": call, "tool": "bash", "phase": phase, **extra}]}


def _late_update_journal(trigger):
    """A long-running tool whose last update is held back while other sessions keep journaling; ``trigger`` releases it."""
    journal: list[dict] = []
    live = InspectorStore(limits=LIMITS, persist=journal.append, clock=Clock())
    live.ingest("sA", _tool("c1", "start", args_preview="make"))
    live.ingest("sA", _tool("c1", "update", output="partial output"))
    for i in range(30):
        live.ingest("sB", {"events": [{"kind": "message", "role": "user", "text": f"hello {i}"}]})
    cut = len(journal)
    trigger(live)
    return journal, cut


@pytest.mark.integration
@pytest.mark.parametrize("trigger", [
    lambda live: live.ingest("sA", _tool("c1", "end")),                          # end row that does not carry the output
    lambda live: live.ingest("sA", {"events": [{"kind": "lifecycle", "phase": "interrupted"}]}),
    lambda live: [live.ingest("sA", {"events": [{"kind": "message", "role": "user", "text": f"m{i}"}]}) for i in range(30)],
], ids=["end", "lifecycle", "ring-eviction"])
def test_a_deferred_tool_update_written_after_the_cursor_does_not_force_a_rebuild(tmp_path, caplog, trigger):
    asyncpg = pytest.importorskip("asyncpg")
    journal, cut = _late_update_journal(trigger)
    assert len(journal) > cut + 1                       # the held update was journaled with the row that released it
    fabric = tmp_path / "fabric.sql"
    fabric.write_text(FABRIC)
    with private_postgres(fabric, MIGRATIONS / "0030_inspector_snapshots.sql", INDEX_MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def scenario():
            conn = await asyncpg.connect(dsn)
            try:
                await _insert_journal(conn, journal[:cut], 0)
                writer = snap.SnapshotWriter(make_store, interval=0, settle=-10**9)
                await writer.tick(conn)
                shadow = writer._shadow
                await _insert_journal(conn, journal, cut)
                with caplog.at_level("WARNING"):
                    await writer.tick(conn)
                return writer, shadow
            finally:
                await conn.close()

        writer, first_shadow = asyncio.run(scenario())
    assert "restarts from the full log" not in caplog.text and writer._shadow is first_shadow
    full = make_store()
    full.hydrate([{**e, "sequence": i + 1, "ingest_timestamp": None} for i, e in enumerate(journal)])
    assert canon(writer._shadow.export_state()) == canon(full.export_state())


@pytest.mark.integration
@pytest.mark.parametrize("page", [17, 1_000])
def test_rebuild_keeps_rows_that_share_a_sequence_across_page_boundaries(tmp_path, page):
    rows = tied(stream(300, seed=2))
    ingest = ingest_of(rows)
    writer, outcome, _ = run_rebuild(tmp_path, rows, page, ingest)
    assert outcome.startswith("written")
    assert canon(writer._shadow.export_state()) == canon(full_replay(rows, ingest).export_state())


@pytest.mark.integration
def test_in_order_replay_feeds_every_row_that_shares_a_sequence_exactly_once(tmp_path):
    rows = tied(stream(300, seed=5), width=3)
    for e in rows:
        e["payload"].pop("_received_at", None)
    rnd = random.Random(1)
    skew = {i: rnd.randint(0, 3000) for i in range(len(rows))}
    ingest_by_id = {e["event_id"]: datetime.fromtimestamp(T0 + skew[i], UTC) for i, e in enumerate(rows)}
    ingest = lambda e: ingest_by_id[e["event_id"]]      # noqa: E731
    writer, outcome, _ = run_rebuild(tmp_path, rows, 40, ingest)
    assert outcome.startswith("written")
    assert canon(writer._shadow.export_state()) == canon(full_replay(rows, ingest).export_state())


@pytest.mark.integration
@pytest.mark.parametrize("archived", [60, 400])
def test_rebuild_with_archived_inspector_rows_replays_them_and_writes_the_snapshot(tmp_path, archived):
    """The archive job moves inspector rows out of fabric_events but the snapshot's event_count still counts them, so a
    rebuild that replays only the live rows can never match and the snapshot stays unwritten for good."""
    asyncpg = pytest.importorskip("asyncpg")
    rows = stream(400, seed=9)
    ingest = ingest_of(rows)
    fabric, archive = tmp_path / "fabric.sql", tmp_path / "archive.sql"
    fabric.write_text(FABRIC)
    archive.write_text(ARCHIVE_SQL)
    with private_postgres(fabric, archive, MIGRATIONS / "0030_inspector_snapshots.sql", INDEX_MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def scenario():
            conn = await asyncpg.connect(dsn)
            try:
                await insert(conn, rows, ingest)
                await conn.execute("INSERT INTO fabric_events_archive SELECT *, now() FROM fabric_events "
                                   "WHERE sequence <= $1", archived)
                await conn.execute("DELETE FROM fabric_events WHERE sequence <= $1", archived)
                writer = snap.SnapshotWriter(make_store, interval=0, settle=-10**9)
                outcome = await writer.tick(conn)
                row = await conn.fetchrow("SELECT watermark, event_count FROM inspector_snapshots WHERE id = 1")
                return writer, outcome, dict(row) if row else None
            finally:
                await conn.close()

        writer, outcome, written = asyncio.run(scenario())
    assert outcome.startswith("written"), outcome
    reference = full_replay(rows, ingest)
    assert canon(writer._shadow.export_state()) == canon(reference.export_state())
    assert written == {"watermark": reference.replay_watermark, "event_count": reference.replay_count}


@pytest.mark.integration
@pytest.mark.parametrize("page", [17, 1_000])
def test_rebuild_with_archived_rows_pages_correctly(tmp_path, page):
    rows = stream(400, seed=9)
    ingest = ingest_of(rows)
    writer, outcome, written = run_rebuild(tmp_path, rows, page, ingest, archived=150)
    assert outcome.startswith("written"), outcome
    reference = full_replay(rows, ingest)
    assert canon(writer._shadow.export_state()) == canon(reference.export_state())
    assert written == {"watermark": reference.replay_watermark, "event_count": reference.replay_count}


@pytest.mark.integration
@pytest.mark.parametrize("width", [1, 3])
def test_in_order_replay_reads_archived_rows_with_skewed_timestamps_and_tied_sequences(tmp_path, width):
    rows = tied(stream(300, seed=5), width=width)
    for e in rows:
        e["payload"].pop("_received_at", None)
    rnd = random.Random(7)
    ingest_by_id = {e["event_id"]: datetime.fromtimestamp(T0 + rnd.randint(0, 3000), UTC) for e in rows}
    ingest = lambda e: ingest_by_id[e["event_id"]]      # noqa: E731
    writer, outcome, written = run_rebuild(tmp_path, rows, 23, ingest, archived=100)
    assert outcome.startswith("written"), outcome
    assert max(writer._shadow.batches) <= 23 and len(writer._shadow.batches) > 1
    reference = full_replay(rows, ingest)
    assert canon(writer._shadow.export_state()) == canon(reference.export_state())
    assert written == {"watermark": reference.replay_watermark, "event_count": reference.replay_count}


@pytest.mark.integration
@pytest.mark.parametrize("skewed", [False, True], ids=["streamed", "in-order"])
def test_rows_present_in_both_tables_are_replayed_and_counted_once(tmp_path, skewed):
    """The archive job copies a row before it deletes it, so an interrupted run leaves it in both tables."""
    rows = tied(stream(300, seed=5), width=2)
    ingest = ingest_of(rows)
    if skewed:
        for e in rows:
            e["payload"].pop("_received_at", None)
        rnd = random.Random(3)
        ingest_by_id = {e["event_id"]: datetime.fromtimestamp(T0 + rnd.randint(0, 3000), UTC) for e in rows}
        ingest = lambda e: ingest_by_id[e["event_id"]]      # noqa: E731
    writer, outcome, written = run_rebuild(tmp_path, rows, 31, ingest, archived=60, copied=40)
    assert outcome.startswith("written"), outcome
    reference = full_replay(rows, ingest)
    assert canon(writer._shadow.export_state()) == canon(reference.export_state())
    assert written == {"watermark": reference.replay_watermark, "event_count": reference.replay_count}


def test_order_keys_are_held_compactly_and_come_back_in_key_order():
    rnd = random.Random(4)
    keys = [(rnd.choice([1.5, 2.5, 3.5]), float(rnd.randint(0, 9)), i // 3, i % 3) for i in range(5_000)]
    held = snap._OrderKeys(run_size=700)
    for k in rnd.sample(keys, len(keys)):
        held.add(*k)
    assert list(held.merged()) == sorted(keys)
    assert held.nbytes <= 32 * len(keys)        # the previous tuple + event_id string per row was about 200 bytes
