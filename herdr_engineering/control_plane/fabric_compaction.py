"""Compacts service observation rows out of ``fabric_events``, archive first, so a boot loads fewer rows.

About 70% of the journal is service observations (a listener reported again with the same facts). A replay keeps
only each service's latest record, so the interior of those runs is dead weight. The rules below were checked on the
production journal (1,762,189 service rows -> 349,664, all 6,544 reconstructed services identical):

* ``ServiceListenerObserved`` / ``ServiceMetadataObserved``: keep each service's first row (it fixes ``created_at``)
  and last row; every row between them is removable.
* ``ServiceUpdated``: removable only when its payload equals the service's previous ``ServiceUpdated`` payload and it
  is not the last one. Other ``ServiceUpdated`` rows carry state transitions (``unavailable_since``) and stay.

Only rows without a v2 envelope (no source, source sequence or actor) are candidates, and only rows older than
``min_age_days``. A pass scans the journal once (read only) into a session temp table of event ids, then moves them
in small chunks, ``chunk_rows`` per transaction with ``chunk_pause_seconds`` between, so a large backlog never holds the
WAL or the table for minutes (one 250,000-row statement stalled every ingest write for ~8 minutes). Each chunk is ONE
statement that deletes the rows and inserts them into ``fabric_events_archive``, so a row is never lost half way, and
rows already listed stay removable (later rows can only make more of a stream interior). Removing rows changes the journal a recovery checkpoint covered; the checkpoint
loader then replays the affected streams in full (correct, slower), so after a drain the job rebuilds the checkpoint.
Off unless ``enabled`` is true; ``dry_run`` (the default) only counts.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

OBSERVATION_TYPES = ("ServiceListenerObserved", "ServiceMetadataObserved")
_LOCK = 0x46414243  # excludes a second compaction; the checkpoint build has its own lock


@dataclass(frozen=True)
class CompactionSettings:
    enabled: bool = False
    dry_run: bool = True
    interval_seconds: float = 3600.0
    min_age_days: float = 2.0
    batch_rows: int = 1_000_000          # rows listed (one journal scan) per pass
    chunk_rows: int = 5_000              # rows moved per transaction
    chunk_pause_seconds: float = 0.25    # breathing room for ingest between chunks
    max_pass_seconds: float = 300.0      # stop moving after this long; the rest is listed again a minute later
    archive_retention_days: float | None = 30.0
    rebuild_checkpoint: bool = True
    keep_checkpoints: int = 2


# The candidate rows. Windows run over the whole stream so "first" and "last" are real, age is applied afterwards.
_REMOVABLE = """
    SELECT event_id, event_type FROM (
        SELECT event_id, event_type, ingest_timestamp, payload,
               schema_version, source, source_sequence, actor,
               row_number() OVER w AS rn, count(*) OVER (PARTITION BY entity_id, event_type) AS cnt,
               lag(payload) OVER w AS prev_payload
        FROM fabric_events
        WHERE entity_type = 'service' AND entity_id IS NOT NULL
          AND event_type IN ('ServiceListenerObserved', 'ServiceMetadataObserved', 'ServiceUpdated')
        WINDOW w AS (PARTITION BY entity_id, event_type ORDER BY ingest_timestamp, sequence, event_id)
    ) x
    WHERE ingest_timestamp < now() - make_interval(secs => $1)
      AND schema_version = 1 AND source IS NULL AND source_sequence IS NULL AND actor IS NULL
      AND rn < cnt
      AND ((event_type <> 'ServiceUpdated' AND rn > 1)
           OR (event_type = 'ServiceUpdated' AND payload IS NOT DISTINCT FROM prev_payload))
"""

_COUNT = f"SELECT event_type, count(*) AS n FROM ({_REMOVABLE}) r GROUP BY event_type"

_LIST = f"""
    CREATE TEMP TABLE compaction_pending AS SELECT event_id FROM ({_REMOVABLE}) r LIMIT $2
"""

_MOVE_CHUNK = """
    WITH batch AS (
        DELETE FROM compaction_pending WHERE ctid IN (SELECT ctid FROM compaction_pending LIMIT $1) RETURNING event_id
    ), moved AS (
        DELETE FROM fabric_events f USING batch WHERE f.event_id = batch.event_id RETURNING f.*
    ) INSERT INTO fabric_events_archive SELECT moved.*, now() FROM moved
"""


class FabricCompactor:
    """Run :meth:`tick` from the worker loop; it does nothing until ``interval_seconds`` have passed."""

    def __init__(self, settings: CompactionSettings, *, clock=time.monotonic, pass_clock=time.monotonic, checkpoint_builder=None):
        self.settings = settings
        self._clock = clock
        self._pass_clock = pass_clock       # times one pass against max_pass_seconds; the tick clock only paces ticks
        self._last: float | None = None
        self._rebuild_pending = False
        self._checkpoint_builder = checkpoint_builder
        self.last_outcome = "not run"

    async def tick(self, conn) -> str:
        now = self._clock()
        if self._last is not None and now - self._last < self.settings.interval_seconds:
            return "waiting"
        self._last = now
        try:
            self.last_outcome = await self._run(conn)
        except Exception as exc:  # never take the worker down; the next tick tries again
            self.last_outcome = f"failed: {type(exc).__name__}"
            log.exception("fabric compaction failed")
        else:
            log.info("fabric compaction %s", self.last_outcome)
        return self.last_outcome

    async def _run(self, conn) -> str:
        s = self.settings
        if not await conn.fetchval("SELECT to_regclass('fabric_events_archive') IS NOT NULL"):
            return "skipped: apply migration 0035 (fabric_events_archive) first"
        age = s.min_age_days * 86400.0
        if s.dry_run:
            counts = {r["event_type"]: int(r["n"]) for r in await conn.fetch(_COUNT, age)}
            return f"dry run: would archive {sum(counts.values())} rows {dict(sorted(counts.items()))}"
        moved, drained = await self._move(conn, age)
        if moved is None:
            return "skipped: another compaction is running"
        purged = 0
        if s.archive_retention_days is not None:
            purged = int((await conn.execute(
                "DELETE FROM fabric_events_archive WHERE archived_at < now() - make_interval(secs => $1)",
                s.archive_retention_days * 86400.0)).split()[-1])
        if moved:
            self._rebuild_pending = True
        if not drained:
            self._last = self._clock() - s.interval_seconds + 60.0       # more to drain: come back in a minute
        outcome = f"archived {moved} rows, purged {purged} from the archive"
        if self._rebuild_pending and drained and s.rebuild_checkpoint:
            outcome += "; " + await self._rebuild(conn)
        return outcome

    async def _move(self, conn, age):
        """List the removable rows once, then move them chunk by chunk. Returns (moved, drained); moved is None when
        another compaction holds the lock. The lock and the temp table belong to this session, so both are released
        in ``finally`` (a failed chunk leaves its own transaction rolled back, the earlier chunks stay moved)."""
        s = self.settings
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK):
            return None, True
        started = self._pass_clock()
        moved = 0
        try:
            await conn.execute("DROP TABLE IF EXISTS compaction_pending")
            await conn.execute(_LIST, age, s.batch_rows)
            listed = int(await conn.fetchval("SELECT count(*) FROM compaction_pending"))
            while moved < listed:
                async with conn.transaction():
                    status = await conn.execute(_MOVE_CHUNK, s.chunk_rows)
                got = int(status.split()[-1])
                if not got:
                    break
                moved += got
                if self._pass_clock() - started >= s.max_pass_seconds:
                    break
                if s.chunk_pause_seconds:
                    await asyncio.sleep(s.chunk_pause_seconds)
            # drained: nothing was left behind by the row cap or the time cap
            return moved, listed < s.batch_rows and moved >= listed
        finally:
            try:
                await conn.execute("DROP TABLE IF EXISTS compaction_pending")
                await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK)
            except Exception:  # the connection is gone; the session end releases both
                log.warning("fabric compaction cleanup failed", exc_info=True)

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
        except Exception as exc:    # the rebuild already succeeded: a failed prune retries on the next rebuild
            outcome += f", prune failed ({type(exc).__name__})"
        return outcome
