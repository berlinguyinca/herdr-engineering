"""Moves old inspector rows out of ``fabric_events`` into ``fabric_events_archive``, so the live journal and a boot replay shrink.

The dashboard reads inspector data from the in-memory ring (24 h); a durable inspector row older than the ring is read
by nothing except a boot replay, which feeds it through ``ingest`` and evicts it at once. This job moves those rows.

A row moves only when ALL of these hold, checked inside the one statement that moves it:

* its type is one of ``event_types`` (default ``inspector.tool`` and ``inspector.message``; only tool, message, file and
  test are allowed). ``inspector.usage`` (feeds ``usage_totals``), ``preferences``, ``association``, ``model``,
  ``lifecycle`` and ``capabilities`` (small, state-bearing) are never moved;
* it is older than ``min_age_days`` (at least 7, default 14) by ``ingest_timestamp``;
* it has no v2 envelope (source, source sequence, actor), like the compaction job;
* an inspector snapshot exists and its ``watermark`` covers the row's ``sequence`` (the snapshot consumed it);
* the row is inside the newest projection checkpoint's coverage: ``projection_position`` is NULL (legacy prefix) or at
  most that checkpoint's ``frontier``. Rows newer than the frontier are the checkpoint's change-log tail and stay.

The move is MOVE, not copy: one statement per chunk deletes the rows and inserts them (every column named) into
``fabric_events_archive``, in its own transaction, ``chunk_rows`` at a time, so a row is never in both tables, never in
neither, and a failed chunk loses nothing. Re-running finds nothing left (idempotent). A dry run only counts.

Interactions (all covered by tests/control_plane/test_inspector_archive.py):

* Projection checkpoints. The delete trigger logs one ``D`` change per row. The checkpoint loader refuses a tail that holds
  an inspector ``D`` ("inspector mutation requires complete authoritative replay"), so a checkpoint published BEFORE a
  pass cannot be used after it. After a drained pass that moved rows the job therefore rebuilds the recovery checkpoint and
  (``publish_projection_checkpoint``, default true) publishes a new projection checkpoint, the same path the compaction job
  uses; until that publish succeeds a boot falls back to a complete replay, which is correct, only slower. The publish is
  retried on later ticks.
* Inspector snapshot. Its covered-row check compares the stored ``event_count`` with the rows at or below the watermark;
  that count now includes the archived inspector rows (``fabric_events_archive``), in the snapshot writer and at boot.
  The compaction job's archive purge never deletes inspector rows, and this job does not purge them.
* PR 7 on-demand history (``inspector_history``) also reads ``fabric_events_archive``: a page that reaches past
  ``min_age_days`` continues into the archive under the same keyset order and cursor, with no gap or duplicate
  (migration 0052 indexes the archive by (entity_id, ingest_timestamp, sequence)). Without the archive table only
  ``fabric_events`` is read.

Configured by ``control_plane.inspector_archive`` in the central fleet YAML (no environment variable). Off unless
``enabled`` is true; ``dry_run`` (the default) only counts.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from .fabric_compaction import _JOURNAL_COLUMNS, _quote, sync_archive_columns

log = logging.getLogger(__name__)

_LOCK = 0x49415243   # excludes a second archive pass; distinct from the compaction and checkpoint locks
ALLOWED_TYPES = ("inspector.tool", "inspector.message", "inspector.file", "inspector.test")
DEFAULT_TYPES = ("inspector.tool", "inspector.message")
MIN_AGE_FLOOR_DAYS = 7.0
_KEYS = ("enabled", "dry_run", "interval_seconds", "min_age_days", "event_types", "batch_rows", "chunk_rows",
         "chunk_pause_seconds", "max_pass_seconds", "rebuild_checkpoint", "keep_checkpoints",
         "publish_projection_checkpoint", "projection_lock_wait_seconds")


@dataclass(frozen=True)
class InspectorArchiveSettings:
    enabled: bool = False
    dry_run: bool = True
    interval_seconds: float = 3600.0
    min_age_days: float = 14.0
    event_types: tuple[str, ...] = DEFAULT_TYPES
    batch_rows: int = 500_000            # rows moved per pass at most
    chunk_rows: int = 5_000              # rows moved per transaction
    chunk_pause_seconds: float = 0.25    # breathing room for ingest between chunks
    max_pass_seconds: float = 300.0      # stop moving after this long; the rest moves a minute later
    rebuild_checkpoint: bool = True
    keep_checkpoints: int = 2
    publish_projection_checkpoint: bool = True
    projection_lock_wait_seconds: float = 60.0


def _number(section: dict, key: str, default: float, minimum: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ValueError(f"inspector_archive.{key} must be a number >= {minimum:g}")
    return float(value)


def _boolean(section: dict, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"inspector_archive.{key} must be a boolean (true or false)")
    return value


def parse_inspector_archive(section: Any) -> InspectorArchiveSettings:
    """Validate ``control_plane.inspector_archive``; ValueError names the field, unknown keys are rejected."""
    if section is None:
        return InspectorArchiveSettings()
    if not isinstance(section, dict):
        raise ValueError("inspector_archive must be a mapping")
    unknown = sorted(str(k) for k in set(section) - set(_KEYS))
    if unknown:
        raise ValueError(f"inspector_archive: unknown key(s) {unknown} (allowed: {', '.join(_KEYS)})")
    types = section.get("event_types", list(DEFAULT_TYPES))
    if (not isinstance(types, (list, tuple)) or not types or len(set(types)) != len(types)
            or any(t not in ALLOWED_TYPES for t in types)):
        raise ValueError(f"inspector_archive.event_types must be a non-empty list of distinct values from "
                         f"{', '.join(ALLOWED_TYPES)}")
    return InspectorArchiveSettings(
        enabled=_boolean(section, "enabled", False), dry_run=_boolean(section, "dry_run", True),
        interval_seconds=_number(section, "interval_seconds", 3600, 60),
        min_age_days=_number(section, "min_age_days", 14, MIN_AGE_FLOOR_DAYS),
        event_types=tuple(types),
        batch_rows=int(_number(section, "batch_rows", 500_000, 1000)),
        chunk_rows=int(_number(section, "chunk_rows", 5000, 100)),
        chunk_pause_seconds=_number(section, "chunk_pause_seconds", 0.25, 0),
        max_pass_seconds=_number(section, "max_pass_seconds", 300, 10),
        rebuild_checkpoint=_boolean(section, "rebuild_checkpoint", True),
        keep_checkpoints=int(_number(section, "keep_checkpoints", 2, 1)),
        publish_projection_checkpoint=_boolean(section, "publish_projection_checkpoint", True),
        projection_lock_wait_seconds=_number(section, "projection_lock_wait_seconds", 60, 0))


# $1 = event types, $2 = min age in seconds. The context row: the snapshot's watermark (NULL = no snapshot = nothing
# is covered) and the newest projection checkpoint's frontier (NULL = none; only legacy NULL-position rows qualify).
_CONTEXT = """
    (SELECT (SELECT watermark FROM inspector_snapshots WHERE id = 1) AS wm,
            {frontier} AS fr) ctx
"""
_ELIGIBLE = """
    f.entity_type = 'inspector' AND f.entity_id IS NOT NULL AND f.entity_id <> ''
    AND f.event_type = ANY($1::text[])
    AND f.ingest_timestamp < now() - make_interval(secs => $2)
    AND f.schema_version = 1 AND f.source IS NULL AND f.source_sequence IS NULL AND f.actor IS NULL
    AND ctx.wm IS NOT NULL AND f.sequence <= ctx.wm
    AND (f.projection_position IS NULL OR f.projection_position <= ctx.fr)
"""

_COUNT = "SELECT f.event_type, count(*) AS n FROM fabric_events f, {ctx} WHERE {eligible} GROUP BY f.event_type"

_MOVE_CHUNK = """
    WITH picked AS (
        SELECT f.event_id FROM fabric_events f, {ctx} WHERE {eligible} ORDER BY f.sequence LIMIT $3
    ), moved AS (
        DELETE FROM fabric_events f USING picked WHERE f.event_id = picked.event_id RETURNING f.*
    ) INSERT INTO fabric_events_archive ({columns}, archived_at) SELECT {moved_columns}, now() FROM moved
"""


async def _statements(conn) -> tuple[str, str]:
    """The count and move statements. The checkpoint frontier is read from the catalog tables when they exist."""
    has_checkpoints = await conn.fetchval("SELECT to_regclass('projection_checkpoints') IS NOT NULL")
    frontier = "(SELECT max(frontier) FROM projection_checkpoints)" if has_checkpoints else "NULL::bigint"
    ctx = _CONTEXT.format(frontier=frontier)
    return (_COUNT.format(ctx=ctx, eligible=_ELIGIBLE), _MOVE_CHUNK.format(ctx=ctx, eligible=_ELIGIBLE, columns="{columns}",
                                                                          moved_columns="{moved_columns}"))


class InspectorArchiver:
    """Run :meth:`tick` from the worker loop; it does nothing until ``interval_seconds`` have passed."""

    def __init__(self, settings: InspectorArchiveSettings, *, clock=time.monotonic, pass_clock=time.monotonic,
                 checkpoint_builder=None, projection_publisher=None):
        self.settings = settings
        self._clock = clock
        self._pass_clock = pass_clock
        self._last: float | None = None
        self._rebuild_pending = False
        self._checkpoint_builder = checkpoint_builder
        self._projection_publisher = projection_publisher       # blocking callable(lock_wait_seconds); None = not wired
        self._projection_pending = False
        self._projection_retry_due = False
        self.last_outcome = "not run"

    async def tick(self, conn) -> str:
        now = self._clock()
        if self._last is not None and now - self._last < self.settings.interval_seconds:
            return await self._retry_projection() or "waiting"
        self._last = now
        try:
            self.last_outcome = await self._run(conn)
        except Exception as exc:  # never take the worker down; the next tick tries again
            self.last_outcome = f"failed: {type(exc).__name__}"
            log.exception("inspector archive failed")
        else:
            log.info("inspector archive %s", self.last_outcome)
        return self.last_outcome

    async def _run(self, conn) -> str:
        s = self.settings
        if not await conn.fetchval("SELECT to_regclass('fabric_events_archive') IS NOT NULL"):
            return "skipped: apply migration 0035 (fabric_events_archive) first"
        if not await conn.fetchval("SELECT to_regclass('inspector_snapshots') IS NOT NULL"):
            return "skipped: apply migration 0030 (inspector_snapshots) first"
        age, types = s.min_age_days * 86400.0, list(s.event_types)
        count_sql, move_sql = await _statements(conn)
        if s.dry_run:
            counts = {r["event_type"]: int(r["n"]) for r in await conn.fetch(count_sql, types, age)}
            return f"dry run: would archive {sum(counts.values())} inspector rows {dict(sorted(counts.items()))}"
        moved, drained = await self._move(conn, move_sql, types, age)
        if moved is None:
            return "skipped: another inspector archive is running"
        if moved:
            self._rebuild_pending = True
            self._projection_pending = True
        if not drained:
            self._last = self._clock() - s.interval_seconds + 60.0       # more to move: come back in a minute
        outcome = f"archived {moved} inspector rows"
        if self._rebuild_pending and drained and s.rebuild_checkpoint:
            outcome += "; " + await self._rebuild(conn)
        if self._projection_pending and drained and s.publish_projection_checkpoint:
            outcome += "; " + await self._publish_projection()
        return outcome

    async def _move(self, conn, move_template: str, types: list[str], age: float):
        """Move chunk by chunk. Returns (moved, drained); moved is None when another pass holds the lock."""
        s = self.settings
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK):
            return None, True
        started, moved = self._pass_clock(), 0
        try:
            columns = await sync_archive_columns(conn)
            move_sql = move_template.format(columns=", ".join(_quote(c) for c in columns),
                                            moved_columns=", ".join("moved." + _quote(c) for c in columns))
            while moved < s.batch_rows:
                async with conn.transaction():
                    # A column added to the journal since this pass began would be deleted with its row but not copied.
                    if [r["name"] for r in await conn.fetch(_JOURNAL_COLUMNS)] != columns:
                        log.warning("inspector archive stopped: the journal's columns changed during the pass")
                        return moved, False
                    got = int((await conn.execute(move_sql, types, age, min(s.chunk_rows, s.batch_rows - moved))).split()[-1])
                if not got:
                    return moved, True
                moved += got
                if self._pass_clock() - started >= s.max_pass_seconds:
                    return moved, False
                if s.chunk_pause_seconds:
                    await asyncio.sleep(s.chunk_pause_seconds)
            return moved, False
        finally:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK)
            except Exception:  # the connection is gone; the session end releases it
                log.warning("inspector archive cleanup failed", exc_info=True)

    async def _retry_projection(self) -> str | None:
        s = self.settings
        if not (self._projection_retry_due and self._projection_pending and s.publish_projection_checkpoint
                and not s.dry_run):
            return None
        outcome = await self._publish_projection()
        self.last_outcome = outcome
        return outcome

    async def _publish_projection(self) -> str:
        """Publish a projection checkpoint off the event loop. Never raises; a failure leaves the publish pending."""
        if self._projection_publisher is None:
            log.warning("inspector archive cannot publish a projection checkpoint: no publisher is wired")
            self._projection_retry_due = True
            return "projection checkpoint deferred (no publisher)"
        try:
            await asyncio.to_thread(self._projection_publisher, self.settings.projection_lock_wait_seconds)
        except Exception as exc:
            log.warning("inspector archive projection checkpoint publish failed (%s)", type(exc).__name__)
            self._projection_retry_due = True
            return f"projection checkpoint deferred ({type(exc).__name__})"
        self._projection_pending = False
        self._projection_retry_due = False
        return "projection checkpoint published"

    async def _rebuild(self, conn) -> str:
        from . import recovery_checkpoint as rc
        build = self._checkpoint_builder or rc.build_checkpoint
        try:
            report = await build(conn)
        except (rc.CheckpointUnavailable, rc.CheckpointBuildInProgress) as exc:
            return f"checkpoint rebuild deferred ({type(exc).__name__})"
        self._rebuild_pending = False
        outcome = f"checkpoint rebuilt ({report.get('selected_events')} rows selected)"
        try:
            outcome += f", pruned {await rc.prune_checkpoints(conn, self.settings.keep_checkpoints)} old generations"
        except rc.CheckpointBuildInProgress:
            outcome += ", prune deferred"
        except Exception as exc:
            outcome += f", prune failed ({type(exc).__name__})"
        return outcome
