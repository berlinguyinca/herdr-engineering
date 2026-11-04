"""Service-observation compaction against a disposable PostgreSQL: what goes, what stays, and that a replay agrees."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.fabric_compaction import CompactionSettings, FabricCompactor
from tests.control_plane import test_recovery_checkpoint as checkpoint_tests
from tests.control_plane.test_postgres_recovery import (
    _insert,
    _projections,
    _reference,
    _row,
    _snapshot,
)


asyncpg = pytest.importorskip("asyncpg")

# The disposable-database fixtures live with the checkpoint tests; re-export them for this module.
pg, db = checkpoint_tests.pg, checkpoint_tests.db
_build, _checkpoint_source = checkpoint_tests._build, checkpoint_tests._checkpoint_source

SVC = "svc-1"


def _listener(n, status="healthy", **extra):
    return _row("service", SVC, "ServiceListenerObserved", {
        "service_id": SVC, "host_id": "h", "source_port": 8000, "port": 8000, "status": status,
        "discovery_source": "listener", "endpoint_scope": "local", "created_at": "2026-10-26T00:00:00+00:00",
        "updated_at": f"2026-10-26T00:00:{n:02d}+00:00", "ownership": None, "session_id": None,
        "verified_origin": None, "listener_identity": None, "metadata_observation": None,
        "metadata_updated_at": None}, n, **extra)


def _updated(n, status):
    return _row("service", SVC, "ServiceUpdated", {**_snapshot(), "status": status}, n)


def _seed(db):
    db.query("TRUNCATE fabric_events_archive")
    db.query("TRUNCATE recovery_checkpoint_manifests CASCADE")
    rows = [_listener(n) for n in range(1, 6)]                                    # 1 is the first; 2-5 are interior
    rows += [_updated(10, "healthy"), _updated(11, "healthy"), _updated(12, "unavailable"),
             _updated(13, "unavailable"), _updated(14, "healthy")]                 # 11 and 13 repeat the row before
    rows.append(_listener(20, source="reporter", source_sequence=1, actor={"id": "a"}))   # v2 envelope: never touched
    rows.append(_listener(21))                                                    # interior too: the young row is the last
    young = _listener(22)
    young["ingest_timestamp"] = datetime.now(UTC) + timedelta(minutes=1)          # inside min_age: untouched
    rows.append(young)
    _insert(db, rows)


def _run(db, settings, compactor=None, ticks=1):
    compactor = compactor or FabricCompactor(settings, clock=iter(range(0, 10**6, 10**5)).__next__)

    async def go():
        conn = await asyncpg.connect(db.dsn)
        try:
            return [await compactor.tick(conn) for _ in range(ticks)]
        finally:
            await conn.close()
    return asyncio.run(go())


def _ids(db, table="fabric_events"):
    return {r["event_id"] for r in db.query(f"SELECT event_id FROM {table}")}


def test_dry_run_counts_and_deletes_nothing(db):
    _seed(db)
    before = _ids(db)
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=True))
    assert outcome.startswith("dry run: would archive 7 rows")
    assert "'ServiceListenerObserved': 5" in outcome and "'ServiceUpdated': 2" in outcome
    assert _ids(db) == before and not _ids(db, "fabric_events_archive")


def test_compaction_archives_exactly_the_interior_rows_and_the_replay_agrees(db):
    _seed(db)
    expected, _ = _reference(db)
    assert expected.export_state()["services"], "the seeded payloads must reconstruct a service"
    before = _ids(db)
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False))
    assert outcome.startswith("archived 7 rows")
    archived = _ids(db, "fabric_events_archive")
    assert archived == {f"service-svc-1-{n}" for n in (2, 3, 4, 5, 21, 11, 13)}
    assert _ids(db) | archived == before and not _ids(db) & archived         # nothing lost, nothing in both
    assert {"service-svc-1-1", "service-svc-1-20", "service-svc-1-22"} <= _ids(db)               # envelope row and young row stay
    recovered, _ = _reference(db)
    assert _projections(recovered) == _projections(expected)
    [again] = _run(db, CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False))
    assert again.startswith("archived 0 rows")                                  # idempotent


def test_an_archive_that_lacks_a_journal_column_is_repaired_and_the_move_still_works(db):
    """``trace_id`` was added to fabric_events after the archive existed: the old positional insert failed on every pass."""
    _seed(db)
    db.query("ALTER TABLE fabric_events_archive DROP COLUMN IF EXISTS trace_id")
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False))
    assert outcome.startswith("archived 7 rows"), outcome
    assert db.query("SELECT 1 FROM information_schema.columns WHERE table_name = 'fabric_events_archive' "
                    "AND column_name = 'trace_id'")
    assert len(_ids(db, "fabric_events_archive")) == 7


def test_batches_drain_over_several_ticks_and_then_rebuild_the_checkpoint(db):
    _seed(db)
    _build(db)
    expected, _ = _reference(db)
    settings = CompactionSettings(enabled=True, dry_run=False, batch_rows=2)
    outcomes = _run(db, settings, ticks=4)
    assert [o.split(";")[0] for o in outcomes] == [
        "archived 2 rows, purged 0 from the archive"] * 3 + ["archived 1 rows, purged 0 from the archive"]
    assert "checkpoint rebuilt" not in " ".join(outcomes[:3]) and "checkpoint rebuilt" in outcomes[3]
    assert len(_ids(db, "fabric_events_archive")) == 7
    recovered, _, metrics = _checkpoint_source(db)
    assert metrics["mode"] != "full" and metrics["tail_events"] == 0
    assert _projections(recovered) == _projections(expected)


def test_small_chunks_move_the_same_rows_and_leave_no_temp_table_or_lock(db):
    _seed(db)
    before = _ids(db)
    settings = CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False, chunk_rows=1,
                                  chunk_pause_seconds=0)
    compactor = FabricCompactor(settings, clock=iter(range(0, 10**6, 10**5)).__next__)

    async def go():
        conn = await asyncpg.connect(db.dsn)
        other = await asyncpg.connect(db.dsn)
        try:
            outcome = await compactor.tick(conn)
            # the lock and the temp table were released: a second session can take the lock at once
            free = await other.fetchval("SELECT pg_try_advisory_lock($1)", 0x46414243)
            temp = await conn.fetchval("SELECT to_regclass('pg_temp.compaction_pending') IS NOT NULL")
            return outcome, free, temp
        finally:
            await conn.close()
            await other.close()
    outcome, free, temp = asyncio.run(go())
    assert outcome.startswith("archived 7 rows") and free and not temp
    assert _ids(db) | _ids(db, "fabric_events_archive") == before and len(_ids(db, "fabric_events_archive")) == 7


def test_the_pass_stops_at_its_time_budget_and_asks_to_come_back(db):
    _seed(db)
    pass_times = iter([0.0, 1000.0])                                   # pass start, then past the budget
    settings = CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False, chunk_rows=2,
                                  chunk_pause_seconds=0, max_pass_seconds=60)
    compactor = FabricCompactor(settings, clock=iter(range(0, 10**6, 10**5)).__next__,
                                pass_clock=lambda: next(pass_times, 1000.0))
    [outcome] = _run(db, settings, compactor)
    assert outcome.startswith("archived 2 rows")                       # one chunk, then the budget ended the pass
    assert compactor._last == 10**5 - settings.interval_seconds + 60.0   # not drained: due again in a minute
    [rest] = _run(db, settings, compactor)                             # a later pass picks up the remainder
    assert rest.startswith("archived") and len(_ids(db, "fabric_events_archive")) > 2


def test_a_second_compaction_holding_the_lock_makes_this_one_skip(db):
    _seed(db)

    async def go():
        holder = await asyncpg.connect(db.dsn)
        conn = await asyncpg.connect(db.dsn)
        try:
            await holder.fetchval("SELECT pg_advisory_lock($1)", 0x46414243)
            settings = CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False)
            return await FabricCompactor(settings).tick(conn)
        finally:
            await holder.close()
            await conn.close()
    assert asyncio.run(go()) == "skipped: another compaction is running"
    assert not _ids(db, "fabric_events_archive")


def test_archive_retention_purges_old_archived_rows(db):
    _seed(db)
    settings = CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False, archive_retention_days=30)
    compactor = FabricCompactor(settings, clock=iter(range(0, 10**6, 10**5)).__next__)
    _run(db, settings, compactor)
    db.query("UPDATE fabric_events_archive SET archived_at = now() - interval '31 days' WHERE event_id = 'service-svc-1-2'")
    [outcome] = _run(db, settings, compactor)
    assert "purged 1 from the archive" in outcome and len(_ids(db, "fabric_events_archive")) == 6


def test_waiting_between_intervals_and_missing_table_are_harmless():
    compactor = FabricCompactor(CompactionSettings(enabled=True, dry_run=False, interval_seconds=600), clock=lambda: 5.0)

    class Conn:
        async def fetchval(self, sql, *a):
            return False

    async def go():
        first = await compactor.tick(Conn())
        return first, await compactor.tick(Conn())
    first, second = asyncio.run(go())
    assert first.startswith("skipped: apply migration 0035") and second == "waiting"


def test_pruning_keeps_the_newest_generations_and_the_active_one_and_boot_still_works(db):
    from herdr_engineering.control_plane.recovery_checkpoint import prune_checkpoints

    _seed(db)
    ids = [checkpoint_tests._build(db)["checkpoint_id"] for _ in range(4)]
    expected, _ = _reference(db)

    async def prune(keep):
        conn = await asyncpg.connect(db.dsn)
        try:
            return await prune_checkpoints(conn, keep)
        finally:
            await conn.close()
    assert asyncio.run(prune(2)) == 2
    left = {r["checkpoint_id"] for r in db.query("SELECT checkpoint_id FROM recovery_checkpoint_manifests")}
    assert left == set(ids[2:])
    for table in ("recovery_checkpoint_rows", "recovery_checkpoint_covered"):
        assert {r["checkpoint_id"] for r in db.query(f"SELECT DISTINCT checkpoint_id FROM {table}")} == left
    assert db.query("SELECT checkpoint_id FROM recovery_checkpoint_active")[0]["checkpoint_id"] == ids[3]
    assert asyncio.run(prune(2)) == 0 and asyncio.run(prune(0)) == 1            # keep is clamped to the active one
    recovered, _, metrics = _checkpoint_source(db)
    assert metrics["mode"] == "checkpoint" and metrics["checkpoint_id"] == ids[3]
    assert _projections(recovered) == _projections(expected)


def test_a_drain_prunes_after_it_rebuilds(db):
    _seed(db)
    for _ in range(3):
        _build(db)
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=False, keep_checkpoints=2))
    assert "checkpoint rebuilt" in outcome and "pruned 2 old generations" in outcome
    assert db.query("SELECT count(*) AS n FROM recovery_checkpoint_manifests")[0]["n"] == 2
