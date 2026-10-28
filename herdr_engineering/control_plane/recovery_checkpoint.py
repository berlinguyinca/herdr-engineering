"""Offline, atomic recovery checkpoints; online reads scan keys, not old payloads.

The current journal is authoritative, including retention and quarantine. A generation contains the
original rows selected by faithful recovery plus the exact (actor_id, event_id)
set visible in the SAME repeatable-read snapshot. No clock, stream sequence or
transaction-start watermark is used. A late commit, including one with backdated
clocks or a reused event ID in another actor domain, is therefore always a tail
row. Prior generations remain available to readers holding older MVCC snapshots.
"""
from __future__ import annotations

import time
import uuid

FORMAT_VERSION = 2
_BUILD_LOCK = 0x48455244524350  # HERDRCP: serialize builders, never journal writers.


class CheckpointUnavailable(RuntimeError):
    """Checkpoint schema is missing; apply migration 0015 before building."""


class CheckpointBuildInProgress(RuntimeError):
    """Another offline builder owns the checkpoint publication lock."""


async def _available(conn) -> bool:
    rows = await conn.fetch("""
        SELECT to_regclass('recovery_checkpoint_manifests') IS NOT NULL
           AND to_regclass('recovery_checkpoint_rows') IS NOT NULL
           AND to_regclass('recovery_checkpoint_covered') IS NOT NULL
           AND to_regclass('recovery_checkpoint_active') IS NOT NULL AS available
    """)
    return bool(rows and rows[0]["available"])


def checkpoint_events_sql() -> str:
    """Checkpoint rows + uncovered identities, then indexed tail payload lookups.

    MATERIALIZED is deliberate: the anti-join must finish on narrow covered keys
    before looking up payloads. OFFSET 0 keeps the parameterized payload lookup
    from being flattened into another whole-journal heap scan. The covering
    identity index in migration 0015 supports the raw-key side of the anti-join;
    MVCC visibility checks can still require heap visits for newly written pages.
    Retention/quarantine removes saved rows too. Deleting a compacted boundary or
    inserting a backdated barrier can expose omitted interior snapshots, so those
    individual streams replay from the current journal without compaction.
    Caller supplies one checkpoint ID and holds a repeatable-read transaction.
    """
    return """
        WITH tail_keys AS MATERIALIZED (
            SELECT COALESCE(e.actor ->> 'id', '') AS actor_id, e.event_id
            FROM fabric_events e
            WHERE NOT EXISTS (
                SELECT 1 FROM recovery_checkpoint_covered c
                WHERE c.checkpoint_id = $1
                  AND c.actor_id = COALESCE(e.actor ->> 'id', '')
                  AND c.event_id = e.event_id
            )
        ), deleted_keys AS MATERIALIZED (
            SELECT c.actor_id, c.event_id, c.entity_type, c.entity_id
            FROM recovery_checkpoint_covered c
            WHERE c.checkpoint_id = $1 AND NOT EXISTS (
                SELECT 1 FROM fabric_events e
                WHERE COALESCE(e.actor ->> 'id', '') = c.actor_id AND e.event_id = c.event_id
            )
        ), saved_events AS MATERIALIZED (
            SELECT original.* FROM recovery_checkpoint_rows saved
            CROSS JOIN LATERAL jsonb_populate_record(NULL::fabric_events, saved.event_row) original
            WHERE saved.checkpoint_id = $1
        ), tail_events AS MATERIALIZED (
            SELECT original.* FROM tail_keys k
            CROSS JOIN LATERAL (
                SELECT e.* FROM fabric_events e
                WHERE COALESCE(e.actor ->> 'id', '') = k.actor_id AND e.event_id = k.event_id
                OFFSET 0
            ) original
        ), stream_ends AS (
            SELECT DISTINCT ON (entity_type, entity_id)
                entity_type, entity_id, ingest_timestamp, sequence, event_id
            FROM saved_events WHERE entity_type IN ('service', 'session', 'host')
            ORDER BY entity_type, entity_id, ingest_timestamp DESC, sequence DESC, event_id DESC
        ), changed_streams AS MATERIALIZED (
            SELECT entity_type, entity_id FROM deleted_keys
            WHERE entity_type IN ('service', 'session', 'host')
            UNION
            SELECT t.entity_type, t.entity_id FROM tail_events t
            JOIN stream_ends b USING (entity_type, entity_id)
            WHERE (t.ingest_timestamp, t.sequence, t.event_id)
                <= (b.ingest_timestamp, b.sequence, b.event_id)
        )
        SELECT original.*, 'saved' AS recovery_origin FROM saved_events original
        WHERE NOT EXISTS (
            SELECT 1 FROM deleted_keys d
            WHERE d.actor_id = COALESCE(original.actor ->> 'id', '') AND d.event_id = original.event_id
        ) AND NOT EXISTS (
            SELECT 1 FROM changed_streams s
            WHERE s.entity_type = original.entity_type AND s.entity_id = original.entity_id
        )
        UNION ALL
        SELECT original.*, 'tail' AS recovery_origin FROM tail_events original
        WHERE NOT EXISTS (
            SELECT 1 FROM changed_streams s
            WHERE s.entity_type = original.entity_type AND s.entity_id = original.entity_id
        )
        UNION ALL
        SELECT original.*, CASE WHEN EXISTS (
            SELECT 1 FROM recovery_checkpoint_covered c WHERE c.checkpoint_id = $1
                AND c.actor_id = COALESCE(original.actor ->> 'id', '') AND c.event_id = original.event_id
        ) THEN 'refold' ELSE 'tail' END AS recovery_origin
        FROM changed_streams s
        CROSS JOIN LATERAL (
            SELECT e.* FROM fabric_events e
            WHERE e.entity_type = s.entity_type AND e.entity_id = s.entity_id
            OFFSET 0
        ) original
        ORDER BY ingest_timestamp, sequence, event_id
    """


async def load_checkpoint_events(conn):
    """Return (original event rows, sanitized metrics), or None for full fallback.

    Called inside PostgresSource's read-only repeatable-read snapshot so pointer,
    selected rows, coverage and tail are consistent even across publication.
    """
    started = time.monotonic()
    if not await _available(conn):
        return None
    active = await conn.fetch("""
        SELECT m.checkpoint_id, m.covered_events, m.selected_events
        FROM recovery_checkpoint_active a
        JOIN recovery_checkpoint_manifests m USING (checkpoint_id)
        WHERE a.singleton AND m.ready AND m.format_version = $1
    """, FORMAT_VERSION)
    if not active:
        return None
    manifest = active[0]
    rows = [dict(row) for row in await conn.fetch(checkpoint_events_sql(), manifest["checkpoint_id"])]
    origins = [row.pop("recovery_origin") for row in rows]
    tail_count = origins.count("tail")
    return rows, {
        "mode": "checkpoint",
        "checkpoint_id": manifest["checkpoint_id"],
        "format_version": FORMAT_VERSION,
        "covered_events": manifest["covered_events"],
        "checkpoint_events": len(rows) - tail_count,
        "refold_events": origins.count("refold"),
        "tail_events": tail_count,
        "loaded_events": len(rows),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


async def build_checkpoint(conn) -> dict:
    """Build/publish one generation on an explicitly supplied writable connection.

    This is an offline/operator operation, never invoked by startup. Only the
    checkpoint tables are written; writers can continue appending journal rows.
    A failed build rolls back everything, including publication. No automatic
    pruning: existing readers may still reference the prior generation.
    Returned diagnostics intentionally contain no DSN, actor IDs or payloads.
    """
    from .bridge import _recovery_events_sql

    started = time.monotonic()
    checkpoint_id = "recovery_" + uuid.uuid4().hex
    async with conn.transaction(isolation="repeatable_read"):
        if not await _available(conn):
            raise CheckpointUnavailable("apply migration 0015 before building a recovery checkpoint")
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", _BUILD_LOCK):
            raise CheckpointBuildInProgress("a recovery checkpoint build is already running")
        await conn.execute("""
            INSERT INTO recovery_checkpoint_manifests (checkpoint_id, format_version)
            VALUES ($1, $2)
        """, checkpoint_id, FORMAT_VERSION)
        covered = await conn.execute("""
            INSERT INTO recovery_checkpoint_covered (checkpoint_id, actor_id, event_id, entity_type, entity_id)
            SELECT $1, COALESCE(actor ->> 'id', ''), event_id, entity_type, entity_id FROM fabric_events
        """, checkpoint_id)
        selected = await conn.execute("""
            INSERT INTO recovery_checkpoint_rows (checkpoint_id, actor_id, event_id, event_row)
            SELECT $1, COALESCE(original.actor ->> 'id', ''), original.event_id, to_jsonb(original)
            FROM (
        """ + _recovery_events_sql("fabric_events") + ") original", checkpoint_id)
        covered_count, selected_count = int(covered.split()[-1]), int(selected.split()[-1])
        await conn.execute("""
            UPDATE recovery_checkpoint_manifests
            SET covered_events=$2, selected_events=$3, ready=true WHERE checkpoint_id=$1
        """, checkpoint_id, covered_count, selected_count)
        # The pointer and all preceding writes become visible in one commit.
        await conn.execute("""
            INSERT INTO recovery_checkpoint_active (singleton, checkpoint_id) VALUES (true, $1)
            ON CONFLICT (singleton) DO UPDATE SET checkpoint_id = EXCLUDED.checkpoint_id
        """, checkpoint_id)
    return {
        "mode": "built", "checkpoint_id": checkpoint_id, "format_version": FORMAT_VERSION,
        "covered_events": covered_count, "selected_events": selected_count,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main(argv=None) -> int:
    """Explicit offline builder; credentials stay in memory and errors are bounded."""
    import argparse
    import asyncio
    import json
    import sys
    from pathlib import Path
    from urllib.parse import urlsplit, urlunsplit

    import asyncpg

    from herdr_engineering.fleet_config import load_fleet_config

    parser = argparse.ArgumentParser(description="Build an atomic recovery checkpoint without restarting servers")
    parser.add_argument("--fleet-config", type=Path,
                        default=Path.home() / ".config/herdr-engineering/fleet/fleet-config.yaml")
    parser.add_argument("--local-port", type=int, help="Use localhost at this published PostgreSQL port")
    parser.add_argument("--timeout", type=float, default=1800, help="Offline SQL timeout in seconds")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or (args.local_port is not None and not 1 <= args.local_port <= 65535):
        parser.error("timeout must be positive and local port must be between 1 and 65535")

    async def run():
        dsn = load_fleet_config(args.fleet_config).section("control_plane")["pg_dsn"]
        if args.local_port is not None:
            parts = urlsplit(dsn)
            credentials = parts.netloc.rpartition("@")[0] + "@" if "@" in parts.netloc else ""
            dsn = urlunsplit((parts.scheme, f"{credentials}127.0.0.1:{args.local_port}",
                              parts.path, parts.query, parts.fragment))
        conn = await asyncpg.connect(dsn, command_timeout=args.timeout)
        try:
            return await build_checkpoint(conn)
        finally:
            await conn.close()

    try:
        print(json.dumps(asyncio.run(run()), sort_keys=True))
    except Exception as error:
        print(f"Recovery checkpoint build failed ({type(error).__name__}); previous generation preserved",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
