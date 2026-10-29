"""The snapshot writer's SQL and the 0030 migration against a real, throwaway Postgres."""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from herdr_engineering.control_plane import inspector_snapshot as snap
from herdr_engineering.control_plane.bridge import _rebuild_events
from tests.control_plane._private_pg import private_postgres
from tests.control_plane.test_inspector_snapshot import LIMITS, T0, canon, store, stream

MIGRATION = Path(__file__).parents[2] / "deploy/control-plane/migrations/0030_inspector_snapshots.sql"
FABRIC = """CREATE TABLE missions (mission_id text); CREATE TABLE sessions (session_id text);
CREATE TABLE services (service_id text); CREATE TABLE hosts (host_id text);
CREATE TABLE fabric_events (
  event_id text NOT NULL, sequence bigint NOT NULL, event_type text NOT NULL, entity_type text, entity_id text,
  mission_id text, session_id text, payload jsonb, source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(), schema_version integer NOT NULL DEFAULT 1,
  source text, source_sequence bigint);"""


def test_migration_is_idempotent_and_down_drops_only_its_table():
    text = MIGRATION.read_text()
    assert text.count("CREATE TABLE IF NOT EXISTS") == 1 and "DROP TABLE IF EXISTS inspector_snapshots;" in text
    assert "fabric_events" not in text.split("-- DOWN:")[1].split("CREATE TABLE")[0]


@pytest.mark.integration
def test_writer_and_boot_against_a_real_postgres(tmp_path):
    asyncpg = pytest.importorskip("asyncpg")
    fabric = tmp_path / "fabric.sql"
    fabric.write_text(FABRIC)
    with private_postgres(fabric, MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")
        rows = stream(400, seed=21)

        async def scenario():
            conn = await asyncpg.connect(dsn)
            try:
                for e in rows[:300]:
                    await conn.execute(
                        "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
                        " source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)",
                        f"id-{e['sequence']}", e["sequence"], e["event_type"], e["entity_id"], json.dumps(e["payload"]),
                        datetime.fromisoformat(e["source_timestamp"]))
                writer = snap.SnapshotWriter(store, interval=0, settle=-10**9)     # everything counts as settled
                first = await writer.tick(conn)
                for e in rows[300:]:
                    await conn.execute(
                        "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
                        " source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)",
                        f"id-{e['sequence']}", e["sequence"], e["event_type"], e["entity_id"], json.dumps(e["payload"]),
                        datetime.fromisoformat(e["source_timestamp"]))
                second = await writer.tick(conn)
                snapshot = dict(await conn.fetchrow("SELECT schema_version, fingerprint, watermark, event_count, body FROM inspector_snapshots WHERE id = 1"))
                stored = [dict(r) for r in await conn.fetch("SELECT * FROM fabric_events")]
                return first, second, snapshot, stored
            finally:
                await conn.close()

        first, second, snapshot, stored = asyncio.run(scenario())
    assert first.startswith("written") and second.startswith("written")
    streams, _ = _rebuild_events([dict(r) for r in stored])
    events = [e for s in streams.values() for e in s]
    booted, full = store(), store()
    assert snap.apply_to_store(booted, snapshot, events) == "snapshot"
    full.hydrate(events)
    assert canon(booted.export_state()) == canon(full.export_state())
    assert snapshot["event_count"] == len(rows)


@pytest.mark.integration
def test_the_real_source_skips_covered_rows_and_boots_to_the_same_state(tmp_path, monkeypatch):
    """PostgresSource + load_into on a real Postgres: fewer rows loaded, identical inspector, and a refused snapshot
    still ends identical because the skipped rows are fetched on demand."""
    asyncpg = pytest.importorskip("asyncpg")
    from herdr_engineering.control_plane import inspector as insp
    from herdr_engineering.control_plane.bridge import PostgresSource, load_into
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    fabric = tmp_path / "fabric.sql"
    fabric.write_text(FABRIC)
    with private_postgres(fabric, MIGRATION) as dsn:
        if dsn is None:
            pytest.skip("no postgres binaries")
        rows = stream(500, seed=41)

        async def seed():
            conn = await asyncpg.connect(dsn)
            try:
                for e in rows:
                    await conn.execute(
                        "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
                        " source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)",
                        f"id-{e['sequence']}", e["sequence"], e["event_type"], e["entity_id"], json.dumps(e["payload"]),
                        datetime.fromisoformat(e["source_timestamp"]))
                    if e["sequence"] == rows[400]["sequence"]:
                        await snap.SnapshotWriter(store, interval=0, settle=-10**9).tick(conn)   # snapshot covers 0..400
            finally:
                await conn.close()

        asyncio.run(seed())

        def boot(snapshots):
            repo = ControlPlaneRepo(inspector_limits=LIMITS)
            repo.inspector._clock = lambda: T0 + 10_000
            source = PostgresSource(dsn=dsn, inspector_snapshots=snapshots)
            result = load_into(repo, source)
            return result["events"], canon(repo.inspector.export_state())

        loaded_all, full = boot(False)
        loaded_less, fast = boot(True)
        assert loaded_less < loaded_all and fast == full
        monkeypatch.setattr(insp, "rules_fingerprint", lambda: "a different rule set")
        events_loaded, refused = boot(True)
        assert events_loaded == loaded_less                      # the rows were skipped at load, then fetched back
        a, b = json.loads(refused), json.loads(full)
        assert a["sessions"] == b["sessions"] and a["watermark"] == b["watermark"] and a["count"] == b["count"]
