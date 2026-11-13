"""The inspector archive job against a real, throwaway PostgreSQL: what moves, what stays, and what a boot still sees."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.inspector_archive import (
    InspectorArchiver,
    InspectorArchiveSettings,
    parse_inspector_archive,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore
from tests.control_plane import test_recovery_checkpoint as checkpoint_tests
from tests.control_plane.test_bridge import FakeSource

asyncpg = pytest.importorskip("asyncpg")

pg, db = checkpoint_tests.pg, checkpoint_tests.db

NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=20)
YOUNG = NOW - timedelta(days=1)
SETTINGS = dict(enabled=True, dry_run=False, rebuild_checkpoint=False, publish_projection_checkpoint=False,
                chunk_pause_seconds=0)


# ---------------------------------------------------------------- configuration (no database)

def test_defaults_are_disabled_and_dry_run():
    s = parse_inspector_archive(None)
    assert s.enabled is False and s.dry_run is True and s.min_age_days == 14.0
    assert s.event_types == ("inspector.tool", "inspector.message")
    assert parse_inspector_archive({}) == s == InspectorArchiveSettings()


@pytest.mark.parametrize("section, message", [
    ({"enable": True}, "unknown key"),
    ({"enabled": "true"}, "enabled must be a boolean"),
    ({"dry_run": 0}, "dry_run must be a boolean"),
    ({"min_age_days": 3}, "min_age_days must be a number >= 7"),
    ({"min_age_days": True}, "min_age_days"),
    ({"chunk_rows": 5}, "chunk_rows"),
    ({"event_types": []}, "event_types"),
    ({"event_types": ["inspector.usage"]}, "event_types"),
    ({"event_types": ["inspector.tool", "inspector.tool"]}, "event_types"),
    ({"event_types": "inspector.tool"}, "event_types"),
    ("on", "must be a mapping"),
])
def test_invalid_sections_are_rejected(section, message):
    with pytest.raises(ValueError, match=message):
        parse_inspector_archive(section)


def _fleet(tmp_path, section):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": {"inspector_archive": section}}},
        "hosts": {"h": {"roles": ["node"], "token_sha256": hashlib.sha256(b"t").hexdigest()}}}))
    os.chmod(path, 0o600)
    return path


def test_fleet_config_validates_the_section(tmp_path):
    FleetConfigStore(_fleet(tmp_path, {"enabled": False, "min_age_days": 30})).current()
    with pytest.raises(FleetConfigError, match="inspector_archive: unknown key"):
        FleetConfigStore(_fleet(tmp_path, {"enabled": True, "bogus": 1})).current()
    with pytest.raises(FleetConfigError, match="min_age_days must be a number >= 7"):
        FleetConfigStore(_fleet(tmp_path, {"min_age_days": 1})).current()


# ---------------------------------------------------------------- the move (real PostgreSQL)

def _row(n, event_type="inspector.tool", *, when=OLD, sid="s1", **extra):
    kind = event_type.split(".", 1)[1]
    return dict(event_id=f"insp-{n}", entity_type="inspector", entity_id=sid, event_type=event_type, sequence=n,
                payload={"kind": kind, "call_id": f"c{n}", "tool": "bash", "output": f"out {n}",
                         "_received_at": when.timestamp(), "_sequence": n},
                source_timestamp=when, ingest_timestamp=when, **extra)


def _put(db, rows):
    columns = ("event_id", "entity_type", "entity_id", "event_type", "payload", "sequence", "source_timestamp",
               "ingest_timestamp", "source", "source_sequence", "actor")

    async def write():
        conn = await asyncpg.connect(db.dsn)
        try:
            await conn.copy_records_to_table("fabric_events", columns=columns, records=[
                tuple(json.dumps(r.get(k)) if k in {"payload", "actor"} and r.get(k) is not None else r.get(k)
                      for k in columns) for r in rows])
        finally:
            await conn.close()
    asyncio.run(write())


def _cover(db, watermark, frontier="max"):
    """A snapshot row at ``watermark`` and a projection checkpoint whose frontier is the newest position so far."""
    db.query("DELETE FROM inspector_snapshots")
    db.query("DELETE FROM projection_checkpoints")
    if watermark is not None:
        db.query("INSERT INTO inspector_snapshots (id, schema_version, fingerprint, watermark, event_count, body) "
                 "VALUES (1, 1, 'f', $1, 0, '\\x00'::bytea)", watermark)
    if frontier is not None:
        value = db.query("SELECT COALESCE(max(projection_position), 0) AS m FROM fabric_events")[0]["m"] \
            if frontier == "max" else frontier
        db.query("INSERT INTO projection_checkpoints (code_fingerprint, policy_fingerprint, frontier, format_version,"
                 " state, checksum) VALUES ('c', 'p', $1, 1, '\\x00'::bytea, 'x')", value)


def _ids(db, table="fabric_events"):
    return {r["event_id"] for r in db.query(f"SELECT event_id FROM {table}")}


def _seed(db):
    db.query("TRUNCATE fabric_events_archive")
    rows = [_row(1), _row(2), _row(3, "inspector.message"),                              # movable: old tool and message
            _row(4, "inspector.usage"), _row(5, "inspector.preferences"), _row(6, "inspector.lifecycle"),
            _row(7, "inspector.file"),                                                   # not in the default types
            _row(8, source="reporter", source_sequence=1, actor={"id": "a"}),            # v2 envelope: never
            _row(9, when=YOUNG),                                                         # young: stays
            _row(40)]                                                                    # old, but above the watermark
    rows.append(dict(_row(10, sid="svc"), entity_type="service", event_type="ServiceUpdated"))   # not inspector
    _put(db, rows)
    _cover(db, watermark=30)
    _put(db, [_row(11)])                                                                 # old, covered by the snapshot,
    # but inserted after the checkpoint: its position is past the frontier (the checkpoint's tail)


def _run(db, settings=None, archiver=None, ticks=1, **overrides):
    archiver = archiver or InspectorArchiver(settings or InspectorArchiveSettings(**{**SETTINGS, **overrides}),
                                             clock=iter(range(0, 10**6, 10**5)).__next__)

    async def go():
        conn = await asyncpg.connect(db.dsn)
        try:
            return [await archiver.tick(conn) for _ in range(ticks)]
        finally:
            await conn.close()
    return asyncio.run(go())


EXPECTED = {"insp-1", "insp-2", "insp-3"}


def test_dry_run_counts_and_changes_nothing(db):
    _seed(db)
    before = _ids(db)
    [outcome] = _run(db, dry_run=True)
    assert outcome.startswith("dry run: would archive 3 inspector rows")
    assert "'inspector.message': 1" in outcome and "'inspector.tool': 2" in outcome
    assert _ids(db) == before and not _ids(db, "fabric_events_archive")


def test_it_moves_exactly_the_eligible_rows_and_is_idempotent(db):
    _seed(db)
    before = _ids(db)
    original = {r["event_id"]: dict(r) for r in db.query("SELECT * FROM fabric_events WHERE event_id = ANY($1)",
                                                         sorted(EXPECTED))}
    [outcome] = _run(db)
    assert outcome == "archived 3 inspector rows"
    archived = _ids(db, "fabric_events_archive")
    assert archived == EXPECTED
    assert _ids(db) | archived == before and not _ids(db) & archived          # nothing lost, nothing in both
    # every column survives the move, including the projection position
    for row in db.query("SELECT * FROM fabric_events_archive"):
        row = dict(row)
        row.pop("archived_at")
        assert row == original[row["event_id"]]
    stays = {"insp-4", "insp-5", "insp-6", "insp-7", "insp-8", "insp-9", "insp-10", "insp-11", "insp-40"}
    assert stays <= _ids(db)       # usage, prefs, lifecycle, file, envelope, young, non-inspector, past frontier, past watermark
    [again] = _run(db)
    assert again == "archived 0 inspector rows" and _ids(db, "fabric_events_archive") == EXPECTED


def test_the_file_type_moves_when_configured(db):
    _seed(db)
    _run(db, event_types=("inspector.tool", "inspector.message", "inspector.file"))
    assert "insp-7" in _ids(db, "fabric_events_archive")
    assert not {"insp-4", "insp-5", "insp-6"} & _ids(db, "fabric_events_archive")


def test_nothing_moves_without_a_snapshot_covering_the_rows(db):
    _seed(db)
    _cover(db, watermark=None)
    [outcome] = _run(db)
    assert outcome == "archived 0 inspector rows" and not _ids(db, "fabric_events_archive")
    _cover(db, watermark=2)                                                     # covers sequences 1 and 2 only
    _run(db)
    assert _ids(db, "fabric_events_archive") == {"insp-1", "insp-2"}


def test_rows_past_the_checkpoint_frontier_stay_and_no_checkpoint_means_only_legacy_rows(db):
    _seed(db)
    _cover(db, watermark=30, frontier=0)                                        # every positioned row is past the frontier
    _run(db)
    assert not _ids(db, "fabric_events_archive")
    db.query("ALTER TABLE fabric_events DISABLE TRIGGER USER")
    try:        # a legacy-prefix row: the trigger would hand a rewritten row a fresh position
        db.query("UPDATE fabric_events SET projection_position = NULL WHERE event_id = 'insp-1'")
        db.query("UPDATE projection_checkpoint_control SET legacy_rows = 1")
    finally:
        db.query("ALTER TABLE fabric_events ENABLE TRIGGER USER")
    _run(db)
    assert _ids(db, "fabric_events_archive") == {"insp-1"}


def test_a_young_row_is_never_moved_however_it_is_configured(db):
    _seed(db)
    _run(db, min_age_days=30)                                                   # rows are 20 days old
    assert not _ids(db, "fabric_events_archive")


def test_chunks_and_the_batch_cap_drain_over_several_ticks(db):
    db.query("TRUNCATE fabric_events_archive")
    _put(db, [_row(n) for n in range(1, 8)])
    _cover(db, watermark=100)
    # one row per transaction, three rows per pass: three passes drain seven rows
    settings = InspectorArchiveSettings(**{**SETTINGS, "chunk_rows": 1, "batch_rows": 3})
    archiver = InspectorArchiver(settings, clock=iter(range(0, 10**6, 10**5)).__next__)
    assert _run(db, archiver=archiver) == ["archived 3 inspector rows"]
    assert len(_ids(db, "fabric_events_archive")) == 3
    archiver._last = None
    assert _run(db, archiver=archiver) == ["archived 3 inspector rows"]
    archiver._last = None
    assert _run(db, archiver=archiver) == ["archived 1 inspector rows"]
    assert len(_ids(db, "fabric_events_archive")) == 7 and not _ids(db)


def test_a_failed_archive_insert_rolls_the_delete_back(db):
    """The move is one statement in one transaction: if the insert fails, the rows are still in the journal."""
    _seed(db)
    db.query("ALTER TABLE fabric_events_archive ADD CONSTRAINT refuse_three CHECK (sequence <> 3)")
    try:
        before = _ids(db)
        [outcome] = _run(db)
        assert outcome == "failed: CheckViolationError"
        assert _ids(db) == before and not _ids(db, "fabric_events_archive")
    finally:
        db.query("ALTER TABLE fabric_events_archive DROP CONSTRAINT refuse_three")


def test_an_archive_that_lacks_a_journal_column_is_repaired(db):
    _seed(db)
    db.query("ALTER TABLE fabric_events_archive DROP COLUMN IF EXISTS trace_id")
    [outcome] = _run(db)
    assert outcome == "archived 3 inspector rows"
    assert db.query("SELECT 1 FROM information_schema.columns WHERE table_name = 'fabric_events_archive' "
                    "AND column_name = 'trace_id'")


def test_two_passes_cannot_run_at_once(db):
    _seed(db)

    async def go():
        holder = await asyncpg.connect(db.dsn)
        try:
            from herdr_engineering.control_plane.inspector_archive import _LOCK
            assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK)
            conn = await asyncpg.connect(db.dsn)
            try:
                return await InspectorArchiver(InspectorArchiveSettings(**SETTINGS)).tick(conn)
            finally:
                await conn.close()
        finally:
            await holder.close()
    assert asyncio.run(go()) == "skipped: another inspector archive is running"
    assert not _ids(db, "fabric_events_archive")


def test_the_compaction_purge_never_deletes_archived_inspector_rows(db):
    from herdr_engineering.control_plane.fabric_compaction import (
        CompactionSettings,
        FabricCompactor,
    )
    _seed(db)
    _run(db)
    db.query("INSERT INTO fabric_events_archive (event_id, sequence, event_type, entity_type, entity_id, "
             "source_timestamp, ingest_timestamp, archived_at) VALUES ('old-svc', 1, 'ServiceUpdated', 'service', 'x', "
             "now(), now(), now() - interval '60 days')")
    db.query("UPDATE fabric_events_archive SET archived_at = now() - interval '60 days'")

    async def go():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await FabricCompactor(CompactionSettings(enabled=True, dry_run=False, rebuild_checkpoint=False,
                                                            archive_retention_days=30)).tick(conn)
        finally:
            await conn.close()
    assert "purged 1 from the archive" in asyncio.run(go())
    assert _ids(db, "fabric_events_archive") == EXPECTED


# ---------------------------------------------------------------- what a boot sees afterwards

def _boot_state(db):
    """Replay the live journal the way a boot does (real clock) and return the inspector's exported state, minus the
    replay bookkeeping (how many rows were consumed) which archiving changes by design."""
    rows = [dict(r) for r in db.query("SELECT * FROM fabric_events ORDER BY ingest_timestamp, sequence, event_id")]
    repo = ControlPlaneRepo()
    load_into(repo, FakeSource(events=rows))
    state = repo.inspector.export_state()
    for key in ("watermark", "count", "epoch"):
        state.pop(key, None)
    state.get("replay", {}).pop("count", None)
    state.get("replay", {}).pop("watermark", None)
    return state


def test_replay_state_is_unchanged_by_archiving_rows_the_ring_has_long_evicted(db):
    _seed(db)
    before = _boot_state(db)
    assert before["sessions"], "the seeded rows must reconstruct an inspector session"
    assert before["stats"]["rejected"] >= 3, "the old tool and message rows are rejected by the ring on replay"
    _run(db)
    after = _boot_state(db)
    for a, b in zip(after["sessions"], before["sessions"], strict=True):
        for key in a:
            if key != "last_ingest":                    # the wall clock of each replay
                assert a[key] == b[key], key


def test_snapshot_count_includes_archived_rows(db):
    """A snapshot that consumed N rows still verifies after some of them moved to the archive."""
    from herdr_engineering.control_plane.inspector_snapshot import archived_inspector_count
    _seed(db)

    async def count():
        conn = await asyncpg.connect(db.dsn)
        try:
            return await archived_inspector_count(conn, 30)
        finally:
            await conn.close()
    assert asyncio.run(count()) == 0
    _run(db)
    assert asyncio.run(count()) == 3


def test_a_history_page_in_inspector_history_order_reads_the_archive_identically(db):
    """PR 7 (#743) pages ``fabric_events`` by (ingest_timestamp, sequence, event_id) newest first. The archive has the same
    columns and index order, so a follow-up that reads it, or a UNION of both, returns the same rows in the same order."""
    _seed(db)
    sql = ("SELECT event_id FROM {t} WHERE entity_type = 'inspector' AND entity_id = 's1' "
           "AND event_type LIKE 'inspector.%' AND event_type <> 'inspector.preferences' "
           "AND event_id = ANY($1) ORDER BY ingest_timestamp DESC, sequence DESC, event_id COLLATE \"C\" DESC")
    ids = sorted(EXPECTED)
    live = [r["event_id"] for r in db.query(sql.format(t="fabric_events"), ids)]
    _run(db)
    assert not db.query(sql.format(t="fabric_events"), ids)
    assert [r["event_id"] for r in db.query(sql.format(t="fabric_events_archive"), ids)] == live


def test_the_archive_index_exists_and_is_partial(db):
    [row] = db.query("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_fabric_events_archive_inspector'")
    assert "entity_id, ingest_timestamp, sequence" in row["indexdef"] and "entity_type = 'inspector'" in row["indexdef"]


def test_it_is_a_noop_when_the_snapshot_table_is_missing_message_is_clear(db):
    """Dry-run on a database that has the tables reports counts; without the archive table it names the migration."""
    _seed(db)
    db.query("ALTER TABLE fabric_events_archive RENAME TO fabric_events_archive_x")
    try:
        [outcome] = _run(db)
        assert outcome == "skipped: apply migration 0035 (fabric_events_archive) first"
    finally:
        db.query("ALTER TABLE fabric_events_archive_x RENAME TO fabric_events_archive")


def test_the_recovery_checkpoint_is_rebuilt_after_a_drained_pass_that_moved_rows(db):
    _seed(db)
    db.query("TRUNCATE recovery_checkpoint_manifests CASCADE")
    archiver = InspectorArchiver(InspectorArchiveSettings(**{**SETTINGS, "rebuild_checkpoint": True}),
                                 clock=iter(range(0, 10**6, 10**5)).__next__)
    [outcome] = _run(db, archiver=archiver)
    assert outcome.startswith("archived 3 inspector rows; checkpoint rebuilt"), outcome
    assert db.query("SELECT count(*) AS n FROM recovery_checkpoint_manifests")[0]["n"] >= 1
    archiver._last = None
    assert _run(db, archiver=archiver) == ["archived 0 inspector rows"]          # nothing moved: nothing rebuilt


# ---------------------------------------------------------------- the real snapshot writer and the boot's count

def test_the_real_snapshot_still_verifies_after_rows_move_to_the_archive(db):
    """Without the archive-aware count the writer would rebuild and a boot would refuse the snapshot (and lose usage)."""
    from herdr_engineering.control_plane import inspector_snapshot as snap
    from herdr_engineering.control_plane.bridge import PostgresSource
    from tests.control_plane.test_inspector_snapshot import LIMITS, T0, stream
    shift = (NOW - timedelta(days=30)).timestamp() - T0
    db.query("TRUNCATE fabric_events_archive")
    db.query("DELETE FROM inspector_snapshots")
    rows = []
    for e in stream(300, seed=5):
        stamp = datetime.fromisoformat(e["source_timestamp"]) + timedelta(seconds=shift)
        rows.append(dict(event_id=f"r{e['sequence']}", entity_type="inspector", entity_id=e["entity_id"],
                         event_type=e["event_type"], sequence=e["sequence"], source_timestamp=stamp,
                         ingest_timestamp=stamp, payload=e["payload"]))
    _put(db, rows)
    live_before = len(rows)

    async def scenario():
        conn = await asyncpg.connect(db.dsn)
        try:
            from herdr_engineering.control_plane.inspector import InspectorStore
            make = lambda: InspectorStore.from_config(LIMITS)           # noqa: E731
            first = await snap.SnapshotWriter(make, interval=0, settle=0).tick(conn)
            row = dict(await conn.fetchrow("SELECT watermark, event_count FROM inspector_snapshots WHERE id = 1"))
            await conn.execute("INSERT INTO projection_checkpoints (code_fingerprint, policy_fingerprint, frontier,"
                               " format_version, state, checksum) SELECT 'c', 'p', COALESCE(max(projection_position), 0),"
                               " 1, '\\x00'::bytea, 'x' FROM fabric_events")
            moved = await InspectorArchiver(InspectorArchiveSettings(**SETTINGS, event_types=(
                "inspector.tool", "inspector.message", "inspector.file", "inspector.test"))).tick(conn)
            second = await snap.SnapshotWriter(make, interval=0, settle=0).tick(conn)   # a fresh writer, as after a restart
            source = PostgresSource(conn, inspector_snapshots=True, run=lambda c: None)
            boot_row, skip = await source._inspector_snapshot_row(conn)
            return first, row, moved, second, boot_row, skip
        finally:
            await conn.close()
    first, row, moved, second, boot_row, skip = asyncio.run(scenario())
    assert first.startswith("written") and row["event_count"] == live_before
    archived = int(moved.split()[1])
    assert moved.startswith("archived") and archived > 0
    assert len(_ids(db)) == live_before - archived
    assert second == "unchanged", second                                          # no "rebuilding"
    assert boot_row["archived_count"] == archived and skip == row["watermark"]    # the boot still skips the snapshot's rows
