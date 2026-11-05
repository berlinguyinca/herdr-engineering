"""G9: the durable Pi activity store on a REAL PostgreSQL (private throwaway cluster, same helper as the recovery tests):
write through DurableEventSink -> restart (PostgresSource + load_into, the production recovery path) -> read -> prune.
Skips like the other Postgres tests when PostgreSQL server binaries or asyncpg are missing. No mocks."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.bridge import PostgresSource, load_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.mission_activity import ENTITY_TYPE, RETENTION_SECONDS
from herdr_engineering.control_plane.mission_outcome import issue_refs_by_mission, pr_refs_by_mission
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.e2e.pg import ThrowawayPostgres, find_bins

asyncpg = pytest.importorskip("asyncpg")


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    bins = find_bins({})
    if bins is None:
        pytest.skip("PostgreSQL server binaries are required")
    db = ThrowawayPostgres(bins, tmp_path_factory.mktemp("activity-pg")).start()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db(pg):
    pg.query("TRUNCATE fabric_events")
    return pg


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def snapshot(key, at, **mission):
    return {"entity_type": "mission_activity", "entity_id": f"pi:{key}", "source": "pi", "event_type": "mission_activity.snapshot",
            "payload": {"contract_version": 1, "host_id": "h", "instance_id": "i", "session_id": "s", "observed_at": stamp(at),
                        "snapshot_at": stamp(at), "mission_revision": 1, "connection": "connected",
                        "mission": {"id": key, "title": f"Title {key}", "status": "RUNNING", **mission},
                        "activity": {"health": "active", "nextAction": "Run the integration suite"}}}


def rows(db):
    return [dict(r) for r in db.query("SELECT entity_type, entity_id, source_timestamp FROM fabric_events "
                                      "WHERE entity_type=$1 ORDER BY entity_id", ENTITY_TYPE)]


def restart(db, clock):
    """A brand-new repo that sees only what PostgreSQL holds, through the production recovery source."""
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    try:
        repo = ControlPlaneRepo(now=lambda: clock[0])
        load_into(repo, PostgresSource(conn, run=loop.run_until_complete))
        return repo
    finally:
        loop.run_until_complete(conn.close())
        loop.close()


def test_write_restart_read_then_prune_on_real_postgres(db):
    t = time.time()
    clock = [t - 2 * RETENTION_SECONDS]
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    try:
        repo = ControlPlaneRepo(now=lambda: clock[0], sink=DurableEventSink(conn=conn, run=loop.run_until_complete))
        service = IngestService(repo)
        # An expired row, written 48 h ago by the same store (the first persist also arms the hourly prune).
        service.ingest(snapshot("MSN-OLD", clock[0]))
        assert [r["entity_id"] for r in rows(db)] == ["pi:MSN-OLD"]

        # Now: a fresh mission with v4 fields. The first persist after an hour prunes the expired row.
        clock[0] = t
        service.ingest(snapshot("MSN-NEW", t, created_at=stamp(t - 600),
                                links=[{"kind": "issue", "repository": "acme/app", "number": 5},
                                       {"kind": "pull_request", "repository": "acme/app", "number": 9}]))
        stored = rows(db)
        assert [r["entity_id"] for r in stored] == ["pi:MSN-NEW"], "the 48 h old row was not pruned from the real table"
    finally:
        loop.run_until_complete(conn.close())
        loop.close()

    # Restart: nothing in memory, only PostgreSQL.
    clock[0] = t + 12
    again = restart(db, clock)
    restored = again.mission_activity.get("pi:MSN-NEW")
    assert restored is not None
    assert restored["activity"]["nextAction"] == "Run the integration suite"
    assert restored["mission"]["created_at"] == stamp(t - 600)
    assert restored["mission"]["links"] == [{"kind": "issue", "repository": "acme/app", "number": 5},
                                            {"kind": "pull_request", "repository": "acme/app", "number": 9}]
    assert restored["freshness"]["age_seconds"] == 12.0
    assert again.mission_activity.get("pi:MSN-OLD") is None
    assert [s["mission"]["id"] for s in again.mission_activity.list_summaries()] == ["MSN-NEW"]
    # The private stream stays out of the shared event streams after recovery.
    assert not any(key[0] == ENTITY_TYPE for key in again._events)

    assert issue_refs_by_mission(again) == {"pi:MSN-NEW": [{"repository": "acme/app", "number": 5}]}
    assert pr_refs_by_mission(again) == {"pi:MSN-NEW": [{"repository": "acme/app", "number": 9}]}


def test_an_expired_row_already_in_postgres_is_ignored_on_restart_even_if_never_pruned(db):
    t = time.time()
    clock = [t - 2 * RETENTION_SECONDS]
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    try:
        repo = ControlPlaneRepo(now=lambda: clock[0], sink=DurableEventSink(conn=conn, run=loop.run_until_complete))
        IngestService(repo).ingest(snapshot("MSN-OLD", clock[0]))
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    assert len(rows(db)) == 1
    clock[0] = t
    assert restart(db, clock).mission_activity.list_summaries() == []
