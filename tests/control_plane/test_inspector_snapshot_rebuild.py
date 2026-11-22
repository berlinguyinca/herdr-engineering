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
        await conn.execute(INSERT, f"id-{e['sequence']}", e["sequence"], e["event_type"], e["entity_id"],
                           json.dumps(e["payload"]), datetime.fromisoformat(e["source_timestamp"]), ingest(e))


def run_rebuild(tmp_path, rows, page, ingest):
    asyncpg = pytest.importorskip("asyncpg")
    fabric = tmp_path / "fabric.sql"
    fabric.write_text(FABRIC)
    with private_postgres(fabric, MIGRATIONS / "0030_inspector_snapshots.sql", INDEX_MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def scenario():
            conn = await asyncpg.connect(dsn)
            try:
                await insert(conn, rows, ingest)
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


@pytest.mark.integration
def test_index_migration_is_idempotent_serves_the_page_query_and_tolerates_a_hand_built_index(tmp_path):
    asyncpg = pytest.importorskip("asyncpg")
    fabric, manual = tmp_path / "fabric.sql", tmp_path / "manual.sql"
    fabric.write_text(FABRIC)
    manual.write_text("CREATE INDEX idx_fabric_events_inspector_sequence ON fabric_events (sequence) "
                      "WHERE entity_type = 'inspector';")
    with private_postgres(fabric, manual, INDEX_MIGRATION, INDEX_MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")

        async def plan():
            conn = await asyncpg.connect(dsn)
            try:
                await insert(conn, stream(200, seed=3), ingest_of(None))
                await conn.execute("ANALYZE fabric_events")
                await conn.execute("SET enable_seqscan = off")
                lines = await conn.fetch(
                    f"EXPLAIN SELECT {snap._COLUMNS} FROM fabric_events WHERE {snap._VALID} AND sequence > $1 "
                    "AND ingest_timestamp < now() - make_interval(secs => $2) ORDER BY sequence LIMIT $3",
                    -1, 60.0, 50)
                return "\n".join(r[0] for r in lines)
            finally:
                await conn.close()

        text = asyncio.run(plan())
    assert "idx_fabric_events_inspector_sequence" in text and "Seq Scan on fabric_events" not in text


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
