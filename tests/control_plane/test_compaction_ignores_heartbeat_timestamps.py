"""ServiceUpdated compaction treats rows that differ only by heartbeat timestamps as repeats, replay-equivalently.

Real PostgreSQL (the disposable fixture of the checkpoint tests); no mocks.
"""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.fabric_compaction import CompactionSettings
from tests.control_plane import test_recovery_checkpoint as checkpoint_tests
from tests.control_plane.test_fabric_compaction import SVC, _ids, _run, asyncpg
from tests.control_plane.test_postgres_recovery import (
    _insert,
    _projections,
    _reference,
    _row,
    _snapshot,
)

pg, db = checkpoint_tests.pg, checkpoint_tests.db      # the disposable-database fixtures

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg required")


def _beat(n, *, route="http://api.test", status="healthy", stamp=None, **extra):
    """One heartbeat row: the three timestamps move with ``n``, everything else is the arguments."""
    stamp = stamp or f"2026-10-26T01:{n:02d}:00+00:00"
    payload = {**_snapshot(), "route": route, "status": status, "metadata_updated_at": stamp,
               "verified_origin": {"kind": "herdr_launch_origin", "session_id": "s", "verified_at": stamp},
               "metadata_observation": {"state": "observed", "observed_at": stamp, "stale_after_seconds": 90}, **extra}
    return _row("service", SVC, "ServiceUpdated", payload, n)


def _seed(db):
    db.query("TRUNCATE fabric_events_archive")
    db.query("TRUNCATE recovery_checkpoint_manifests CASCADE")
    # 1 first | 2,3 timestamp-only repeats | 4 route changed (kept) | 5 repeat | 6 status change (kept) | 7 repeat | 8 last
    _insert(db, [_beat(1), _beat(2), _beat(3), _beat(4, route="http://api2.test"), _beat(5, route="http://api2.test"),
                 _beat(6, route="http://api2.test", status="unavailable", unavailable_since="2026-10-26T01:06:00+00:00"),
                 _beat(7, route="http://api2.test", status="unavailable", unavailable_since="2026-10-26T01:06:00+00:00"),
                 _beat(8, route="http://api2.test", status="unavailable", unavailable_since="2026-10-26T01:06:00+00:00")])


def test_timestamp_only_repeats_are_archived_and_the_replay_is_identical(db):
    _seed(db)
    expected, _ = _reference(db)
    assert expected.export_state()["services"][SVC]["status"] == "unavailable"
    before = _ids(db)
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False))
    assert outcome.startswith("archived 4 rows"), outcome
    archived = _ids(db, "fabric_events_archive")
    assert archived == {f"service-{SVC}-{n}" for n in (2, 3, 5, 7)}      # first, material changes and the last row stay
    assert _ids(db) | archived == before and not _ids(db) & archived
    recovered, _ = _reference(db)
    assert _projections(recovered) == _projections(expected)           # includes updated_at: the last row is kept


def test_a_changed_material_field_is_never_a_repeat(db):
    _seed(db)
    _insert(db, [_beat(9, route="http://api2.test", status="unavailable", unavailable_since="2026-10-26T01:06:00+00:00",
                       description="renamed")])
    _run(db, CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False))
    kept = _ids(db)
    assert f"service-{SVC}-9" in kept                                  # the changed description row is the last row
    assert f"service-{SVC}-6" in kept                                  # the status transition stays
    assert f"service-{SVC}-8" not in kept                              # 8 only repeats 7: removable once 9 is last


def test_exact_equality_is_back_when_normalising_is_off(db):
    _seed(db)
    expected, _ = _reference(db)
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False,
                                            normalize_service_timestamps=False))
    assert outcome.startswith("archived 0 rows"), outcome            # every beat carries a new timestamp
    recovered, _ = _reference(db)
    assert _projections(recovered) == _projections(expected)


def test_dry_run_counts_the_normalised_repeats(db):
    _seed(db)
    [outcome] = _run(db, CompactionSettings(enabled=True, dry_run=True))
    assert "'ServiceUpdated': 4" in outcome
