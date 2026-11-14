"""Partitioning fabric_events: prepare, backfill, cutover, rollback against a real, throwaway PostgreSQL (no mocks)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from herdr_engineering.control_plane import fabric_partitioning as fp
from herdr_engineering.control_plane.projection_store import FENCE_NAMESPACE, ProjectionStore
from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore
from tests.e2e.pg import ThrowawayPostgres, find_bins

asyncpg = pytest.importorskip("asyncpg")

ON = fp.FabricPartitioningSettings(enabled=True, batch_rows=100, pause_seconds=0, lock_timeout_seconds=2)
NOW = datetime.now(UTC)


# ---------------------------------------------------------------- configuration (no database)

def test_defaults_are_disabled():
    s = fp.parse_fabric_partitioning(None)
    assert s.enabled is False and fp.parse_fabric_partitioning({}) == s


@pytest.mark.parametrize("section, message", [
    ({"enable": True}, "unknown key"),
    ({"enabled": "yes"}, "enabled must be a boolean"),
    ({"verify_counts": 1}, "verify_counts must be a boolean"),
    ({"batch_rows": 5}, "batch_rows"),
    ({"batch_rows": True}, "batch_rows"),
    ({"pause_seconds": -1}, "pause_seconds"),
    ({"lock_timeout_seconds": 0}, "lock_timeout_seconds"),
    ("on", "must be a mapping"),
])
def test_invalid_sections_are_rejected(section, message):
    with pytest.raises(ValueError, match=message):
        fp.parse_fabric_partitioning(section)


def _fleet(tmp_path, section):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": {"fabric_partitioning": section}}},
        "hosts": {"h": {"roles": ["node"], "token_sha256": hashlib.sha256(b"t").hexdigest()}}}))
    os.chmod(path, 0o600)
    return path


def test_fleet_config_validates_the_section(tmp_path):
    FleetConfigStore(_fleet(tmp_path, {"enabled": False, "batch_rows": 1000})).current()
    with pytest.raises(FleetConfigError, match="fabric_partitioning: unknown key"):
        FleetConfigStore(_fleet(tmp_path, {"bogus": 1})).current()
    with pytest.raises(FleetConfigError, match="batch_rows"):
        FleetConfigStore(_fleet(tmp_path, {"batch_rows": 1})).current()


# ---------------------------------------------------------------- real PostgreSQL

@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bins = find_bins({})
    if bins is None:
        pytest.skip("PostgreSQL server binaries are required")
    database = ThrowawayPostgres(bins, tmp_path_factory.mktemp("partition-pg"), db="template_fe")
    try:
        subprocess.run([str(bins / "initdb"), "-D", str(database.data), "-U", "postgres", "--auth=trust", "-E", "UTF8",
                        "--no-sync"], check=True, capture_output=True)
        database.start_existing()
        subprocess.run([str(bins / "createdb"), "-h", str(database.sock), "-U", "postgres", database.db],
                       check=True, capture_output=True)
        asyncio.run(database._migrate())
        yield database
    finally:
        database.close()


class Db:
    """One fresh database per test (cloned from the migrated template) with a persistent connection."""

    def __init__(self, cluster):
        self.name = "t_" + uuid.uuid4().hex[:10]
        self.cluster = cluster
        subprocess.run([str(cluster.bins / "createdb"), "-h", str(cluster.sock), "-U", "postgres", "-T", cluster.db,
                        self.name], check=True, capture_output=True)
        self.loop = asyncio.new_event_loop()
        self.conn = self.run(asyncpg.connect(f"postgresql://postgres@/{self.name}?host={cluster.sock}"))

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def q(self, sql, *args):
        return self.run(self.conn.fetch(sql, *args))

    def one(self, sql, *args):
        return self.run(self.conn.fetchval(sql, *args))

    def close(self):
        self.run(self.conn.close())
        self.loop.close()


@pytest.fixture
def db(cluster):
    d = Db(cluster)
    yield d
    d.close()


def _put(db, rows):
    columns = ("event_id", "entity_type", "entity_id", "event_type", "payload", "sequence", "source_timestamp",
               "ingest_timestamp", "source", "source_sequence", "actor")
    db.run(db.conn.copy_records_to_table("fabric_events", columns=columns, records=[
        tuple(json.dumps(r.get(k)) if k in {"payload", "actor"} and r.get(k) is not None else r.get(k)
              for k in columns) for r in rows]))


def _row(n, entity_type="inspector", **extra):
    base = dict(event_id=f"e{n}", entity_type=entity_type, entity_id=f"{entity_type}-{n % 5}",
                event_type=f"{entity_type}.tool", sequence=n, payload={"n": n}, source_timestamp=NOW,
                ingest_timestamp=NOW - timedelta(minutes=n))
    base.update(extra)
    return base


def _seed(db, inspector=250, other=60):
    rows = [_row(i) for i in range(inspector)]
    rows += [_row(1000 + i, "service") for i in range(other)]
    rows += [_row(2000 + i, "mission", source="agent", source_sequence=i + 1, actor={"id": "alice", "kind": "agent"})
             for i in range(10)]
    _put(db, rows)
    return len(rows)


def _backfilled(db):
    total = _seed(db)
    assert db.run(fp.backfill(db.conn, ON))["done"]
    return total


def _fingerprint(db, table="fabric_events"):
    return db.q(f"SELECT event_id, entity_type, projection_position, recovery_instance::text AS ri, sequence "
                f"FROM {table} ORDER BY event_id, entity_type")


def test_migration_prepares_without_touching_the_live_table(db):
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events'::regclass") == "r"
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events_part'::regclass") == "p"
    parts = db.q("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                 "WHERE i.inhparent='fabric_events_part'::regclass ORDER BY 1")
    assert [r["relname"] for r in parts] == ["fabric_events_part_inspector", "fabric_events_part_other"]
    assert db.one("SELECT count(*) FROM fabric_events_part") == 0
    assert db.one("SELECT phase FROM fabric_events_partition_state") == "prepared"
    # the prepared table carries no projection triggers until cutover
    assert db.one("SELECT count(*) FROM pg_trigger WHERE tgrelid='fabric_events_part'::regclass AND NOT tgisinternal") == 0


def test_every_live_index_has_a_partitioned_counterpart(db):
    live = {r["name"] for r in db.q("SELECT c.relname AS name FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                                    "WHERE i.indrelid='fabric_events'::regclass")}
    part = {r["name"][:-2] for r in db.q(
        "SELECT c.relname AS name FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        "WHERE i.indrelid='fabric_events_part'::regclass")}
    # the live primary key was dropped by migration 0005 and the projection_position index is built by `checkpoint`
    assert live - part <= {"idx_fabric_events_projection_position"}, live - part
    assert "idx_fabric_events_projection_position" in part


def test_steps_refuse_while_disabled(db):
    off = fp.FabricPartitioningSettings()
    for step in (fp.backfill, fp.cutover, fp.rollback, fp.drop_old):
        with pytest.raises(fp.PartitioningError, match="enabled is false"):
            db.run(step(db.conn, off))
    assert db.one("SELECT phase FROM fabric_events_partition_state") == "prepared"


def test_backfill_copies_every_row_verbatim_and_logs_nothing(db):
    total = _seed(db)
    changes_before = db.one("SELECT count(*) FROM projection_event_changes")
    before = _fingerprint(db)
    result = db.run(fp.backfill(db.conn, ON))
    assert result["done"] and result["phase"] == "backfilled"
    assert db.one("SELECT count(*) FROM fabric_events_part") == total
    assert _fingerprint(db, "fabric_events_part") == before        # positions and recovery instances preserved
    assert db.one("SELECT count(*) FROM projection_event_changes") == changes_before
    assert db.one("SELECT count(*) FROM fabric_events_part_inspector") == 250
    assert db.one("SELECT count(*) FROM fabric_events_part_other") == 70


def test_backfill_is_resumable_and_idempotent(db):
    total = _seed(db)
    first = db.run(fp.backfill(db.conn, ON, max_batches=2))
    assert first["done"] is False and first["copied"] == 200
    assert db.one("SELECT phase FROM fabric_events_partition_state") == "backfilling"
    assert db.run(fp.backfill(db.conn, ON))["done"]
    assert db.run(fp.backfill(db.conn, ON))["copied"] == 0           # nothing left, nothing duplicated
    assert db.one("SELECT count(*) FROM fabric_events_part") == total


def test_cutover_refuses_before_the_backfill_is_done(db):
    _seed(db)
    db.run(fp.backfill(db.conn, ON, max_batches=1))
    with pytest.raises(fp.PartitioningError, match="backfill is not complete"):
        db.run(fp.cutover(db.conn, ON))
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events'::regclass") == "r"


def test_cutover_catches_up_writes_updates_and_deletes_made_during_the_backfill(db):
    _seed(db)
    db.run(fp.backfill(db.conn, ON, max_batches=1))
    # live traffic after the backfill began: an insert, an update of a copied row and a delete of a copied row
    _put(db, [_row(5000, "service"), _row(5001, "inspector")])
    db.q("UPDATE fabric_events SET payload='{\"changed\":true}'::jsonb WHERE event_id='e3'")
    db.q("DELETE FROM fabric_events WHERE event_id='e1'")
    db.q("DELETE FROM fabric_events WHERE event_id='e249'")      # deleted before it was ever copied
    assert db.run(fp.backfill(db.conn, ON))["done"]
    _put(db, [_row(5002, "mission")])                             # after the backfill completed
    expected = _fingerprint(db)
    result = db.run(fp.cutover(db.conn, ON))
    assert result["phase"] == "partitioned"
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events'::regclass") == "p"
    assert _fingerprint(db) == expected
    assert db.one("SELECT payload->>'changed' FROM fabric_events WHERE event_id='e3'") == "true"
    assert db.one("SELECT count(*) FROM fabric_events WHERE event_id IN ('e1','e249')") == 0


def test_after_cutover_writes_route_by_partition_and_keep_the_projection_contract(db):
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))
    top = db.one("SELECT max(projection_position) FROM fabric_events")
    _put(db, [_row(7000), _row(7001, "service")])
    got = db.q("SELECT tableoid::regclass::text AS part, entity_type, projection_position, recovery_instance "
               "FROM fabric_events WHERE event_id IN ('e7000','e7001') ORDER BY event_id")
    assert [r["part"] for r in got] == ["fabric_events_part_inspector", "fabric_events_part_other"]
    assert all(r["projection_position"] > top and r["recovery_instance"] for r in got)
    changes = db.q("SELECT operation, event_id FROM projection_event_changes WHERE event_id IN ('e7000','e7001') ORDER BY 2")
    assert [(c["operation"], c["event_id"]) for c in changes] == [("I", "e7000"), ("I", "e7001")]
    db.q("DELETE FROM fabric_events WHERE event_id = 'e7000'")
    assert db.one("SELECT count(*) FROM projection_event_changes WHERE operation='D' AND event_id='e7000'") == 1
    db.q("TRUNCATE fabric_events")
    assert db.one("SELECT count(*) FROM projection_event_changes WHERE operation='T'") == 1


def test_the_writer_fence_still_waits_for_writers_on_a_partition(db):
    """Row triggers on a partition see the PARTITION's name; the fence key must still be the parent's."""
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))

    async def go():
        writer = await asyncpg.connect(f"postgresql://postgres@/{db.name}?host={db.cluster.sock}")
        try:
            tx = writer.transaction()
            await tx.start()
            await writer.execute("INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, "
                                 "source_timestamp) VALUES ('fence', 1, 'inspector.tool', 'inspector', 'x', now())")
            held = await db.conn.fetchval(
                "SELECT pg_try_advisory_lock($1::int, hashtext(current_schema() || '.fabric_events'))",
                FENCE_NAMESPACE)
            await tx.rollback()
            return held
        finally:
            await writer.close()
    assert db.run(go()) is False


def test_uniqueness_survives_partitioning(db):
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))
    n = db.one("SELECT count(*) FROM fabric_events")
    # same event retried: ON CONFLICT DO NOTHING (what the sink uses) dedups, in the same partition
    _put_conflict = ("INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, source_timestamp) "
                     "VALUES ($1, 1, 't', $2, 'x', now()) ON CONFLICT DO NOTHING")
    db.q(_put_conflict, "e5", "inspector")
    db.q(_put_conflict, "dup-null", None)
    db.q(_put_conflict, "dup-null", None)           # NULL entity_type still collides (NULLS NOT DISTINCT)
    assert db.one("SELECT count(*) FROM fabric_events") == n + 1
    # the v2 sequence rule still holds within a partition
    with pytest.raises(asyncpg.UniqueViolationError):
        _put(db, [_row(9000, "mission", source="agent", source_sequence=3, actor={"id": "alice"})])


def test_cutover_refuses_inspector_rows_with_v2_envelopes(db):
    _put(db, [_row(1, source="agent", source_sequence=1, actor={"id": "a"})])
    assert db.run(fp.backfill(db.conn, ON))["done"]
    with pytest.raises(fp.PartitioningError, match="inspector rows carry v2 envelopes"):
        db.run(fp.cutover(db.conn, ON))
    assert db.run(fp.cutover(db.conn, ON, allow_inspector_envelopes=True))["phase"] == "partitioned"


def test_cutover_rolls_back_on_a_count_mismatch(db):
    _backfilled(db)
    # a stray row in the copy that the live table never had: no catch-up can explain it
    db.q("INSERT INTO fabric_events_part (event_id, sequence, event_type, entity_type, source_timestamp) "
         "VALUES ('stray', 1, 't', 'service', now())")
    with pytest.raises(fp.PartitioningError, match="verification failed"):
        db.run(fp.cutover(db.conn, ON))
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events'::regclass") == "r"
    assert db.one("SELECT phase FROM fabric_events_partition_state") == "backfilled"
    assert db.one("SELECT to_regclass('fabric_events_unpartitioned')") is None


def test_cutover_does_not_wait_forever_for_a_busy_table(db):
    _backfilled(db)

    async def go():
        blocker = await asyncpg.connect(f"postgresql://postgres@/{db.name}?host={db.cluster.sock}")
        try:
            tx = blocker.transaction()
            await tx.start()
            await blocker.execute("LOCK TABLE fabric_events IN ROW EXCLUSIVE MODE")
            fast = fp.FabricPartitioningSettings(enabled=True, lock_timeout_seconds=0.5)
            try:
                await fp.cutover(db.conn, fast)
            finally:
                await tx.rollback()
        finally:
            await blocker.close()
    with pytest.raises(fp.PartitioningError, match="could not take the table lock"):
        db.run(go())
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events'::regclass") == "r"


def test_index_names_are_canonical_after_cutover_and_old_ones_are_suffixed(db):
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))
    names = {r["name"] for r in db.q("SELECT c.relname AS name FROM pg_class c WHERE c.relkind='I'")}
    assert {"idx_fabric_events_stream", "uq_fabric_events_actor_event_id",
            "idx_fabric_events_projection_position"} <= names
    olds = {r["name"] for r in db.q("SELECT c.relname AS name FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                                    "WHERE i.indrelid='fabric_events_unpartitioned'::regclass")}
    assert olds and all(n.endswith("_old") for n in olds)


def test_checkpoint_index_preparation_skips_a_partitioned_journal(db):
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))
    db.run(ProjectionStore(db.conn).ensure_indexes())        # would raise: CONCURRENTLY on a partitioned table


def test_rollback_returns_every_change_made_since_cutover(db):
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))
    _put(db, [_row(8000), _row(8001, "service")])
    db.q("UPDATE fabric_events SET payload='{\"after\":1}'::jsonb WHERE event_id='e4'")
    db.q("DELETE FROM fabric_events WHERE event_id='e2'")
    expected = _fingerprint(db)
    changes = db.one("SELECT count(*) FROM projection_event_changes")
    result = db.run(fp.rollback(db.conn, ON))
    assert result["phase"] == "backfilled"
    assert db.one("SELECT relkind::text FROM pg_class WHERE oid='fabric_events'::regclass") == "r"
    assert _fingerprint(db) == expected
    assert db.one("SELECT payload->>'after' FROM fabric_events WHERE event_id='e4'") == "1"
    assert db.one("SELECT count(*) FROM projection_event_changes") == changes     # the copy logged nothing
    # the old table's own triggers work again, and the partitioned table is ready for a second attempt
    _put(db, [_row(8100)])
    assert db.one("SELECT projection_position FROM fabric_events WHERE event_id='e8100'") is not None
    assert db.one("SELECT to_regclass('fabric_events_part')") is not None
    assert db.one("SELECT count(*) FROM pg_trigger WHERE tgrelid='fabric_events_part'::regclass AND NOT tgisinternal") == 0
    assert {r["name"] for r in db.q("SELECT c.relname AS name FROM pg_class c WHERE c.relkind='i' "
                                    "AND c.relname LIKE 'idx_fabric_events_stream%'")} >= {"idx_fabric_events_stream"}
    assert db.run(fp.cutover(db.conn, ON))["phase"] == "partitioned"               # re-cutover works
    assert db.one("SELECT count(*) FROM fabric_events WHERE event_id='e8100'") == 1


def test_drop_old_is_the_only_irreversible_step(db):
    _backfilled(db)
    db.run(fp.cutover(db.conn, ON))
    db.run(fp.drop_old(db.conn, ON))
    assert db.one("SELECT to_regclass('fabric_events_unpartitioned')") is None
    with pytest.raises(fp.PartitioningError, match="cannot roll back"):
        db.run(fp.rollback(db.conn, ON))


def test_status_reports_phase_and_counts(db):
    total = _backfilled(db)
    s = db.run(fp.status(db.conn))
    assert s["phase"] == "backfilled" and s["counts"]["fabric_events_part"] == total
    db.run(fp.cutover(db.conn, ON))
    s = db.run(fp.status(db.conn))
    assert s["phase"] == "partitioned" and s["relkinds"]["fabric_events"] == "p"
    assert set(s["counts"]["partitions"]) == {"fabric_events_part_inspector", "fabric_events_part_other"}


def test_a_column_added_after_preparation_is_refused_not_lost(db):
    _seed(db)
    db.q("ALTER TABLE fabric_events ADD COLUMN extra text")
    with pytest.raises(fp.PartitioningError, match="different columns"):
        db.run(fp.backfill(db.conn, ON))
