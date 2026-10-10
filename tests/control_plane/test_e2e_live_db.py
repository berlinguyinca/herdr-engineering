"""Live-Postgres end-to-end test (opt-in; requires a running Postgres).

This is the only suite that touches a real database. It is **not** part of the
default CI run: the module is skipped unless the caller explicitly points it at
a throwaway Postgres via ``HERDR_E2E_PG_DSN`` (and asyncpg is importable).

It exercises the durable round-trip exactly the way the deployed stack does:

1. **migrations**  - ``Migrator`` applies ``deploy/control-plane/migrations``
   against the live DB (this is what the ``control-plane worker`` does at
   startup with ``--migrations``).
2. **durable write** - rows are inserted into ``missions`` / ``sessions`` /
   ``services`` / ``hosts`` / ``fabric_events`` (the shape the fabric gateway
   writes on behalf of agents; the network hop itself is out of scope here).
3. **bridge**      - ``PostgresSource`` + ``load_into`` hydrate a fresh
   ``ControlPlaneRepo`` from those tables (what the web does at startup).
4. **web**         - a real ``ControlPlaneServer`` boots against the same DSN
   and serves the hydrated state over ``/api/v1``, with ``/health`` reporting
   ``source: postgres``.

Teardown drops the schema, so a dedicated test database stays reusable and
the real ``herdr`` database is never touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.request

import pytest

from herdr_engineering.control_plane.bridge import PostgresSource, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo

_PG_DSN = os.environ.get("HERDR_E2E_PG_DSN")

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "deploy", "control-plane", "migrations"
)

pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="set HERDR_E2E_PG_DSN to a throwaway Postgres (e.g. "
           "postgresql://user:pass@host:port/herdr_e2e) to run the live e2e test",
)


class _LoopRunner:
    """Run async work on one persistent event loop (daemon thread).

    asyncpg connections are bound to the loop they were created on, so
    connect + all subsequent queries must share a single loop — the same
    constraint the ``bridge.PostgresSource`` already handles.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="herdr-e2e-pg-loop")
        self._thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


_RUN = _LoopRunner().run


async def _connect(dsn):
    import asyncpg  # lazy: only needed when the test actually runs
    return await asyncpg.connect(dsn)


async def _apply_migrations(conn) -> list[str]:
    from herdr_engineering.control_plane.db import Migrator
    return await Migrator(conn).apply(_MIGRATIONS_DIR)


async def _seed(conn) -> None:
    await conn.execute(
        "INSERT INTO hosts (host_id, hostname, tailnet_ip, status) "
        "VALUES ($1, $2, $3, $4)",
        "h1", "bender", "100.64.1.1", "healthy",
    )
    await conn.execute(
        "INSERT INTO missions (mission_id, title, purpose, status) "
        "VALUES ($1, $2, $3, $4)",
        "m1", "e2e mission", "round-trip", "active",
    )
    await conn.execute(
        "INSERT INTO sessions (session_id, mission_id, agent_role, model, status) "
        "VALUES ($1, $2, $3, $4, $5)",
        "s1", "m1", "coder", "gpt-4", "active",
    )
    await conn.execute(
        "INSERT INTO services (service_id, service_name, host_id, port, health) "
        "VALUES ($1, $2, $3, $4, $5)",
        "sv1", "api", "h1", 8080, "healthy",
    )
    await conn.execute(
        "INSERT INTO fabric_events "
        "(event_id, sequence, event_type, entity_type, entity_id, mission_id, "
        " session_id, payload, source_timestamp) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())",
        "e1", 0, "MissionCreated", "mission", "m1", "m1", None,
        json.dumps({"title": "e2e mission"}),
    )
    await conn.execute(
        "INSERT INTO fabric_events "
        "(event_id, sequence, event_type, entity_type, entity_id, mission_id, "
        " session_id, payload, source_timestamp) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())",
        "e2", 1, "MissionStageChanged", "mission", "m1", "m1", None,
        json.dumps({"stage": "implementation"}),
    )


@pytest.fixture(scope="module")
def seeded_db():
    """Reset the target database, apply migrations, and seed durable rows."""
    conn = _RUN(_connect(_PG_DSN))
    try:
        _RUN(conn.execute("DROP SCHEMA IF EXISTS public CASCADE"))
        _RUN(conn.execute("CREATE SCHEMA public"))
        applied = _RUN(_apply_migrations(conn))
        assert applied, "expected at least one migration to be applied"
        _RUN(_seed(conn))
    finally:
        _RUN(conn.close())
    yield
    # Leave the dedicated test database clean for reuse.
    conn2 = _RUN(_connect(_PG_DSN))
    try:
        _RUN(conn2.execute("DROP SCHEMA IF EXISTS public CASCADE"))
        _RUN(conn2.execute("CREATE SCHEMA public"))
    finally:
        _RUN(conn2.close())


def test_migrations_applied_and_round_trip_hydration(seeded_db):
    """Migrations ran; PostgresSource -> load_into reproduces the durable rows."""
    repo = ControlPlaneRepo()
    source = PostgresSource(dsn=_PG_DSN)
    counts = load_into(repo, source)
    assert counts == {"missions": 1, "sessions": 1, "services": 1,
                      "hosts": 1, "events": 2}

    m = repo.get_mission("m1")
    assert m["title"] == "e2e mission"
    assert m["sessions"] == ["s1"]
    s = repo.get_session("s1")
    assert s["model"] == "gpt-4" and s["agent_role"] == "coder"
    h = repo.get_host("h1")
    assert h["host_name"] == "bender" and h["tailnet_ip"] == "100.64.1.1"
    sv = repo.get_service("sv1")
    assert sv["url"] == "http://h1:8080"

    stream = repo.stream("mission", "m1")
    assert [e["event_type"] for e in stream] == ["MissionCreated",
                                                 "MissionStageChanged"]
    assert [e["sequence"] for e in stream] == [0, 1]
    assert repo.recent_events(limit=1)[0]["event_type"] == "MissionStageChanged"


def test_durable_sink_ingest_survives_restart(seeded_db):
    """Ingesting via the sink persists to Postgres; a fresh repo (a simulated
    web restart) re-hydrates the exact same entity from the durable log."""
    from herdr_engineering.control_plane.ingest import IngestService
    from herdr_engineering.control_plane.sink import DurableEventSink

    live = ControlPlaneRepo(sink=DurableEventSink(dsn=_PG_DSN))
    IngestService(live).ingest({
        "entity_type": "host", "entity_id": "e2e-ingest-host",
        "event_type": "host.registered",
        "payload": {"host_name": "ingest-host", "tailnet_ip": "100.64.9.9"}})
    IngestService(live).ingest({
        "entity_type": "host", "entity_id": "e2e-ingest-host",
        "event_type": "host.heartbeat",
        "payload": {"cpu": 7, "mem": 33}})

    # Simulate a web restart: a brand-new repo hydrated from Postgres alone.
    fresh = ControlPlaneRepo()
    counts = load_into(fresh, PostgresSource(dsn=_PG_DSN))
    h = fresh.get_host("e2e-ingest-host")
    assert h is not None
    assert h["host_name"] == "ingest-host"
    assert h["cpu"] == 7
    assert h["tailnet_ip"] == "100.64.9.9"
    # Both ingested events made it into the durable event log.
    stream = fresh.stream("host", "e2e-ingest-host")
    assert [e["event_type"] for e in stream] == [
        "HostRegistered", "HostHeartbeat"]
    assert counts["events"] >= 2


def test_web_serves_postgres_hydrated_state(seeded_db):
    """A real ControlPlaneServer reads the durable state and serves it over HTTP."""
    from herdr_engineering.control_plane.api import ControlPlaneServer

    repo = ControlPlaneRepo()
    source = PostgresSource(dsn=_PG_DSN)
    svr = ControlPlaneServer(repo, source=source)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{svr.port}/api/v1/missions") as resp:
            body = resp.read().decode()
        assert '"mission_id": "m1"' in body
        assert '"title": "e2e mission"' in body

        with urllib.request.urlopen(
                f"http://127.0.0.1:{svr.port}/api/v1/health") as resp:
            health = resp.read().decode()
        assert '"source": "postgres"' in health
        assert '"version"' in health
    finally:
        svr.shutdown()
