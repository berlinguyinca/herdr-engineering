"""Moves the live ``fabric_events`` journal onto a partitioned table in explicit, resumable, operator-driven steps.

Migration 0053 only PREPARES ``fabric_events_part`` (LIST on ``entity_type``: an ``inspector`` partition and a default
partition). Nothing in this module runs automatically; every step is a CLI call and every step refuses unless
``control_plane.fabric_partitioning.enabled`` is true in the fleet YAML (default false).

* ``backfill``: copies rows into the prepared table in keyset batches over the live unique key
  ``(COALESCE(actor->>'id',''), event_id)``, one short transaction per batch, with a pause in between. Triggers are not
  attached to the new table, so ``projection_position`` and ``recovery_instance`` are copied verbatim and nothing is logged
  to ``projection_event_changes``. Resumable: the cursor lives in ``fabric_events_partition_state``. The live table is only read.
* ``cutover``: ONE transaction under ``lock_timeout``. It takes ACCESS EXCLUSIVE on the live table, catches the new table up
  (rows whose ``projection_position`` is above the backfill's start mark, minus ``D`` changes from the change log), verifies
  the row counts, renames the tables and indexes, and attaches the projection triggers to the partitioned table. If the lock
  is not granted in time, or anything differs, the transaction rolls back and nothing changed.
* ``rollback``: the mirror image. It carries every row written since cutover back into the untouched old table and renames
  everything back. The old table is kept (as ``fabric_events_unpartitioned``) until ``drop-old`` is run.

Why the catch-up is exact: every insert, update and delete of the live table takes a fresh ``projection_position`` (rows)
or logs a ``D`` change with one (deletes) under the shared writer fence. A ``T`` (truncate) since the mark aborts the step.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

POSITION_OVERLAP = 100_000   # catch-up reaches this far below the start mark: covers writers in flight when it was taken
_KEYS = ("enabled", "batch_rows", "pause_seconds", "lock_timeout_seconds", "verify_counts")
_TRIGGERS = (
    ("projection_position_before_write", "BEFORE INSERT OR UPDATE", "FOR EACH ROW",
     "fabric_events_part_assign_position"),
    ("projection_change_after_write", "AFTER INSERT OR UPDATE OR DELETE", "FOR EACH ROW",
     "fabric_events_part_record_change"),
    ("projection_change_after_truncate", "AFTER TRUNCATE", "FOR EACH STATEMENT",
     "fabric_events_part_record_truncate"),
    ("fabric_event_recovery_instance", "BEFORE INSERT OR UPDATE", "FOR EACH ROW",
     "set_fabric_event_recovery_instance"),
)


class PartitioningError(RuntimeError):
    """A step refused or aborted; the database is unchanged by the refused step (a backfill batch is atomic)."""


@dataclass(frozen=True)
class FabricPartitioningSettings:
    enabled: bool = False
    batch_rows: int = 5_000
    pause_seconds: float = 0.2
    lock_timeout_seconds: float = 5.0
    verify_counts: bool = True


def _number(section: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError(f"fabric_partitioning.{key} must be a number between {minimum:g} and {maximum:g}")
    return float(value)


def parse_fabric_partitioning(section: Any) -> FabricPartitioningSettings:
    """Validate ``control_plane.fabric_partitioning``; ValueError names the field, unknown keys are rejected."""
    if section is None:
        return FabricPartitioningSettings()
    if not isinstance(section, dict):
        raise ValueError("fabric_partitioning must be a mapping")
    unknown = sorted(str(k) for k in set(section) - set(_KEYS))
    if unknown:
        raise ValueError(f"fabric_partitioning: unknown key(s) {unknown} (allowed: {', '.join(_KEYS)})")
    for key in ("enabled", "verify_counts"):
        if key in section and not isinstance(section[key], bool):
            raise ValueError(f"fabric_partitioning.{key} must be a boolean (true or false)")
    return FabricPartitioningSettings(
        enabled=section.get("enabled", False),
        batch_rows=int(_number(section, "batch_rows", 5_000, 100, 100_000)),
        pause_seconds=_number(section, "pause_seconds", 0.2, 0, 60),
        lock_timeout_seconds=_number(section, "lock_timeout_seconds", 5, 0.5, 60),
        verify_counts=section.get("verify_counts", True))


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_COLUMNS = """
    SELECT a.attname AS name FROM pg_attribute a
    WHERE a.attrelid = to_regclass($1) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
"""
_KEY = "(COALESCE(actor ->> 'id', ''), event_id, entity_type)"


async def _relkind(conn, name: str) -> str | None:
    return await conn.fetchval("SELECT relkind::text FROM pg_class WHERE oid = to_regclass($1)", name)


async def _columns(conn, table: str) -> list[str]:
    return [r["name"] for r in await conn.fetch(_COLUMNS, table)]


def _require_enabled(settings: FabricPartitioningSettings) -> None:
    if not settings.enabled:
        raise PartitioningError("control_plane.fabric_partitioning.enabled is false; set it to true in the fleet YAML "
                                "to run this step")


async def _state(conn) -> dict:
    row = await conn.fetchrow("SELECT * FROM fabric_events_partition_state WHERE singleton")
    if row is None:
        raise PartitioningError("fabric_events_partition_state is missing; apply migration 0053")
    return dict(row)


async def _preflight(conn, *, forward: bool) -> None:
    live, other = ("fabric_events", "fabric_events_part") if forward else ("fabric_events", "fabric_events_unpartitioned")
    if await _relkind(conn, "fabric_events_part" if forward else "fabric_events_unpartitioned") is None:
        raise PartitioningError("the prepared table is missing (migration 0053 did nothing: PostgreSQL older than 15?)"
                                if forward else "fabric_events_unpartitioned is missing; nothing to roll back to")
    if await _columns(conn, live) != await _columns(conn, other):
        raise PartitioningError(f"{live} and {other} have different columns (a migration changed the journal after the "
                                "prepared table was created); drop the prepared table and re-prepare it")


async def status(conn) -> dict:
    """Read-only: the phase, row counts per table and what a cutover would refuse on."""
    state = await _state(conn)
    kinds = {name: await _relkind(conn, name) for name in
             ("fabric_events", "fabric_events_part", "fabric_events_unpartitioned")}
    counts = {}
    for name, kind in kinds.items():
        if kind:
            counts[name] = await conn.fetchval(f"SELECT count(*) FROM {_quote(name)}")
    if kinds["fabric_events"] == "p":
        parts = await conn.fetch(
            "SELECT c.relname AS partition, c.reltuples::bigint AS estimate FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = to_regclass('fabric_events') ORDER BY 1")
        counts["partitions"] = {r["partition"]: r["estimate"] for r in parts}
    return {"phase": state["phase"], "copied_rows": state["copied_rows"], "start_position": state["start_position"],
            "cutover_position": state["cutover_position"], "relkinds": kinds, "counts": counts}


async def backfill(conn, settings: FabricPartitioningSettings, *, max_batches: int | None = None,
                   sleep=asyncio.sleep) -> dict:
    """Copy live rows into the prepared table, resumable. Returns {copied, done, phase}."""
    _require_enabled(settings)
    state = await _state(conn)
    if state["phase"] == "partitioned":
        raise PartitioningError("already partitioned; nothing to backfill")
    if await _relkind(conn, "fabric_events") != "r":
        raise PartitioningError("fabric_events is not a plain table; the journal is already partitioned")
    await _preflight(conn, forward=True)
    columns = await _columns(conn, "fabric_events")
    col_list = ", ".join(_quote(c) for c in columns)

    if state["phase"] == "prepared" or state["start_position"] is None:
        # Writers allocate positions from this sequence; the backfill's later catch-up starts just below this mark.
        mark = await conn.fetchrow("SELECT last_value, is_called FROM projection_position_seq")
        start = mark["last_value"] if mark["is_called"] else 0
        await conn.execute("UPDATE fabric_events_partition_state SET phase='backfilling', start_position=$1, "
                           "updated_at=now() WHERE singleton", start)
        state = await _state(conn)

    first = ("SELECT {cols}, COALESCE(actor ->> 'id', '') AS _k1 FROM fabric_events "
             "ORDER BY COALESCE(actor ->> 'id', ''), event_id LIMIT $1")
    later = ("SELECT {cols}, COALESCE(actor ->> 'id', '') AS _k1 FROM fabric_events "
             "WHERE (COALESCE(actor ->> 'id', ''), event_id) > ($2::text, $3::text) "
             "ORDER BY COALESCE(actor ->> 'id', ''), event_id LIMIT $1")
    copied = batches = 0
    while max_batches is None or batches < max_batches:
        cursor = (state["cursor_actor"], state["cursor_event"])
        select = (first if cursor[0] is None else later).format(cols=col_list)
        args = (settings.batch_rows,) if cursor[0] is None else (settings.batch_rows, *cursor)
        sql = (f"WITH batch AS ({select}), "
               f"ins AS (INSERT INTO fabric_events_part ({col_list}) SELECT {col_list} FROM batch "
               "ON CONFLICT DO NOTHING RETURNING 1) "
               "SELECT (SELECT count(*) FROM batch) AS n, "
               "(SELECT _k1 FROM batch ORDER BY _k1 DESC, event_id DESC LIMIT 1) AS last_actor, "
               "(SELECT event_id FROM batch ORDER BY _k1 DESC, event_id DESC LIMIT 1) AS last_event")
        async with conn.transaction():
            got = await conn.fetchrow(sql, *args)
            n = got["n"]
            if n:
                await conn.execute(
                    "UPDATE fabric_events_partition_state SET cursor_actor=$1, cursor_event=$2, "
                    "copied_rows=copied_rows+$3, updated_at=now() WHERE singleton",
                    got["last_actor"], got["last_event"], n)
            else:
                await conn.execute("UPDATE fabric_events_partition_state SET phase='backfilled', updated_at=now() "
                                   "WHERE singleton")
        batches += 1
        if not n:
            return {"copied": copied, "done": True, "phase": "backfilled"}
        copied += n
        state = await _state(conn)
        if settings.pause_seconds:
            await sleep(settings.pause_seconds)
    return {"copied": copied, "done": False, "phase": state["phase"]}


async def _catch_up(conn, source: str, target: str, since: int) -> dict:
    """Make ``target`` hold what ``source`` holds for every change after ``since`` (call with ``source`` locked)."""
    truncated = await conn.fetchval(
        "SELECT count(*) FROM projection_event_changes WHERE operation = 'T' AND position > $1", since)
    if truncated:
        raise PartitioningError("the journal was truncated after the backfill started; re-prepare and start over")
    columns = await _columns(conn, source)
    col_list = ", ".join(_quote(c) for c in columns)
    src, dst = _quote(source), _quote(target)
    deleted = await conn.execute(
        f"DELETE FROM {dst} t USING projection_event_changes c WHERE c.operation = 'D' AND c.position > $1 "
        "AND COALESCE(t.actor ->> 'id', '') = c.actor_id AND t.event_id = c.event_id "
        "AND t.entity_type IS NOT DISTINCT FROM c.entity_type", since)
    replaced = await conn.execute(
        f"DELETE FROM {dst} t USING {src} s WHERE s.projection_position > $1 "
        "AND COALESCE(t.actor ->> 'id', '') = COALESCE(s.actor ->> 'id', '') AND t.event_id = s.event_id "
        "AND t.entity_type IS NOT DISTINCT FROM s.entity_type", since)
    inserted = await conn.execute(
        f"INSERT INTO {dst} ({col_list}) SELECT {col_list} FROM {src} WHERE projection_position > $1", since)
    return {"deleted": int(deleted.split()[-1]), "replaced": int(replaced.split()[-1]),
            "inserted": int(inserted.split()[-1])}


async def _verify(conn, source: str, target: str) -> None:
    for what, sql in (("rows", "SELECT count(*) FROM {t}"),
                      ("legacy rows (no projection_position)", "SELECT count(*) FROM {t} WHERE projection_position IS NULL"),
                      ("newest projection_position", "SELECT COALESCE(max(projection_position), 0) FROM {t}")):
        a = await conn.fetchval(sql.format(t=_quote(source)))
        b = await conn.fetchval(sql.format(t=_quote(target)))
        if a != b:
            raise PartitioningError(f"verification failed: {what} differ ({source}={a}, {target}={b}); nothing changed")


_INDEXES = ("SELECT c.relname AS name FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = to_regclass($1) AND c.relkind::text = $2 ORDER BY 1")


async def _rename_indexes(conn, table: str, kind: str, rename) -> None:
    for r in await conn.fetch(_INDEXES, table, kind):
        new = rename(r["name"])
        if new != r["name"]:
            await conn.execute(f"ALTER INDEX {_quote(r['name'])} RENAME TO {_quote(new)}")


def _strip(suffix: str):
    return lambda name: name[:-len(suffix)] if name.endswith(suffix) else name


async def _swap_names(conn, *, forward: bool) -> None:
    """Rename tables and (table-level) indexes.

    forward:  fabric_events -> fabric_events_unpartitioned (its indexes get ``_old``), fabric_events_part -> fabric_events
              (its ``_p`` indexes lose the suffix).  rollback: the exact reverse.
    """
    if forward:
        await conn.execute("ALTER TABLE fabric_events RENAME TO fabric_events_unpartitioned")
        await _rename_indexes(conn, "fabric_events_unpartitioned", "i", lambda n: n + "_old")
        await conn.execute("ALTER TABLE fabric_events_part RENAME TO fabric_events")
        await _rename_indexes(conn, "fabric_events", "I", _strip("_p"))
    else:
        await _rename_indexes(conn, "fabric_events", "I", lambda n: n + "_p")
        await conn.execute("ALTER TABLE fabric_events RENAME TO fabric_events_part")
        await conn.execute("ALTER TABLE fabric_events_unpartitioned RENAME TO fabric_events")
        await _rename_indexes(conn, "fabric_events", "i", _strip("_old"))


async def _lock(conn, settings: FabricPartitioningSettings) -> None:
    await conn.execute(f"SET LOCAL lock_timeout = '{int(settings.lock_timeout_seconds * 1000)}ms'")
    try:
        await conn.execute("LOCK TABLE fabric_events IN ACCESS EXCLUSIVE MODE")
    except Exception as exc:   # asyncpg LockNotAvailableError; never leave a half-taken swap
        if type(exc).__name__ == "LockNotAvailableError":
            raise PartitioningError(f"could not take the table lock within {settings.lock_timeout_seconds:g}s "
                                    "(writers busy); nothing changed, retry later") from None
        raise


async def cutover(conn, settings: FabricPartitioningSettings, *, allow_inspector_envelopes: bool = False) -> dict:
    """Switch traffic to the partitioned table. One transaction; nothing changes unless every check passes."""
    _require_enabled(settings)
    state = await _state(conn)
    if state["phase"] == "partitioned":
        raise PartitioningError("already partitioned")
    if state["phase"] != "backfilled":
        raise PartitioningError(f"backfill is not complete (phase={state['phase']}); run backfill until done")
    if await _relkind(conn, "fabric_events") != "r":
        raise PartitioningError("fabric_events is not a plain table")
    await _preflight(conn, forward=True)
    if not allow_inspector_envelopes and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM fabric_events WHERE entity_type = 'inspector' AND source_sequence IS NOT NULL)"):
        raise PartitioningError(
            "inspector rows carry v2 envelopes; the partitioned unique index (actor, source, source_sequence, entity_type) "
            "would stop detecting a sequence collision between an inspector and a non-inspector row. Re-run with "
            "--allow-inspector-envelopes only if that is acceptable")
    since = max(0, state["start_position"] - POSITION_OVERLAP)
    started = time.monotonic()
    async with conn.transaction():
        await _lock(conn, settings)
        caught = await _catch_up(conn, "fabric_events", "fabric_events_part", since)
        if settings.verify_counts:
            await _verify(conn, "fabric_events", "fabric_events_part")
        mark = await conn.fetchrow("SELECT last_value, is_called FROM projection_position_seq")
        await _swap_names(conn, forward=True)
        for name, when, scope, function in _TRIGGERS:
            await conn.execute(f"DROP TRIGGER IF EXISTS {name} ON fabric_events")
            await conn.execute(f"CREATE TRIGGER {name} {when} ON fabric_events {scope} EXECUTE FUNCTION {function}()")
        await conn.execute("UPDATE fabric_events_partition_state SET phase='partitioned', cutover_position=$1, "
                           "updated_at=now() WHERE singleton", mark["last_value"] if mark["is_called"] else 0)
    return {"phase": "partitioned", "caught_up": caught, "lock_held_seconds": round(time.monotonic() - started, 3)}


async def rollback(conn, settings: FabricPartitioningSettings) -> dict:
    """Return traffic to the old table, carrying over every change made since cutover."""
    _require_enabled(settings)
    state = await _state(conn)
    if state["phase"] != "partitioned" or await _relkind(conn, "fabric_events") != "p":
        raise PartitioningError("the journal is not partitioned; nothing to roll back")
    if await _relkind(conn, "fabric_events_unpartitioned") != "r":
        raise PartitioningError("fabric_events_unpartitioned is missing (already dropped); cannot roll back")
    if await _columns(conn, "fabric_events") != await _columns(conn, "fabric_events_unpartitioned"):
        raise PartitioningError("the two tables have different columns; a migration ran after cutover. Add the column "
                                "to fabric_events_unpartitioned by hand, then retry")
    since = state["cutover_position"] or 0
    async with conn.transaction():
        await _lock(conn, settings)
        # The old table kept its triggers; they must not fire for the copy (they would re-assign positions and log
        # every carried row as a new change).
        await conn.execute("ALTER TABLE fabric_events_unpartitioned DISABLE TRIGGER USER")
        caught = await _catch_up(conn, "fabric_events", "fabric_events_unpartitioned", since)
        await conn.execute("ALTER TABLE fabric_events_unpartitioned ENABLE TRIGGER USER")
        if settings.verify_counts:
            await _verify(conn, "fabric_events", "fabric_events_unpartitioned")
        for name, *_ in _TRIGGERS:
            await conn.execute(f"DROP TRIGGER IF EXISTS {name} ON fabric_events")
        await _swap_names(conn, forward=False)
        await conn.execute("UPDATE fabric_events_partition_state SET phase='backfilled', updated_at=now() WHERE singleton")
    return {"phase": "backfilled", "caught_up": caught}


async def drop_old(conn, settings: FabricPartitioningSettings) -> dict:
    """Free the old table's space. Irreversible: after this there is no rollback."""
    _require_enabled(settings)
    state = await _state(conn)
    if state["phase"] != "partitioned":
        raise PartitioningError("only valid while partitioned")
    await conn.execute("DROP TABLE IF EXISTS fabric_events_unpartitioned")
    return {"dropped": "fabric_events_unpartitioned"}
