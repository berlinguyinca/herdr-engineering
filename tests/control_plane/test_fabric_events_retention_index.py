"""The 5-minute token_usage retention sweep deletes fabric_events by
(entity_type, source_timestamp); a covering index keeps that off a sequential scan.

The static tests always run. The EXPLAIN test needs a throwaway Postgres, given the
same way as test_e2e_live_db (``HERDR_E2E_PG_DSN``); it works in its own schema and
drops it afterwards.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane.retention import USAGE_ENTITY_TYPE
from herdr_engineering.control_plane.sink import DurableEventSink, prune_events_sql

_MIGRATIONS = Path(__file__).parents[2] / "deploy" / "control-plane" / "migrations"
_INDEX = "idx_fabric_events_type_source_ts"
_PG_DSN = os.environ.get("HERDR_E2E_PG_DSN")


def _index_migrations():
    return [p for p in sorted(_MIGRATIONS.glob("*.sql")) if _INDEX in p.read_text()]


def test_a_migration_indexes_fabric_events_by_type_and_source_timestamp():
    found = _index_migrations()
    assert len(found) == 1, "exactly one migration creates the retention index"
    sql = re.sub(r"\s+", " ", found[0].read_text())
    assert re.search(rf"CREATE INDEX IF NOT EXISTS {_INDEX} ON fabric_events ?"
                     r"\(entity_type, source_timestamp\)", sql)
    # sorts after the schema it depends on
    assert found[0].name > "0001_control_plane.sql"


def test_prune_uses_the_shared_statement():
    sql = prune_events_sql("fabric_events")
    assert sql == "DELETE FROM fabric_events WHERE entity_type=$1 AND source_timestamp < $2"

    calls = []

    class _Conn:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    sink = DurableEventSink(conn=_Conn(), run=asyncio.run)
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    sink.prune_events_before(USAGE_ENTITY_TYPE, cutoff)
    assert calls == [(sql, (USAGE_ENTITY_TYPE, cutoff))]


@pytest.mark.skipif(not _PG_DSN, reason="set HERDR_E2E_PG_DSN to a throwaway Postgres")
def test_retention_delete_uses_the_index_on_200k_rows():
    asyncpg = pytest.importorskip("asyncpg")
    from herdr_engineering.control_plane.db import Migrator

    schema = f"retention_ix_{secrets.token_hex(4)}"

    async def scenario():
        conn = await asyncpg.connect(_PG_DSN)
        try:
            await conn.execute(f"CREATE SCHEMA {schema}")
            await conn.execute(f"SET search_path TO {schema}")
            await Migrator(conn).apply(str(_MIGRATIONS))
            # 200k events over the last 14 days, 1 in 5 token_usage. Steady state: the
            # sweep runs every 5 minutes, so only ~5 minutes of rows are past the cutoff.
            await conn.execute("""
                INSERT INTO fabric_events (event_id, sequence, event_type, entity_type,
                                           entity_id, source_timestamp)
                SELECT 'ev_' || g, g, 'x.recorded',
                       (ARRAY['token_usage','session','host','mission','service'])[1 + g % 5],
                       'e' || (g % 500),
                       now() - interval '14 days 5 minutes' + (g * interval '6 seconds')
                FROM generate_series(1, 200000) g
            """)
            await conn.execute("ANALYZE fabric_events")
            cutoff = datetime.now(UTC) - timedelta(days=14)
            sql = prune_events_sql("fabric_events")
            plan = "\n".join(r[0] for r in await conn.fetch(
                "EXPLAIN " + sql, USAGE_ENTITY_TYPE, cutoff))
            tr = conn.transaction()
            await tr.start()
            status = await conn.execute(sql, USAGE_ENTITY_TYPE, cutoff)
            await tr.rollback()
            return plan, status
        finally:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.close()

    plan, status = asyncio.run(scenario())
    assert _INDEX in plan, plan
    assert "Seq Scan" not in plan, plan
    assert status.startswith("DELETE ") and int(status.split()[1]) > 0
