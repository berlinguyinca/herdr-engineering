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
