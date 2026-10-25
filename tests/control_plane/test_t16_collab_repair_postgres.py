"""T16 H1, MAJOR B: ``collab-repair --apply`` against a REAL PostgreSQL (a throwaway cluster on a unix socket).

The fake-connection test in test_t16_security.py cannot see driver binding errors (asyncpg binds a ``tid`` as a
(block, offset) tuple, not as the text ``ctid::text`` returns). This file starts a real server with the repo's own
migrations, writes a real collaboration log through the durable sink, adds pre-fix forgeries, and runs the repair.
It skips when no PostgreSQL binaries are installed (the acceptance config lists the same bin dirs).
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import tempfile
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.bridge import PostgresSource  # noqa: E402
from herdr_engineering.control_plane.collab import (  # noqa: E402
    CollabService,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.collab_repair import repair_postgres  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from herdr_engineering.control_plane.sink import DurableEventSink  # noqa: E402
from tests.control_plane.test_collab import _digest  # noqa: E402
from tests.control_plane.test_collab_tasks import USERS, W  # noqa: E402
from tests.e2e.pg import ThrowawayPostgres  # noqa: E402

BIN_DIRS = [Path(f"/usr/lib/postgresql/{v}/bin") for v in (18, 17, 16, 15, 14)]


def _bins():
    for d in BIN_DIRS:
        if (d / "initdb").exists() and (d / "pg_ctl").exists():
            return d
    return None


pytestmark = pytest.mark.skipif(_bins() is None, reason="no PostgreSQL binaries installed")


@pytest.fixture(scope="module")
def pg():
    root = Path(tempfile.mkdtemp(prefix="t16-repair-pg-"))
    db = ThrowawayPostgres(_bins(), root).start()
    try:
        yield db
    finally:
        db.stop()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(db.sock, ignore_errors=True)


def _run(dsn, fn):
    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def _start(dsn):
    sink = DurableEventSink(dsn=dsn)
    repo = ControlPlaneRepo(sink=sink)
    collab = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    srv = ControlPlaneServer(repo, collab=collab, source=PostgresSource(dsn=dsn), allow_open=True)
    return srv, repo, collab, sink


def _forge(dsn, real_max):
    """Pre-fix forgeries, written exactly the way the v1 ingest path stored them (repo.record_event, durable)."""
    repo = ControlPlaneRepo(sink=DurableEventSink(dsn=dsn))
    shape = {"org_id": "default", "correlation_id": "c-forged", "created_at": "2026-01-01T00:00:00+00:00"}
    body = {"room_id": "r", "subject_user_id": "mallory", "role": "owner", "actor_user_id": "alice", "project_id": "x"}
    for i in range(real_max + 2):                       # sequences 0 .. real_max + 1 on the collab/log stream
        repo.record_event("collab", "log", "member_added", {**body, **(shape if i == real_max + 1 else {})})
    repo.record_event("collab", "elsewhere", "member_added", body)       # a foreign stream
    repo.record_event("mission", "m1", "mission.created", {"n": 1})      # an unrelated row that must not move


def test_collab_repair_apply_on_real_postgres_moves_forged_rows_and_nothing_else(pg):
    dsn = pg.dsn
    # a real log, through the real durable sink
    sink = DurableEventSink(dsn=dsn)
    c = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    W(c)
    real = len(c._log)
    assert real > 5
    _forge(dsn, real)

    async def counts(conn):
        return {
            "collab": await conn.fetchval("SELECT count(*) FROM fabric_events WHERE entity_type = 'collab'"),
            "other": await conn.fetchval("SELECT count(*) FROM fabric_events WHERE entity_type <> 'collab'"),
            "ids": sorted(r["event_id"] for r in await conn.fetch("SELECT event_id FROM fabric_events")),
            "quarantine": await conn.fetchval(
                "SELECT count(*) FROM fabric_events_quarantine") if await conn.fetchval(
                "SELECT to_regclass('fabric_events_quarantine')") else 0,
        }
    before = _run(dsn, counts)
    assert before["collab"] > real + 2

    # dry run (the default): reports, changes nothing, and names the rows it would move
    dry = _run(dsn, lambda conn: repair_postgres(conn))
    assert dry["mode"] == "dry-run" and dry["quarantine"] and dry["keep"] == real
    reasons = {q["reason"] for q in dry["quarantine"]}
    assert reasons == {"foreign_stream", "not_collab_schema", "timestamp_mismatch"}
    assert _run(dsn, counts) == before

    with pytest.raises(PermissionError):
        _run(dsn, lambda conn: repair_postgres(conn, apply=True))
    assert _run(dsn, counts) == before

    # THE FIX: this raised "list or tuple expected (got str)" on a real server
    done = _run(dsn, lambda conn: repair_postgres(conn, apply=True, confirmed=True))
    moved = len(done["quarantine"])
    after = _run(dsn, counts)
    assert moved == len(dry["quarantine"]) and after["collab"] == real
    assert after["other"] == before["other"]                         # the unrelated row did not move
    assert after["quarantine"] == moved                              # every moved row is kept in the quarantine table

    async def quarantined(conn):
        rows = await conn.fetch("SELECT event_id, quarantine_reason, quarantined_at, payload FROM "
                                "fabric_events_quarantine")
        live = {r["event_id"] for r in await conn.fetch("SELECT event_id FROM fabric_events")}
        return rows, live
    rows, live = _run(dsn, quarantined)
    assert {r["quarantine_reason"] for r in rows} == reasons and all(r["quarantined_at"] for r in rows)
    assert not ({r["event_id"] for r in rows} & live)
    assert all(json.loads(r["payload"]) is not None for r in rows)    # the payload came across whole
    # nothing was deleted: live + quarantined is exactly what was there before
    assert sorted(live | {r["event_id"] for r in rows}) == before["ids"]

    # idempotent: a second apply finds nothing and changes nothing
    again = _run(dsn, lambda conn: repair_postgres(conn, apply=True, confirmed=True))
    assert again["quarantine"] == [] and _run(dsn, counts) == after

    # and the control plane starts clean on the repaired table (no quarantine at startup, collaboration up)
    srv, repo, collab, _ = _start(dsn)
    try:
        assert not collab.degraded and len(collab._log) == real
        assert repo.collab_quarantine == []
        assert copy.deepcopy(collab._rooms)                          # state was rebuilt from the repaired log
    finally:
        srv.shutdown()


def test_the_startup_path_alone_quarantines_forgeries_on_real_postgres_without_the_repair_command(pg):
    """Before anyone runs the command, the same table already starts cleanly: forged rows are not replayed."""
    dsn = pg.dsn

    async def reset(conn):
        await conn.execute("TRUNCATE fabric_events")
        await conn.execute("DROP TABLE IF EXISTS fabric_events_quarantine")
    _run(dsn, reset)
    sink = DurableEventSink(dsn=dsn)
    c = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    W(c)
    real = len(c._log)
    _forge(dsn, real)
    srv, repo, collab, _ = _start(dsn)
    try:
        assert not collab.degraded and len(collab._log) == real
        assert repo.collab_quarantine and not any(r.get("source") for r in repo.collab_quarantine)
    finally:
        srv.shutdown()


def test_repair_on_a_database_without_the_envelope_columns_says_to_migrate_first(pg):
    from herdr_engineering.control_plane.collab_repair import SchemaTooOldError

    async def make(conn):
        await conn.execute("DROP DATABASE IF EXISTS old_schema")
        await conn.execute("CREATE DATABASE old_schema")
    admin = pg.dsn.replace("/" + pg.db + "?", "/postgres?")
    _run(admin, make)
    old = pg.dsn.replace("/" + pg.db + "?", "/old_schema?")

    async def table(conn):
        await conn.execute("CREATE TABLE fabric_events (event_id text, entity_type text, entity_id text, "
                           "sequence bigint, event_type text, payload jsonb, ingest_timestamp timestamptz)")
    _run(old, table)
    for kw in ({}, {"apply": True, "confirmed": True}):
        with pytest.raises(SchemaTooOldError, match="migrate"):
            _run(old, lambda conn, kw=kw: repair_postgres(conn, **kw))

    async def none(conn):
        await conn.execute("DROP TABLE fabric_events")
    _run(old, none)
    with pytest.raises(SchemaTooOldError, match="migrate"):
        _run(old, lambda conn: repair_postgres(conn))


def test_apply_locks_the_table_against_writers_for_its_transaction(pg):
    """A concurrent INSERT must wait until the repair's transaction ends (SHARE ROW EXCLUSIVE conflicts with writers)."""
    dsn = pg.dsn

    async def go():
        a, b = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
        try:
            await a.execute("TRUNCATE fabric_events")
            tx = a.transaction()
            await tx.start()
            await a.execute("LOCK TABLE fabric_events IN SHARE ROW EXCLUSIVE MODE")
            blocked = asyncio.ensure_future(b.execute(
                "INSERT INTO fabric_events (event_id, entity_type, entity_id, sequence, event_type, payload, "
                "source_timestamp, ingest_timestamp) VALUES ('w1','x','y',0,'t','{}', now(), now())"))
            await asyncio.sleep(0.5)
            waited = not blocked.done()
            await tx.commit()
            await asyncio.wait_for(blocked, 5)
            return waited
        finally:
            await a.close()
            await b.close()
    assert asyncio.run(go()) is True
