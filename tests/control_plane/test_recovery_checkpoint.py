"""Exact-set recovery checkpoints, using only a disposable PostgreSQL cluster."""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.bridge import PostgresSource, _v2_value, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_postgres_recovery import (
    _insert,
    _projections,
    _reference,
    _row,
    _snapshot,
)
from tests.e2e.pg import ThrowawayPostgres, find_bins

asyncpg = pytest.importorskip("asyncpg")


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    bins = find_bins({})
    if bins is None:
        pytest.skip("PostgreSQL server binaries are required")
    database = ThrowawayPostgres(bins, tmp_path_factory.mktemp("checkpoint-pg"))
    try:
        # Only this disposable fixture skips initdb's filesystem sync. The
        # existing harness already runs throwaway servers with fsync=off.
        subprocess.run([str(bins / "initdb"), "-D", str(database.data), "-U", "postgres",
                        "--auth=trust", "-E", "UTF8", "--no-sync"], check=True, capture_output=True)
        database.start_existing()
        subprocess.run([str(bins / "createdb"), "-h", str(database.sock), "-U", "postgres", database.db],
                       check=True, capture_output=True)
        asyncio.run(database._migrate())
        yield database
    finally:
        database.close()


@pytest.fixture
def db(pg):
    pg.query("TRUNCATE fabric_events")
    # The first regression also runs before checkpoint tables have been added.
    if pg.query("SELECT to_regclass('recovery_checkpoint_manifests') AS name")[0]["name"]:
        pg.query("TRUNCATE recovery_checkpoint_manifests CASCADE")
    return pg


def _build(db):
    from herdr_engineering.control_plane.recovery_checkpoint import build_checkpoint

    async def go():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await build_checkpoint(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def _checkpoint_source(db):
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    source = PostgresSource(conn, run=loop.run_until_complete)
    try:
        repo = ControlPlaneRepo(now=lambda: 1793059200)
        repo.inspector._clock = lambda: 1793059200
        load_into(repo, source)
        repo._durable_history = None  # Returned value outlives this helper's connection.
        return repo, source.events(), source.recovery_metrics
    finally:
        loop.run_until_complete(conn.close())
        loop.close()


def test_checkpoint_keeps_original_rows_and_exact_coverage(db):
    rows = [_row("service", "svc", "ServiceUpdated", _snapshot(), n) for n in range(100)]
    rows.extend(_row("ci_fix", "fix", "Partial", {"field": n}, n) for n in range(3))
    _insert(db, rows)
    expected, originals = _reference(db)
    report = _build(db)
    assert report["covered_events"] == 103
    assert report["selected_events"] == 5
    assert set(report) == {"mode", "checkpoint_id", "format_version", "covered_events", "selected_events", "elapsed_seconds"}
    assert report["mode"] == "built" and report["elapsed_seconds"] >= 0
    recovered, selected, metrics = _checkpoint_source(db)
    assert metrics["mode"] == "checkpoint" and metrics["tail_events"] == 0
    assert _projections(recovered) == _projections(expected)
    assert recovered.stream("ci_fix", "fix") == expected.stream("ci_fix", "fix")
    original_by_id = {r["event_id"]: r for r in originals}
    assert all(row == original_by_id[row["event_id"]] for row in selected)
    assert db.query("SELECT count(*) AS n FROM fabric_events")[0]["n"] == 103


def test_inflight_backdated_and_cross_actor_tail_are_not_lost(db):
    from herdr_engineering.control_plane.recovery_checkpoint import build_checkpoint

    first = _row("session", "s", "session.updated", {"_envelope": {"content_sha256": "a"}},
                 100, source="reporter", source_sequence=5, actor={"id": "a"})
    _insert(db, [first, _row("session", "s", "SessionStarted", {"status": "active"}, 101)])

    async def exercise():
        writer, builder = await asyncpg.connect(db.dsn), await asyncpg.connect(db.dsn)
        try:
            async with writer.transaction():
                # This transaction starts before the checkpoint, but its event is
                # invisible to the builder until AFTER publication. Both clocks
                # and the stream sequence precede already-covered observations.
                await writer.execute("""
                    INSERT INTO fabric_events(event_id, entity_type, entity_id, event_type, sequence,
                        source_timestamp, ingest_timestamp, payload, source, source_sequence, actor)
                    VALUES($1, 'session', 's', 'session.updated', 0, $2, $2, $3::jsonb,
                           'reporter', 0, '{"id":"b"}')
                """, first["event_id"], first["source_timestamp"] - timedelta(days=100),
                    json.dumps({"_envelope": {"content_sha256": "b"}}))
                report = await build_checkpoint(builder)
                assert report["covered_events"] == 2
            return report
        finally:
            await writer.close()
            await builder.close()
    report = asyncio.run(exercise())
    expected, _ = _reference(db)
    recovered, selected, metrics = _checkpoint_source(db)
    assert metrics["checkpoint_id"] == report["checkpoint_id"]
    assert metrics["tail_events"] == 1 and len(selected) == 3
    assert _projections(recovered) == _projections(expected)
    assert recovered.ledger._events == expected.ledger._events
    assert {_v2_value("actor", e["actor"])["id"] for e in selected if e["event_id"] == first["event_id"]} == {"a", "b"}
    for actor in ("a", "b"):
        assert recovered.ledger.check(dict(event_id=first["event_id"], source="reporter", source_sequence=5 if actor == "a" else 0,
                                           actor={"id": actor}, content_hash=actor)) == "duplicate"


def test_pointer_publication_is_atomic_and_failure_preserves_previous_generation(db):
    from herdr_engineering.control_plane.recovery_checkpoint import (
        build_checkpoint,
        load_checkpoint_events,
    )

    _insert(db, [_row("mission", "m", "MissionStarted", {"title": "original"}, 0)])
    first = _build(db)
    _insert(db, [_row("session", "s", "SessionStarted", {"mission_id": "m", "status": "stopped"}, 1)])

    async def exercise():
        conn, reader = await asyncpg.connect(db.dsn), await asyncpg.connect(db.dsn)
        before_publish, resume = asyncio.Event(), asyncio.Event()

        class PausedPublisher:
            def __getattr__(self, name):
                return getattr(conn, name)

            async def execute(self, sql, *args):
                if "INSERT INTO recovery_checkpoint_active" in sql:
                    before_publish.set()
                    await resume.wait()
                    raise RuntimeError("injected publication failure")
                return await conn.execute(sql, *args)

        task = asyncio.create_task(build_checkpoint(PausedPublisher()))
        try:
            await asyncio.wait_for(before_publish.wait(), 20)
            async with reader.transaction(isolation="repeatable_read", readonly=True):
                rows, metrics = await load_checkpoint_events(reader)
                assert metrics["checkpoint_id"] == first["checkpoint_id"]
                assert metrics["tail_events"] == 1 and len(rows) == 2
                assert await reader.fetchval("SELECT count(*) FROM recovery_checkpoint_manifests") == 1
            resume.set()
            with pytest.raises(RuntimeError, match="publication failure"):
                await task
            assert await reader.fetchval("SELECT count(*) FROM recovery_checkpoint_manifests") == 1
            assert await reader.fetchval("SELECT checkpoint_id FROM recovery_checkpoint_active") == first["checkpoint_id"]
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await conn.close()
            await reader.close()
    asyncio.run(exercise())
    second = _build(db)
    assert second["checkpoint_id"] != first["checkpoint_id"]
    _, _, metrics = _checkpoint_source(db)
    assert metrics["checkpoint_id"] == second["checkpoint_id"] and metrics["tail_events"] == 0


def test_absent_or_incompatible_checkpoint_falls_back_faithfully(db):
    _insert(db, [_row("session", "s", "SessionStarted", {"status": "stopped"}, 0)])
    repo, rows, metrics = _checkpoint_source(db)
    assert metrics["mode"] == "full" and len(rows) == 1
    assert repo.get_session("s")["status"] == "stopped"
    report = _build(db)
    db.query("UPDATE recovery_checkpoint_manifests SET format_version = 999 WHERE checkpoint_id=$1", report["checkpoint_id"])
    _, again, metrics = _checkpoint_source(db)
    assert metrics["mode"] == "full" and again == rows


def test_retention_does_not_restore_expired_events(db):
    from herdr_engineering.control_plane.sink import DurableEventSink

    old = _row("token_usage", "s", "TokenUsage", {"tokens": 7}, 0)
    fresh = _row("token_usage", "s", "TokenUsage", {"tokens": 9}, 100)
    _insert(db, [old, fresh])
    _build(db)
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    try:
        DurableEventSink(conn=conn, run=loop.run_until_complete).prune_events_before(
            "token_usage", fresh["source_timestamp"])
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    _, selected, metrics = _checkpoint_source(db)
    assert [row["event_id"] for row in selected] == [fresh["event_id"]]
    assert metrics["mode"] == "checkpoint"


def test_quarantine_does_not_restore_forged_events(db):
    from herdr_engineering.control_plane.collab_repair import repair_postgres

    _insert(db, [_row("collab", "foreign", "member_added", {"actor_user_id": "forged"}, 0)])
    _build(db)

    async def repair():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await repair_postgres(conn, apply=True, confirmed=True)
        finally:
            await conn.close()
    report = asyncio.run(repair())
    assert len(report["quarantine"]) == 1
    _, selected, metrics = _checkpoint_source(db)
    assert selected == [] and metrics["mode"] == "checkpoint"


def test_deleted_snapshot_boundary_replays_remaining_original_stream(db):
    rows = [_row("service", "svc", "ServiceUpdated", _snapshot(), n) for n in range(8)]
    _insert(db, rows)
    _build(db)
    db.query("DELETE FROM fabric_events WHERE event_id=$1", rows[-1]["event_id"])
    expected, originals = _reference(db)
    recovered, selected, _ = _checkpoint_source(db)
    assert _projections(recovered) == _projections(expected)
    assert {row["event_id"] for row in selected} == {row["event_id"] for row in originals}


def test_backdated_barrier_replays_interior_snapshots(db):
    rows = [_row("service", "svc", "ServiceUpdated", _snapshot(), n * 10) for n in range(8)]
    _insert(db, rows)
    _build(db)
    _insert(db, [_row("service", "svc", "ServiceUnavailable", {"status": "unavailable"}, 35)])
    expected, originals = _reference(db)
    recovered, selected, _ = _checkpoint_source(db)
    assert _projections(recovered) == _projections(expected)
    assert {row["event_id"] for row in selected} == {row["event_id"] for row in originals}


def test_operator_builder_uses_private_config_and_prints_only_metrics(db, monkeypatch, capsys):
    from types import SimpleNamespace

    from herdr_engineering import fleet_config
    from herdr_engineering.control_plane import recovery_checkpoint

    _insert(db, [_row("mission", "m", "MissionStarted", {"title": "private title"}, 0)])
    monkeypatch.setattr(fleet_config, "load_fleet_config", lambda path: SimpleNamespace(
        section=lambda name: {"pg_dsn": db.dsn}))
    assert recovery_checkpoint.main(["--fleet-config", "/private/config.yaml"]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["mode"] == "built" and report["covered_events"] == 1
    assert db.dsn not in output.out and "private title" not in output.out
    assert not output.err


def test_operator_builder_does_not_print_exception_secrets(monkeypatch, capsys):
    from herdr_engineering import fleet_config
    from herdr_engineering.control_plane import recovery_checkpoint

    def fail(path):
        raise ValueError("postgres://private-credentials")

    monkeypatch.setattr(fleet_config, "load_fleet_config", fail)
    assert recovery_checkpoint.main(["--fleet-config", "/private/config.yaml"]) == 1
    output = capsys.readouterr()
    assert "private-credentials" not in output.err + output.out
    assert "ValueError" in output.err


def test_commit_during_build_is_uncovered_tail(db):
    from herdr_engineering.control_plane.recovery_checkpoint import build_checkpoint

    _insert(db, [_row("mission", "m", "MissionStarted", {"title": "first"}, 10)])

    async def exercise():
        builder, writer = await asyncpg.connect(db.dsn), await asyncpg.connect(db.dsn)

        class ConcurrentWriter:
            def __getattr__(self, name):
                return getattr(builder, name)

            async def execute(self, sql, *args):
                if "INSERT INTO recovery_checkpoint_rows" in sql:
                    await writer.execute("""
                        INSERT INTO fabric_events (event_id, entity_type, entity_id, event_type,
                            sequence, source_timestamp, ingest_timestamp, payload)
                        VALUES ('during-build', 'mission', 'm', 'MissionStarted', 5,
                                '2026-10-26T00:00:05Z', '2026-10-26T00:00:05Z', '{"title":"late"}')
                    """)
                return await builder.execute(sql, *args)
        try:
            return await build_checkpoint(ConcurrentWriter())
        finally:
            await builder.close()
            await writer.close()
    report = asyncio.run(exercise())
    assert report["covered_events"] == report["selected_events"] == 1
    expected, originals = _reference(db)
    recovered, selected, metrics = _checkpoint_source(db)
    assert metrics["tail_events"] == 1
    assert _projections(recovered) == _projections(expected)
    assert selected == originals


def test_existing_reader_keeps_old_generation_during_successful_publication(db):
    from herdr_engineering.control_plane.recovery_checkpoint import (
        build_checkpoint,
        load_checkpoint_events,
    )

    _insert(db, [_row("mission", "m", "MissionStarted", {"title": "first"}, 0)])
    first = _build(db)

    async def exercise():
        reader, writer = await asyncpg.connect(db.dsn), await asyncpg.connect(db.dsn)
        try:
            async with reader.transaction(isolation="repeatable_read", readonly=True):
                before, metrics = await load_checkpoint_events(reader)
                assert len(before) == 1 and metrics["checkpoint_id"] == first["checkpoint_id"]
                await writer.execute("""
                    INSERT INTO fabric_events (event_id, entity_type, entity_id, event_type,
                        sequence, source_timestamp, ingest_timestamp, payload)
                    VALUES ('after-reader', 'mission', 'm', 'MissionStarted', 1, now(), now(), '{"title":"second"}')
                """)
                second = await build_checkpoint(writer)
                after, same_metrics = await load_checkpoint_events(reader)
                assert after == before and same_metrics["checkpoint_id"] == first["checkpoint_id"]
            async with reader.transaction(isolation="repeatable_read", readonly=True):
                latest, metrics = await load_checkpoint_events(reader)
                assert len(latest) == 2 and metrics["checkpoint_id"] == second["checkpoint_id"]
        finally:
            await reader.close()
            await writer.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("legacy", [False, True])
def test_reinserted_identity_uses_new_journal_content_and_ledger(db, legacy):
    row = _row("session", "s", "session.updated", {"_envelope": {"content_sha256": "old"}},
               0, source="reporter", source_sequence=1, actor={"id": "a"})
    if legacy:
        db.query("ALTER TABLE fabric_events DISABLE TRIGGER fabric_event_recovery_instance")
    try:
        _insert(db, [row])
    finally:
        if legacy:
            db.query("ALTER TABLE fabric_events ENABLE TRIGGER fabric_event_recovery_instance")
    initial = db.query("SELECT recovery_instance FROM fabric_events")[0]["recovery_instance"]
    assert (initial is None) == legacy
    _build(db)
    db.query("DELETE FROM fabric_events WHERE event_id=$1", row["event_id"])
    row.update(payload={"_envelope": {"content_sha256": "new"}}, source_sequence=2)
    _insert(db, [row])
    assert db.query("SELECT recovery_instance FROM fabric_events")[0]["recovery_instance"] != initial
    expected, originals = _reference(db)
    recovered, selected, metrics = _checkpoint_source(db)
    assert selected == originals
    assert recovered.ledger._events == expected.ledger._events
    assert metrics["tail_events"] == 1


def test_updated_compacted_interior_is_replayed_from_current_journal(db):
    rows = [_row("service", "svc", "ServiceUpdated", _snapshot(), n) for n in range(8)]
    _insert(db, rows)
    _build(db)
    db.query("UPDATE fabric_events SET payload = payload || '{\"status\":\"unavailable\"}'::jsonb "
             "WHERE event_id=$1", rows[3]["event_id"])
    expected, originals = _reference(db)
    recovered, selected, _ = _checkpoint_source(db)
    assert selected == originals
    assert _projections(recovered) == _projections(expected)


def test_repair_upgrades_existing_archive_with_original_column_order(db):
    from herdr_engineering.control_plane.collab_repair import repair_postgres

    db.query("DROP TABLE IF EXISTS fabric_events_quarantine")
    db.query("CREATE TABLE fabric_events_quarantine (LIKE fabric_events)")
    db.query("ALTER TABLE fabric_events_quarantine DROP COLUMN recovery_instance")
    db.query("ALTER TABLE fabric_events_quarantine ADD COLUMN quarantine_reason text, "
             "ADD COLUMN quarantined_at timestamptz DEFAULT now()")
    _insert(db, [_row("collab", "foreign", "member_added", {"actor_user_id": "forged"}, 0)])
    original = db.query("SELECT event_id, recovery_instance FROM fabric_events")[0]

    async def repair():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await repair_postgres(conn, apply=True, confirmed=True)
        finally:
            await conn.close()
    report = asyncio.run(repair())
    assert len(report["quarantine"]) == 1
    archived = db.query("SELECT event_id, recovery_instance, quarantine_reason, quarantined_at "
                        "FROM fabric_events_quarantine")[0]
    assert archived["event_id"] == original["event_id"]
    assert archived["recovery_instance"] == original["recovery_instance"]
    assert archived["quarantine_reason"] == "foreign_stream" and archived["quarantined_at"] is not None
    assert db.query("SELECT count(*) AS n FROM fabric_events")[0]["n"] == 0


def test_checkpoint_boot_keeps_the_inspector_snapshot(db):
    """A checkpoint boot carries every inspector row, so nothing is skipped, but the verified snapshot must still
    replace the replay of the rows below its watermark (the replay was ~8 minutes in production)."""
    from datetime import datetime

    from herdr_engineering.control_plane import inspector_snapshot as snap
    from tests.control_plane.test_inspector_snapshot import LIMITS, T0, canon, stream

    def store():
        # Built the way the worker builds it, so the fingerprint (limits, roles, redaction) matches the booting repo.
        shadow = ControlPlaneRepo(inspector_limits=LIMITS).inspector
        shadow._clock = lambda: T0 + 10_000
        return shadow

    rows = stream(400, seed=21)

    async def seed():
        conn = await asyncpg.connect(db.dsn)
        try:
            for index, e in enumerate(rows):
                await conn.execute(
                    "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
                    " source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)",
                    f"id-{e['sequence']}", e["sequence"], e["event_type"], e["entity_id"], json.dumps(e["payload"]),
                    datetime.fromisoformat(e["source_timestamp"]))
                if index == 299:
                    await snap.SnapshotWriter(store, interval=0, settle=-10**9).tick(conn)
        finally:
            await conn.close()

    asyncio.run(seed())
    _build(db)

    def boot(snapshots):
        repo = ControlPlaneRepo(inspector_limits=LIMITS)
        repo.inspector._clock = lambda: T0 + 10_000
        source = PostgresSource(dsn=db.dsn, inspector_snapshots=snapshots)
        load_into(repo, source)
        assert source.recovery_metrics["mode"] != "full"
        return canon(repo.inspector.export_state())

    full, fast = boot(False), boot(True)
    assert fast == full
    seen = []
    real = snap.apply_to_store
    snap.apply_to_store = lambda *a, **k: (seen.append(real(*a, **k)) or seen[-1])
    try:
        boot(True)
    finally:
        snap.apply_to_store = real
    assert seen == ["snapshot"]
