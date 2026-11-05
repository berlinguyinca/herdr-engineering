"""Recover every durable projection from a verified prefix and affected streams."""
from __future__ import annotations

import logging
import time

from .envelope import SourceLedger
from .inspector import InspectorStore
from .projection_checkpoint import (
    COMPACT_TYPES,
    PreparedSource,
    capture_projection,
    code_fingerprint,
    event_time,
    policy_fingerprint,
    restore_projection,
)
from .projection_compatibility import compatible_fingerprints
from .projection_history import ENVELOPE_ONLY, PostgresHistory, hidden_history, public_event
from .projection_store import CheckpointInvalid, ProjectionStore, SnapshotChanged, decode_checkpoint

log = logging.getLogger(__name__)
RETENTION_TYPES = frozenset({'inspector','host_metrics','token_usage','resource_usage',
                             'ci_report','ci_observation','ci_telemetry','ci_pipeline','mission_activity_store'})
MAX_TAIL_ROWS = 100_000
MAX_DIRTY_ROWS = 250_000


def feed_visible(row):
    kind = row.get('entity_type')
    return bool(kind and row.get('entity_id') and not hidden_history(kind)
                and not ((row.get('source_sequence') is not None or row.get('has_envelope'))
                         and kind in ENVELOPE_ONLY))


async def _rows(conn, sql, *args, maximum=None):
    import json
    result = []
    async for row in conn.cursor(sql, *args, prefetch=1000):
        item = dict(row)
        for field in ('payload','actor','evidence_refs'):
            if isinstance(item.get(field), str):
                item[field] = json.loads(item[field])
        result.append(item)
        if maximum is not None and len(result) > maximum:
            raise CheckpointInvalid('incremental stream budget exceeded; complete replay required')
    return result


async def _read_prepared(source, code, policy):
    for attempt in range(3):
        try:
            return await _read_prepared_once(source,code,policy)
        except SnapshotChanged:
            if attempt==2:
                raise


async def _read_prepared_once(source, code, policy):
    import asyncpg
    if source._conn is None or source._conn.is_closed():
        source._conn = await asyncpg.connect(source._dsn)
    conn = source._conn
    store = ProjectionStore(conn)
    if not await store.available():
        return None
    async with store.snapshot() as point:
        schema = await conn.fetchval('SELECT current_schema()')
        structured = {name:[dict(row) for row in await conn.fetch(f'SELECT * FROM {name}')]
                      for name in ('missions','sessions','services','hosts')}
        candidates = await store.candidates(code, policy, compatible_codes=compatible_fingerprints(code))
        # Retain the shipped row-selection/inspector optimization until an
        # explicit materializer has prepared a compatible complete capsule.
        if not candidates and not source.checkpoint_publish:
            if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM recovery_checkpoint_active)'):
                return None
        for candidate in candidates:
            try:
                checkpoint = decode_checkpoint(bytes(candidate['state']), candidate['checksum'])
                prefix = checkpoint['coverage']
                frontier = candidate['frontier']
                if (type(frontier) is not int or prefix['position'] != frontier
                        or not point['replay_floor'] <= frontier <= point['position']):
                    raise CheckpointInvalid('checkpoint frontier is outside retained change coverage')
                changes = await store.changes_after(frontier, point['position'])
                if len(changes) > MAX_TAIL_ROWS:
                    raise CheckpointInvalid('incremental change budget exceeded')
                if any(row['operation'] in {'T','U'} or
                       (row['operation']=='D' and row['entity_type'] not in RETENTION_TYPES)
                       for row in changes):
                    raise CheckpointInvalid('structural history mutation requires complete replay')
                legacy = prefix['legacy_rows'] - sum(row['operation']=='D' and row['had_position'] is None for row in changes)
                if legacy != point['legacy_rows']:
                    raise CheckpointInvalid('legacy prefix coverage changed unexpectedly')
                inserted = [row['position'] for row in changes if row['operation']=='I']
                raw_tail = await _rows(conn, 'SELECT * FROM fabric_events WHERE projection_position=ANY($1::bigint[]) '
                                       'ORDER BY projection_position', inserted) if inserted else []
                tail_by_pair, changes_by_pair = {}, {}
                for row in raw_tail:
                    tail_by_pair.setdefault((row['entity_type'], row['entity_id']), []).append(row)
                for row in changes:
                    changes_by_pair.setdefault((row['entity_type'], row['entity_id']), []).append(row)
                private_changes = [row for row in changes if row['entity_type'] == 'inspector']
                inspector_append = False
                if private_changes:
                    order = InspectorStore(clock=getattr(source, '_checkpoint_inspector_clock', time.time))._replay_order
                    tail = [row for row in raw_tail if row['entity_type'] == 'inspector']
                    key = checkpoint['inspector']['replay']['key']
                    inspector_append = (all(row['operation']=='I' for row in private_changes)
                        and len(tail)==len(private_changes)
                        and (key is None or min(order(row) for row in tail) > tuple(key)))
                    if not inspector_append:
                        raise CheckpointInvalid('inspector mutation requires complete authoritative replay')
                pairs = sorted({(row['entity_type'],row['entity_id']) for row in changes})
                if inspector_append:
                    pairs = [pair for pair in pairs if pair[0] != 'inspector']
                append_pairs = set()
                cursors = checkpoint.get('core_cursors', {})
                for kind, identity in pairs:
                    if kind not in COMPACT_TYPES:
                        continue
                    key = f'{kind}|{identity}'
                    tail = tail_by_pair.get((kind, identity), [])
                    operations = changes_by_pair[(kind, identity)]
                    previous = cursors.get(key)
                    if (all(row['operation']=='I' for row in operations) and len(tail)==len(operations)
                            and ((previous is None and key not in checkpoint['repo']['events'])
                                 or (previous is not None and min(event_time(row) for row in tail)>previous))):
                        append_pairs.add((kind,identity))
                pairs = [pair for pair in pairs if pair not in append_pairs]
                if any(not kind or not identity for kind,identity in pairs):
                    raise CheckpointInvalid('untyped durable changes require complete replay')
                streams = {pair:[] for pair in pairs}
                if pairs:
                    raw = await _rows(conn,
                        'SELECT events.* FROM fabric_events AS events JOIN '
                        'unnest($1::text[],$2::text[]) AS dirty(kind,identity) '
                        'ON events.entity_type=dirty.kind AND events.entity_id=dirty.identity '
                        'ORDER BY events.ingest_timestamp,events.sequence,events.event_id',
                        [p[0] for p in pairs],[p[1] for p in pairs],maximum=MAX_DIRTY_ROWS)
                    for row in raw:
                        streams[(row['entity_type'],row['entity_id'])].append(public_event(row))
                coverage = {**point,
                    'raw_rows': prefix['raw_rows'] + sum(1 if row['operation']=='I' else -1 for row in changes),
                    'public_rows': prefix['public_rows'] + sum((1 if row['operation']=='I' else -1)
                                                               for row in changes if feed_visible(row))}
                return dict(checkpoint=checkpoint, full_streams=streams,
                    tail_rows=[public_event(row) for row in raw_tail], structured=structured,
                    coverage=coverage, schema=schema, scanned=sum(len(s) for s in streams.values())+len(raw_tail))
            except (CheckpointInvalid, KeyError, TypeError, ValueError) as exc:
                log.warning('projection checkpoint candidate rejected: %s',type(exc).__name__)
        raw = await _rows(conn, 'SELECT * FROM fabric_events ORDER BY '
            '(projection_position IS NOT NULL),projection_position NULLS FIRST,ingest_timestamp,sequence,event_id')
        return dict(checkpoint=None,rows=raw,structured=structured,
                    coverage={**point,'raw_rows':len(raw),'public_rows':sum(feed_visible(row) for row in raw)},
                    schema=schema,scanned=len(raw))


def _reset_private(repo):
    empty = InspectorStore(limits=repo.inspector.limits,clock=repo.inspector._clock,
                           store_reasoning=repo.inspector.store_reasoning)
    repo.inspector.restore_checkpoint(empty.export_checkpoint())
    repo.host_metrics = repo._new_host_metrics()
    repo._collab_replay = []
    repo.collab_quarantine = []
    repo.collab_quarantine_reasons = []


def recover_repository(repo, source):
    if not source.checkpoint_publish:
        return _recover_repository(repo, source)
    # Hold one session lock across capture, validation and publication. A second
    # worker or prewarm must not prune change coverage underneath a materializer.
    async def connect():
        import asyncpg
        if source._conn is None or source._conn.is_closed():
            source._conn = await asyncpg.connect(source._dsn)
    source._run(connect())
    ownership = ProjectionStore(source._conn).publisher()
    source._run(ownership.__aenter__())
    try:
        return _recover_repository(repo, source)
    finally:
        source._run(ownership.__aexit__(None, None, None))


def _validate_publication(repo, capsule, structured):
    """Exercise every restore codec on a detached repository before publishing."""
    from .projection_store import encode_checkpoint
    from .repo import ControlPlaneRepo
    packed, checksum = encode_checkpoint(capsule)
    decoded = decode_checkpoint(packed, checksum)
    detached = ControlPlaneRepo(now=repo._now, service_archive_days=repo._service_archive_days,
                                host_aliases=repo._host_aliases)
    detached.inspector = InspectorStore(limits=repo.inspector.limits,
        clock=repo.inspector._clock, store_reasoning=repo.inspector.store_reasoning)
    restore_projection(detached, decoded, full_streams={}, tail_rows=[],
                       structured=structured, coverage=capsule['coverage'])
    if detached.ledger.export_checkpoint() != repo.ledger.export_checkpoint():
        raise CheckpointInvalid('source continuity changed during publication validation')


def _recover_repository(repo, source):
    from .bridge import _load_full_into
    start = time.monotonic()
    source._checkpoint_inspector_clock = repo.inspector._clock
    code,policy = code_fingerprint(),policy_fingerprint(repo)
    prepared = source._run(_read_prepared(source,code,policy))
    if prepared is None:
        source.recovery_info={'mode':'legacy_full_replay','reason':'checkpoint_schema_unavailable'}
        return None
    checkpoint = prepared.pop('checkpoint')
    if checkpoint is not None:
        try:
            counts = restore_projection(repo,checkpoint,
                full_streams=prepared['full_streams'],tail_rows=prepared['tail_rows'],
                structured=prepared['structured'],coverage=prepared['coverage'])
            mode='checkpoint'
        except (ValueError,KeyError,TypeError) as exc:
            log.warning('projection restore rejected; complete replay required: %s',type(exc).__name__)
            _reset_private(repo)
            prepared = source._run(_read_full_prepared(source))
            checkpoint=None
    if checkpoint is None:
        rows = prepared.pop('rows')
        ledger = SourceLedger(now=repo._now)
        ledger.hydrate(rows,preserve_order=True)
        # Admission order is authoritative for the ledger. Business reducers
        # retain the legacy deterministic ingest/sequence/event-ID ordering,
        # including equal-sequence rows admitted by different principals.
        rows.sort(key=lambda row: (row['ingest_timestamp'].isoformat(), row['sequence'],
                                  row['event_id'], str((row.get('actor') or {}).get('id') or '')))
        counts = _load_full_into(repo,PreparedSource(prepared['structured'],rows,
            source_ledger=ledger.export_checkpoint()),capture_event_core=True)
        mode='full_replay'
    coverage=prepared['coverage']
    counts.update(events=coverage['raw_rows'])
    if source.checkpoint_publish:
        capsule = capture_projection(repo,repo._checkpoint_event_core,coverage=coverage)
        _validate_publication(repo, capsule, prepared['structured'])
        source._run(ProjectionStore(source._conn).publish(code,policy,coverage['position'],capsule))
    history = PostgresHistory(source._dsn,source._run,schema=prepared['schema'])
    source._history_provider = history
    for (kind,_),stream in repo._events.items():
        if kind in COMPACT_TYPES:
            stream[:] = stream[-1:]
    repo._event_order=[row for row in repo._event_order if row[0] not in COMPACT_TYPES]
    repo._durable_history=history
    repo._durable_public_count=coverage['public_rows']
    repo._durable_row_count=coverage['raw_rows']
    repo._checkpoint_coverage=coverage
    source.recovery_info={'mode':mode,'frontier':coverage['position'],
        'tail_events':len(prepared.get('tail_rows',[])),'event_rows_scanned':prepared['scanned'],
        'elapsed_ms':round((time.monotonic()-start)*1000,2)}
    source._cache=dict(prepared['structured'])
    log.info('projection recovery mode=%s rows_scanned=%s elapsed_ms=%s',
             mode,prepared['scanned'],source.recovery_info['elapsed_ms'])
    return counts


async def _read_full_prepared(source):
    async with ProjectionStore(source._conn).snapshot() as point:
        conn=source._conn
        structured={name:[dict(r) for r in await conn.fetch(f'SELECT * FROM {name}')]
                    for name in ('missions','sessions','services','hosts')}
        rows=await _rows(conn,'SELECT * FROM fabric_events ORDER BY '
            '(projection_position IS NOT NULL),projection_position NULLS FIRST,ingest_timestamp,sequence,event_id')
        return dict(rows=rows,structured=structured,schema=await conn.fetchval('SELECT current_schema()'),
            coverage={**point,'raw_rows':len(rows),'public_rows':sum(feed_visible(r) for r in rows)},scanned=len(rows))
