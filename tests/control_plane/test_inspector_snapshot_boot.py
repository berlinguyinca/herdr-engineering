"""PR 5: inspector snapshot boot. YAML-only config (validated in fleet_config) and, against a real throwaway
Postgres, boot-from-snapshot equals full replay (totals, rev, next_seq) and every failure falls back to replay."""
import asyncio
import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime

import pytest
import yaml

from herdr_engineering.control_plane import inspector_snapshot as snap
from herdr_engineering.control_plane.bridge import PostgresSource, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore
from tests.control_plane.test_inspector_snapshot import LIMITS, T0, stream
from tests.e2e.pg import ThrowawayPostgres, find_bins

INSERT = ("INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
          " source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)")


def test_parse_defaults_to_off():
    assert snap.parse_inspector_snapshots(None) == {"enabled": False, "interval_seconds": 600.0}
    assert snap.parse_inspector_snapshots({}) == {"enabled": False, "interval_seconds": 600.0}
    assert snap.parse_inspector_snapshots({"enabled": True, "interval_seconds": 90}) == {
        "enabled": True, "interval_seconds": 90.0}


@pytest.mark.parametrize("bad, match", [
    ("on", "must be a mapping"), ([True], "must be a mapping"),
    ({"enabled": "yes"}, "enabled must be a boolean"), ({"enabled": 1}, "enabled must be a boolean"),
    ({"enabled": True, "interval_seconds": 59}, "interval_seconds"),
    ({"enabled": True, "interval_seconds": True}, "interval_seconds"),
    ({"enabled": True, "interval_seconds": "600"}, "interval_seconds"),
    ({"enabled": True, "interval": 600}, "unknown key"),
])
def test_parse_rejects_anything_malformed(bad, match):
    with pytest.raises(ValueError, match=match):
        snap.parse_inspector_snapshots(bad)


def _fleet(tmp_path, value):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": {"inspector_snapshots": value}}},
        "hosts": {"h": {"roles": ["node"], "token_sha256": hashlib.sha256(b"t").hexdigest()}}}))
    os.chmod(path, 0o600)
    return path


def test_fleet_config_accepts_valid_and_rejects_unknown_keys(tmp_path):
    FleetConfigStore(_fleet(tmp_path, {"enabled": True, "interval_seconds": 120})).current()
    with pytest.raises(FleetConfigError, match="unknown key"):
        FleetConfigStore(_fleet(tmp_path, {"enabled": True, "env": "X"})).current()
    with pytest.raises(FleetConfigError, match="enabled must be a boolean"):
        FleetConfigStore(_fleet(tmp_path, {"enabled": "true"})).current()


def _insert_all(conn, rows):
    async def go():
        for e in rows:
            await conn.execute(INSERT, f"id-{e['sequence']}", e["sequence"], e["event_type"], e["entity_id"],
                               json.dumps(e["payload"]), datetime.fromisoformat(e["source_timestamp"]))
    return go()


def _boot(dsn, snapshots):
    repo = ControlPlaneRepo(inspector_limits=LIMITS)
    repo.inspector._clock = lambda: T0 + 10_000
    load_into(repo, PostgresSource(dsn=dsn, inspector_snapshots=snapshots))
    return repo.inspector.export_state()


def _view(state):
    """The per-session scalars the design calls out, plus the global watermark and count."""
    return {"watermark": state["watermark"], "count": state["count"],
            "sessions": {s["session_id"]: {k: s[k] for k in ("next_seq", "rev", "usage_totals", "usage_latest", "dropped_ring")}
                         for s in state["sessions"]}}


@pytest.fixture(scope="module")
def _database(tmp_path_factory):
    """A throwaway Postgres with every real migration applied (the same schema PostgresSource reads in production)."""
    bins = find_bins({})
    if bins is None:
        pytest.skip("PostgreSQL server binaries are required")
    database = ThrowawayPostgres(bins, tmp_path_factory.mktemp("snapshot-boot-pg"))
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


@pytest.fixture
def pg(_database):
    pytest.importorskip("asyncpg")
    _database.query("TRUNCATE fabric_events")
    _database.query("DELETE FROM inspector_snapshots")
    return _database.dsn


def _worker_store():
    """Built the way the worker builds it, so the fingerprint (limits, roles, redaction) matches a booting repo."""
    shadow = ControlPlaneRepo(inspector_limits=LIMITS).inspector
    shadow._clock = lambda: T0 + 10_000
    return shadow


def _seed(dsn, rows, snapshot_after):
    import asyncpg

    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            await _insert_all(conn, rows[:snapshot_after])
            writer = snap.SnapshotWriter(_worker_store, interval=0, settle=-10**9)
            await writer.tick(conn)
            await _insert_all(conn, rows[snapshot_after:])
        finally:
            await conn.close()
    asyncio.run(go())


async def _exec(dsn, sql, *args):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


@pytest.mark.integration
def test_snapshot_boot_equals_full_replay_for_totals_rev_and_next_seq(pg, caplog):
    rows = stream(500, seed=77)
    _seed(pg, rows, 350)
    caplog.set_level(logging.INFO)
    full = _boot(pg, False)
    caplog.clear()
    fast = _boot(pg, True)
    # the snapshot, not a replay, rebuilt the covered 350 rows: every row was loaded, but the 350 covered ones were not re-run through redaction
    assert "boot inspector replay: snapshot (501 events)" in caplog.text
    assert _view(fast) == _view(full)
    assert any(s["next_seq"] > 0 for s in fast["sessions"])


@pytest.mark.integration
def test_a_writer_killed_mid_write_leaves_the_previous_snapshot_and_boot_still_matches(pg):
    import asyncpg
    rows = stream(500, seed=78)
    _seed(pg, rows, 300)
    before = asyncio.run(_exec(pg, "SELECT watermark, event_count, md5(body) AS h FROM inspector_snapshots WHERE id = 1"))[0]

    async def kill_mid_write():
        victim = await asyncpg.connect(pg)
        killer = await asyncpg.connect(pg)
        try:
            pid = await victim.fetchval("SELECT pg_backend_pid()")
            tx = victim.transaction()
            await tx.start()
            await victim.execute("UPDATE inspector_snapshots SET watermark = watermark + 999, event_count = 1, "
                                 "body = '\\x00'::bytea WHERE id = 1")
            await killer.execute("SELECT pg_terminate_backend($1)", pid)     # the writer dies before COMMIT
        finally:
            await killer.close()
            victim.terminate()
    asyncio.run(kill_mid_write())
    after = asyncio.run(_exec(pg, "SELECT watermark, event_count, md5(body) AS h FROM inspector_snapshots WHERE id = 1"))[0]
    assert dict(after) == dict(before)
    assert _view(_boot(pg, True)) == _view(_boot(pg, False))


@pytest.mark.integration
@pytest.mark.parametrize("damage", [
    "body = substring(body from 1 for 40)",              # torn write: truncated gzip
    "body = '\\x00'::bytea",                              # garbage
    "event_count = event_count + 1",                      # row disagrees with the log
    "schema_version = schema_version + 1",
])
def test_a_damaged_snapshot_falls_back_to_the_full_replay(pg, damage):
    rows = stream(400, seed=79)
    _seed(pg, rows, 300)
    asyncio.run(_exec(pg, f"UPDATE inspector_snapshots SET {damage} WHERE id = 1"))
    assert _view(_boot(pg, True)) == _view(_boot(pg, False))


@pytest.mark.integration
def test_a_missing_snapshot_table_row_is_a_full_replay(pg):
    rows = stream(200, seed=80)
    _seed(pg, rows, 150)
    asyncio.run(_exec(pg, "DELETE FROM inspector_snapshots"))
    assert _view(_boot(pg, True)) == _view(_boot(pg, False))
