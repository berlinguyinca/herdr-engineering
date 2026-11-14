"""Bounded database history reads; raw rows remain authoritative across restarts."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

PRIVATE_TYPES = frozenset({'lineage', 'review_request', 'inspector', 'host_metrics',
                           'agent_identity', 'agent_summary', 'deployment', 'deployment_plan',
                           'deployment_request', 'deployment_escalation', 'mission_activity_store'})
ENVELOPE_ONLY = ('host', 'session', 'mission', 'service', 'inspector', 'mission_activity', 'lineage')


def hidden_history(kind):
    return kind.casefold() in PRIVATE_TYPES or kind.casefold().startswith('collab')


def public_event(row):
    from .bridge import _rebuild_events
    streams, _ = _rebuild_events([dict(row)])
    return next(iter(streams.values()))[0]


def _b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def _unb64(value):
    raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    if _b64(raw) != value:
        raise ValueError('noncanonical history cursor')
    return raw


def _tag(kind, identity):
    return _b64(hashlib.sha256(json.dumps([kind, identity], separators=(',', ':')).encode()).digest()[:16])


def _row_key(row):
    actor = row.get('actor') or {}
    if isinstance(actor, str):
        actor = json.loads(actor)
    principal = actor.get('id') or ''
    return hashlib.sha256(f'{len(principal)}:{principal}{row["event_id"]}'.encode()).digest()


def _event_digest(row):
    # Internal row-instance UUID changes on every UPDATE. Never expose an
    # unkeyed digest of payload text: it could become a secret-guessing oracle.
    value = [str(row.get('recovery_instance') or ''), row['event_id'],
             row['sequence'], _micros(row['ingest_timestamp'])]
    return _b64(hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).digest())


def _micros(stamp):
    delta = stamp - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _cursor(kind, identity, row):
    value = [2, _tag(kind, identity), _b64(_row_key(row)), _event_digest(row),
             row['sequence'], _micros(row['ingest_timestamp'])]
    encoded = _b64(json.dumps(value, separators=(',', ':')).encode())
    if len(encoded) > 256:
        raise RuntimeError('history cursor exceeds its bound')
    return encoded


def _boundary(kind, identity, cursor):
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 256:
            raise ValueError()
        value = json.loads(_unb64(cursor))
        if isinstance(value,list) and len(value)==4 and value[0]==1:
            expected=hashlib.sha256(json.dumps([kind,identity],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if (type(value[1]) is not int or not 0<value[1]<2**63 or value[2]!=expected
                    or not isinstance(value[3],str) or len(value[3])!=64
                    or bytes.fromhex(value[3]).hex()!=value[3]):
                raise ValueError()
            return value,None
        if (not isinstance(value, list) or len(value) != 6 or value[0] != 2
                or value[1] != _tag(kind, identity) or type(value[4]) is not int
                or not -(2**63)<=value[4]<2**63
                or type(value[5]) is not int or len(_unb64(value[2])) != 32
                or len(_unb64(value[3])) != 32):
            raise ValueError()
        stamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value[5])
        return value, stamp
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        raise ValueError('invalid history cursor') from exc


class DurableHistoryUnavailable(RuntimeError):
    """A durable read failed; expose only a static message and exception class."""

    def __init__(self, cause_name):
        super().__init__('Durable history is unavailable.')
        self.cause_name = cause_name


class HistoryCursorStale(ValueError):
    """A validated cursor no longer identifies its original durable anchor."""


class PostgresHistory:
    def __init__(self, dsn, run, *, schema='public'):
        self.dsn, self.run, self.schema = dsn, run, schema
        self._pool = None
        self._pool_lock = None
        self._closed = False
        self._close_lock = threading.Lock()
        self.table = '"' + schema.replace('"', '""') + '".fabric_events'

    async def _get_pool(self):
        import asyncio

        import asyncpg
        if self._closed:
            raise RuntimeError('durable history is closed')
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with asyncio.timeout(5), self._pool_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4,
                    command_timeout=30, timeout=5,
                    server_settings={'search_path': '"'+self.schema.replace('"', '""')+'"'})
        return self._pool

    def _call(self, operation):
        try:
            return self.run(operation)
        except HistoryCursorStale:
            raise
        except Exception as exc:
            raise DurableHistoryUnavailable(type(exc).__name__) from None

    def stream(self, kind, identity):
        return self._call(self._stream(kind, identity))

    async def _stream(self, kind, identity):
        pool = await self._get_pool()
        async with pool.acquire(timeout=5) as conn:
            rows = await conn.fetch(f'SELECT * FROM {self.table} WHERE entity_type=$1 AND entity_id=$2 '
                'ORDER BY sequence,ingest_timestamp,event_id COLLATE "C",COALESCE(actor->>\'id\',\'\') COLLATE "C"', kind, identity)
        return [public_event(row) for row in rows]

    def stream_page(self, kind, identity, *, limit=50, before=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be an integer between 1 and 200')
        boundary = _boundary(kind, identity, before) if before is not None else None
        return self._call(self._page(kind, identity, limit, boundary))

    async def _page(self, kind, identity, limit, boundary):
        pool = await self._get_pool()
        order = 'sequence,ingest_timestamp,event_id COLLATE "C",COALESCE(actor->>\'id\',\'\') COLLATE "C"'
        descending = 'sequence DESC,ingest_timestamp DESC,event_id COLLATE "C" DESC,COALESCE(actor->>\'id\',\'\') COLLATE "C" DESC'
        async with pool.acquire(timeout=5) as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            latest = await conn.fetchrow(f'SELECT * FROM {self.table} WHERE entity_type=$1 AND entity_id=$2 ORDER BY {descending} LIMIT 1', kind, identity)
            args = [kind, identity]
            predicate = ''
            if boundary:
                value, stamp = boundary
                if value[0]==1:
                    anchor=await conn.fetchrow(f'SELECT * FROM {self.table} WHERE entity_type=$1 AND entity_id=$2 '
                        f'ORDER BY {order} OFFSET $3 LIMIT 1',kind,identity,value[1])
                    from .history_cursor import fingerprint
                    matches=anchor is not None and fingerprint(public_event(anchor))==value[3]
                else:
                    principal = "COALESCE(actor->>'id','')"
                    key = f"sha256(convert_to(length({principal})::text||':'||{principal}||event_id,'UTF8'))"
                    anchor = await conn.fetchrow(f'SELECT * FROM {self.table} WHERE entity_type=$1 AND entity_id=$2 '
                        f'AND sequence=$3 AND ingest_timestamp=$4 AND {key}=$5', kind, identity, value[4], stamp, _unb64(value[2]))
                    matches=anchor is not None and _event_digest(anchor)==value[3]
                if not matches:
                    raise HistoryCursorStale('staleHistoryCursor')
                actor = anchor['actor'] or {}
                if isinstance(actor, str):
                    actor = json.loads(actor)
                args += [anchor['sequence'], anchor['ingest_timestamp'], anchor['event_id'], actor.get('id') or '']
                predicate = ' AND (sequence,ingest_timestamp,event_id COLLATE "C",COALESCE(actor->>\'id\',\'\') COLLATE "C") < ($3,$4,$5::text COLLATE "C",$6::text COLLATE "C")'
            args.append(limit + 1)
            rows = await conn.fetch(f'SELECT * FROM {self.table} WHERE entity_type=$1 AND entity_id=$2{predicate} ORDER BY {descending} LIMIT ${len(args)}', *args)
        more = len(rows) > limit
        shown = list(reversed(rows[:limit]))
        return {'events': [public_event(row) for row in shown], 'has_more': more,
                'next_cursor': _cursor(kind, identity, shown[0]) if more else None,
                'latest_sequence': latest['sequence'] if latest else None,
                'last_event_id': latest['event_id'] if latest else None}

    def inspector_page(self, session_id, *, limit, before=None, cutoff=None, kinds=None):
        """One bounded page of durable inspector rows older than the ring (see inspector_history)."""
        return self._call(self._inspector_page(session_id, limit, before, cutoff, kinds))

    async def _inspector_page(self, session_id, limit, before, cutoff, kinds):
        from .inspector_history import fetch_page
        pool = await self._get_pool()
        async with pool.acquire(timeout=5) as conn:
            return await fetch_page(conn, self.table, session_id, limit=limit, before=before,
                                    cutoff=cutoff, kinds=kinds, archive_table=self.table + "_archive")

    def entity_ids(self, kind):
        return self._call(self._ids(kind))

    async def _ids(self, kind):
        pool = await self._get_pool()
        async with pool.acquire(timeout=5) as conn:
            rows = await conn.fetch(f'SELECT entity_id,min(ingest_timestamp) AS first FROM {self.table} '
                'WHERE entity_type=$1 AND entity_id IS NOT NULL GROUP BY entity_id ORDER BY first,entity_id', kind)
        return [row['entity_id'] for row in rows]

    def recent_events(self, limit=50):
        return self._call(self._recent(limit))

    def communications(self, limit, before):
        return self._call(self._communications(limit, before))

    async def _communications(self, limit, before):
        """One bounded result per operation, with server receipts preferred."""
        stamp = datetime.fromisoformat(before[0]) if before else None
        identity = before[1] if before else None
        pool = await self._get_pool()
        async with pool.acquire(timeout=5) as conn:
            rows = await conn.fetch(f'''
                WITH ranked AS (
                  SELECT *, min(ingest_timestamp) OVER (PARTITION BY entity_id) AS communication_started,
                    row_number() OVER (PARTITION BY entity_id ORDER BY
                      (payload->>'evidence_kind' = 'server_receipt') DESC,
                      (payload->>'status' <> 'queued') DESC,
                      ingest_timestamp DESC, sequence DESC, event_id DESC) AS choice
                  FROM {self.table} WHERE entity_type='communication'
                )
                SELECT * FROM ranked WHERE choice=1
                  AND ($1::timestamptz IS NULL OR (communication_started,entity_id) < ($1,$2::text))
                ORDER BY communication_started DESC,entity_id DESC LIMIT $3
            ''', stamp, identity, limit)
        return [{**public_event(row), 'communication_started': row['communication_started'].isoformat()}
                for row in rows]

    def export_events(self):
        return self._call(self._export_events())

    async def _export_events(self):
        from .bridge import _rebuild_events
        pool = await self._get_pool()
        async with pool.acquire(timeout=5) as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            rows = [dict(row) async for row in conn.cursor(
                f'SELECT * FROM {self.table} WHERE entity_type<>ALL($1::text[]) '
                'ORDER BY ingest_timestamp,sequence,event_id',
                ['agent_identity','agent_summary','inspector','host_metrics','mission_activity_store','collab'], prefetch=1000)]
        streams, order = _rebuild_events(rows, reuse=True)
        order = [row for row in order if not hidden_history(row[0])]
        return streams, order

    async def _recent(self, limit):
        if type(limit) is not int or limit < 1:
            return []
        pool = await self._get_pool()
        predicate = "entity_type IS NOT NULL AND entity_id IS NOT NULL AND lower(entity_type)<>ALL($1::text[]) AND lower(entity_type) NOT LIKE 'collab%' AND NOT (source_sequence IS NOT NULL AND entity_type=ANY($2::text[]))"
        async with pool.acquire(timeout=5) as conn, conn.transaction(isolation='repeatable_read', readonly=True):
            rows = list(await conn.fetch(f'SELECT * FROM {self.table} WHERE projection_position IS NOT NULL AND {predicate} ORDER BY projection_position DESC LIMIT $3', list(PRIVATE_TYPES), list(ENVELOPE_ONLY), limit))
            if len(rows) < limit:
                rows += list(await conn.fetch(f'SELECT * FROM {self.table} WHERE projection_position IS NULL AND {predicate} ORDER BY ingest_timestamp DESC,sequence DESC,event_id DESC LIMIT $3', list(PRIVATE_TYPES), list(ENVELOPE_ONLY), limit-len(rows)))
        return [public_event(row) for row in rows]

    def close(self):
        with self._close_lock:
            if self._pool is not None:
                async def close_pool():
                    import asyncio
                    try:
                        await asyncio.wait_for(self._pool.close(), timeout=5)
                    except TimeoutError:
                        self._pool.terminate()
                self.run(close_pool())
            self._closed = True
