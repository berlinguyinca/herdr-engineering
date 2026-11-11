"""Private JSON projection checkpoints and a commit-fenced replay frontier.

The raw event table remains authoritative. A position is assigned under the
writer's transaction lock, never by a column default evaluated before that lock.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import zlib
from contextlib import asynccontextmanager

FORMAT_VERSION = 1
FENCE_NAMESPACE = 1213355076
MAX_CHECKPOINT_BYTES = 1024 * 1024 * 1024


class CheckpointInvalid(ValueError):
    pass


class SnapshotChanged(CheckpointInvalid):
    pass


def encode_checkpoint(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise CheckpointInvalid('projection checkpoint exceeds the decoded byte limit')
    return zlib.compress(raw, level=3), hashlib.sha256(raw).hexdigest()


def decode_checkpoint(packed, checksum):
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, MAX_CHECKPOINT_BYTES + 1)
        if (len(raw) > MAX_CHECKPOINT_BYTES or decoder.unconsumed_tail
                or not decoder.eof or decoder.unused_data):
            raise CheckpointInvalid('invalid checkpoint compression or size')
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise CheckpointInvalid('projection checkpoint checksum mismatch')
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get('schema')) is not int
                or value['schema'] != FORMAT_VERSION):
            raise CheckpointInvalid('incompatible projection checkpoint format')
        return value
    except (ValueError, TypeError, zlib.error, UnicodeError) as exc:
        raise CheckpointInvalid('invalid projection checkpoint') from exc


class ProjectionStore:
    def __init__(self, conn, *, fence_timeout=5):
        self.conn = conn
        self.fence_timeout = fence_timeout

    async def ensure_indexes(self):
        """Explicit preparation only; never called by read-only web startup."""
        if self.conn.is_in_transaction():
            raise RuntimeError('concurrent checkpoint index preparation requires autocommit')
        valid = await self.conn.fetchval("SELECT indisvalid AND indisready FROM pg_index "
            "WHERE indexrelid=to_regclass('idx_fabric_events_projection_position')")
        if valid is False:
            await self.conn.execute('DROP INDEX CONCURRENTLY idx_fabric_events_projection_position')
        await self.conn.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fabric_events_projection_position '
            'ON fabric_events(projection_position) WHERE projection_position IS NOT NULL')

    @asynccontextmanager
    async def publisher(self, wait=0.0, poll=0.25):
        """One materializer per database schema; readers never take this lock.

        ``wait`` bounds how long (seconds) to keep retrying a held lock before
        giving up; the default of zero fails at once, as the worker's refresh does.
        """
        conn = self.conn
        key = await conn.fetchval("SELECT hashtext(current_schema() || '.projection_checkpoints')")
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            acquired = await conn.fetchval('SELECT pg_try_advisory_lock($1::int,$2::int)',
                                           FENCE_NAMESPACE, key)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckpointInvalid('another projection checkpoint materializer is active'
                                        + await self._lock_holder(key))
            await asyncio.sleep(min(poll, remaining))
        try:
            yield
        finally:
            if not conn.is_closed():
                await conn.execute('SELECT pg_advisory_unlock($1::int,$2::int)', FENCE_NAMESPACE, key)

    async def _lock_holder(self, key):
        """Name the session holding the materializer lock, so an abort says who to look at."""
        try:
            rows = await self.conn.fetch(
                "SELECT a.pid, a.client_addr::text AS addr, a.application_name AS app, a.state, "
                "extract(epoch FROM now() - a.state_change)::int AS idle_seconds, left(a.query, 80) AS query "
                "FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                "WHERE l.locktype = 'advisory' AND l.classid = $1::int::oid AND l.objid = $2::int::oid "
                "AND l.granted AND a.pid <> pg_backend_pid()",
                FENCE_NAMESPACE, key)
        except Exception:  # diagnostics must never mask the real refusal
            return ""
        return "".join(
            f" (held by pid {r['pid']} addr {r['addr'] or 'local'} app {r['app'] or '-'} "
            f"state {r['state']} for {r['idle_seconds']}s: {r['query']!r})" for r in rows)

    async def available(self):
        return bool(await self.conn.fetchval(
            "SELECT to_regclass('projection_checkpoints') IS NOT NULL "
            "AND to_regclass('projection_event_changes') IS NOT NULL "
            "AND to_regclass('projection_checkpoint_control') IS NOT NULL "
            "AND EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('fabric_events') "
            "AND attname='projection_position' AND NOT attisdropped) "
            "AND (SELECT count(*) FROM pg_trigger WHERE tgrelid=to_regclass('fabric_events') "
            "AND tgname IN ('projection_position_before_write','projection_change_after_write',"
            "'projection_change_after_truncate') AND tgenabled IN ('O','A'))=3 "
            "AND EXISTS(SELECT 1 FROM pg_sequences WHERE schemaname=current_schema() "
            "AND sequencename='projection_position_seq' AND cache_size=1) "
            "AND EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass('fabric_events') "
            "AND tgname='fabric_event_recovery_instance' AND tgenabled IN ('O','A'))"))

    @asynccontextmanager
    async def snapshot(self):
        """Fence writers briefly, then fold an unlocked repeatable-read view."""
        conn = self.conn
        if conn.is_in_transaction():
            raise RuntimeError('checkpoint fence must precede the snapshot transaction')
        key = await conn.fetchval("SELECT hashtext(current_schema() || '.fabric_events')")
        locked = False
        transaction = None
        try:
            try:
                await asyncio.wait_for(conn.execute('SELECT pg_advisory_lock($1::int,$2::int)',
                                                    FENCE_NAMESPACE,key),self.fence_timeout)
            except BaseException:
                # Cancellation can race acquisition. Closing the session is the
                # only unconditional proof that no exclusive fence remains held.
                await conn.close()
                raise
            locked = True
            transaction = conn.transaction(isolation='repeatable_read', readonly=True)
            await transaction.start()
            frontier = await conn.fetchval('SELECT GREATEST(replay_floor,COALESCE((SELECT max(position) FROM projection_event_changes),0)) '
                                           'FROM projection_checkpoint_control WHERE singleton')
            await conn.execute('SELECT pg_advisory_unlock($1::int,$2::int)', FENCE_NAMESPACE, key)
            locked = False
            legacy = await conn.fetchval('SELECT legacy_rows FROM projection_checkpoint_control WHERE singleton')
            if legacy is None:
                # Bootstrap outside the DDL lock and writer fence. The RR view
                # is exact; publication reconciles intervening legacy deletions.
                legacy = await conn.fetchval('SELECT count(*) FROM fabric_events WHERE projection_position IS NULL')
            floor = await conn.fetchval('SELECT replay_floor FROM projection_checkpoint_control WHERE singleton')
            yield {'position': int(frontier), 'legacy_rows': int(legacy), 'replay_floor': int(floor)}
            await transaction.commit()
            transaction = None
            structural = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM projection_event_changes WHERE operation='T' AND position>$1)",frontier)
            if structural:
                raise SnapshotChanged('history was truncated during snapshot capture')
        finally:
            if transaction is not None and not conn.is_closed():
                await transaction.rollback()
            if locked and not conn.is_closed():
                await conn.execute('SELECT pg_advisory_unlock($1::int,$2::int)', FENCE_NAMESPACE, key)

    async def changes_after(self, frontier, through=None):
        return [dict(row) for row in await self.conn.fetch(
            'SELECT * FROM projection_event_changes WHERE position>$1 AND ($2::bigint IS NULL OR position<=$2) ORDER BY position',
            frontier, through)]

    async def candidates(self, code, policy, *, compatible_codes=()):
        return [dict(row) for row in await self.conn.fetch(
            'SELECT * FROM projection_checkpoints WHERE code_fingerprint=ANY($1::text[]) AND policy_fingerprint=$2 '
            'AND format_version=$3 ORDER BY frontier DESC, checkpoint_id DESC LIMIT 4',
            [code, *compatible_codes], policy, FORMAT_VERSION)]

    async def publish(self, code, policy, frontier, value):
        packed, checksum = encode_checkpoint(value)
        decode_checkpoint(packed, checksum)
        async with self.conn.transaction():
            legacy = await self.conn.fetchval('SELECT legacy_rows FROM projection_checkpoint_control WHERE singleton FOR UPDATE')
            if legacy is None and isinstance(value.get('coverage'), dict):
                removed = await self.conn.fetchval("SELECT count(*) FROM projection_event_changes "
                    "WHERE position>$1 AND operation IN ('D','U') AND had_position IS NULL", frontier)
                await self.conn.execute('UPDATE projection_checkpoint_control SET legacy_rows=$1 WHERE singleton',
                                        value['coverage']['legacy_rows'] - removed)
            await self.conn.execute(
                'INSERT INTO projection_checkpoints(code_fingerprint,policy_fingerprint,frontier,format_version,state,checksum) '
                'VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(code_fingerprint,policy_fingerprint,frontier) '
                'DO UPDATE SET state=EXCLUDED.state,checksum=EXCLUDED.checksum,created_at=now()',
                code, policy, frontier, FORMAT_VERSION, packed, checksum)
            # Retain independent earlier valid candidates for corruption/rollback.
            await self.conn.execute('DELETE FROM projection_checkpoints WHERE checkpoint_id NOT IN '
                                    '(SELECT checkpoint_id FROM projection_checkpoints ORDER BY created_at DESC,checkpoint_id DESC LIMIT 4)')
            floor = await self.conn.fetchval('SELECT min(frontier) FROM projection_checkpoints')
            if floor is not None:
                await self.conn.execute('DELETE FROM projection_event_changes WHERE position<=$1', floor)
                await self.conn.execute('UPDATE projection_checkpoint_control SET replay_floor=GREATEST(replay_floor,$1) WHERE singleton', floor)
        return {'frontier': frontier, 'compressed_bytes': len(packed), 'checksum': checksum}
