"""Recover every durable projection from a verified prefix and affected streams."""
from __future__ import annotations

import logging
import time
from typing import NamedTuple

from .envelope import SourceLedger
from .inspector import InspectorStore
from .projection_checkpoint import (
    COMPACT_TYPES,
    FOLDABLE_TYPES,
    PreparedSource,
    actor_id,
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
                             'ci_report','ci_observation','ci_telemetry','ci_pipeline'})
# Fabric compaction deletes interior service observation rows. The change log carries no event_type, so any
# service delete is tolerated; the stream is then rebuilt from its surviving rows (see _append_blocker).
COMPACTED_TYPES = frozenset({'service'})
MAX_TAIL_ROWS = 100_000
MAX_DIRTY_ROWS = 600_000
HARD_DIRTY_ROWS = 2_000_000
TOP_CONTRIBUTORS = 5


class CheckpointBudgets(NamedTuple):
    """Row budgets for one incremental recovery.

    ``max_tail_rows``: change-log tail. ``max_dirty_rows``: refolded rows the recovery is expected to
    re-read; above it recovery logs and carries on. ``hard_dirty_rows``: the cap above which the dirty
    streams are no cheaper than a complete replay (None = same as ``max_dirty_rows``).
    """
    max_tail_rows: int
    max_dirty_rows: int
    hard_dirty_rows: int | None = None

    @property
    def dirty_cap(self):
        return self.max_dirty_rows if self.hard_dirty_rows is None else self.hard_dirty_rows


def checkpoint_budgets(raw=None):
    """Validate ``control_plane.projection_checkpoint`` (fleet YAML); missing keys take the defaults.

    Raises ValueError naming the key for anything but a positive integer.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('control_plane.projection_checkpoint must be a mapping')
    unknown = sorted(set(raw) - {'max_tail_rows', 'max_dirty_rows', 'hard_dirty_rows'})
    if unknown:
        raise ValueError(f'control_plane.projection_checkpoint: unknown key(s) {unknown}')
    values = {}
    for key, default in (('max_tail_rows', MAX_TAIL_ROWS), ('max_dirty_rows', MAX_DIRTY_ROWS)):
        value = raw.get(key, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f'control_plane.projection_checkpoint.{key} must be a positive integer, got {value!r}')
        values[key] = value
    hard = raw.get('hard_dirty_rows', max(HARD_DIRTY_ROWS, values['max_dirty_rows']))
    if type(hard) is not int or hard <= 0:
        raise ValueError(f'control_plane.projection_checkpoint.hard_dirty_rows must be a positive integer, got {hard!r}')
    if hard < values['max_dirty_rows']:
        raise ValueError('control_plane.projection_checkpoint.hard_dirty_rows must be >= max_dirty_rows')
    return CheckpointBudgets(**values, hard_dirty_rows=hard)


def feed_visible(row):
    kind = row.get('entity_type')
    return bool(kind and row.get('entity_id') and not hidden_history(kind)
                and not ((row.get('source_sequence') is not None or row.get('has_envelope'))
                         and kind in ENVELOPE_ONLY))


async def _rows(conn, sql, *args, maximum=None, explain=None):
    import json
    result = []
    async for row in conn.cursor(sql, *args, prefetch=1000):
        item = dict(row)
        for field in ('payload','actor','evidence_refs'):
            if isinstance(item.get(field), str):
                item[field] = json.loads(item[field])
        result.append(item)
        if maximum is not None and len(result) > maximum:
            described = explain() if explain else None
            if hasattr(described, '__await__'):
                described = await described
            detail = f' ({described})' if described else ''
            raise CheckpointInvalid(f'incremental stream budget exceeded; complete replay required{detail}')
    return result


def _append_blocker(operations, tail, previous, in_prefix_events):
    """Why a compactable stream cannot fold its tail incrementally, or None when it can.

    The tail may be folded onto the captured core only when every change is an
    insert and every new server-ingest clock follows the stream's captured
    maximum. Anything else needs the stream in its authoritative order.
    """
    if not all(row['operation'] == 'I' for row in operations):
        return 'non_insert'
    if len(tail) != len(operations):
        return 'tail_rows_missing'
    if previous is None:
        return None if not in_prefix_events else 'no_cursor'
    return None if min(event_time(row) for row in tail) > previous else 'not_after_cursor'


def _fold_plan(operations, tail):
    """``(removed_keys, None)`` when a foldable stream folds from its change log, else ``(None, reason)``.

    Replays the pair's changes in log order: an insert is alive until a later delete; a delete of
    anything not inserted since the checkpoint removes a checkpointed row. The tail rows read from the
    table must be exactly the rows still alive, otherwise the log and the table disagree.
    """
    alive, removed = set(), set()
    for row in operations:
        key = (row['event_id'], row['actor_id'] or '')
        if row['operation'] == 'I':
            alive.add(key)
        elif row['operation'] == 'D':
            if key in alive:
                alive.discard(key)
            else:
                removed.add(key)
        else:
            return None, 'non_insert_delete'
    if {(row['event_id'], actor_id(row)) for row in tail} != alive:
        return None, 'tail_rows_missing'
    return removed, None


def _budget_summary(pairs, blocked, rows_by_type=None, limit=None):
    kinds = {}
    for kind, _ in pairs:
        kinds[kind] = kinds.get(kind, 0) + 1
    reasons = {f'{kind}:{reason}': count for (kind, reason), count in blocked.items()}
    text = f'dirty streams by type {dict(sorted(kinds.items()))}; compact append blocked {dict(sorted(reasons.items()))}'
    if rows_by_type is None:
        return text
    ranked = sorted(rows_by_type.items(), key=lambda item: (-item[1], item[0]))
    top = ', '.join(f'{kind}={count}' for kind, count in ranked[:TOP_CONTRIBUTORS])
    if len(ranked) > TOP_CONTRIBUTORS:
        top += f', +{len(ranked) - TOP_CONTRIBUTORS} more types'
    return f'dirty rows {sum(rows_by_type.values())} > limit {limit}: {top}; {text}'


async def _dirty_rows_by_type(conn, pairs):
    found = await conn.fetch(
        'SELECT dirty.kind AS kind, count(*) AS rows FROM fabric_events AS events JOIN '
        'unnest($1::text[],$2::text[]) AS dirty(kind,identity) '
        'ON events.entity_type=dirty.kind AND events.entity_id=dirty.identity GROUP BY dirty.kind',
        [p[0] for p in pairs], [p[1] for p in pairs])
    return {row['kind']: row['rows'] for row in found}


async def _explain_overflow(conn, pairs, blocked, limit, hard=False):
    text = _budget_summary(pairs, blocked, await _dirty_rows_by_type(conn, pairs), limit)
    return text.replace(' > limit ', ' > hard cap ', 1) if hard else text


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
                budgets = getattr(source, 'checkpoint_budgets', None) or CheckpointBudgets(MAX_TAIL_ROWS, MAX_DIRTY_ROWS)
                if len(changes) > budgets.max_tail_rows:
                    raise CheckpointInvalid(f'incremental change budget exceeded: {len(changes)} tail rows > limit '
                                            f'{budgets.max_tail_rows}; complete replay required')
                if any(row['operation'] in {'T','U'} or
                       (row['operation']=='D' and row['entity_type'] not in RETENTION_TYPES | COMPACTED_TYPES)
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
                blocked = {}
                cursors = checkpoint.get('core_cursors', {})
                for kind, identity in pairs:
                    if kind not in COMPACT_TYPES:
                        continue
                    key = f'{kind}|{identity}'
                    tail = tail_by_pair.get((kind, identity), [])
                    operations = changes_by_pair[(kind, identity)]
                    previous = cursors.get(key)
                    reason = _append_blocker(operations, tail, previous,
                                             key in checkpoint['repo']['events'])
                    if reason is None:
                        append_pairs.add((kind,identity))
                    else:
                        blocked[(kind, reason)] = blocked.get((kind, reason), 0) + 1
                fold_removed = {}
                for kind, identity in pairs:
                    if kind not in FOLDABLE_TYPES:
                        continue
                    removed, reason = _fold_plan(changes_by_pair[(kind, identity)],
                                                 tail_by_pair.get((kind, identity), []))
                    if reason is None:
                        fold_removed[(kind, identity)] = removed
                    else:
                        blocked[(kind, reason)] = blocked.get((kind, reason), 0) + 1
                pairs = [pair for pair in pairs if pair not in append_pairs and pair not in fold_removed]
                if any(not kind or not identity for kind,identity in pairs):
                    raise CheckpointInvalid('untyped durable changes require complete replay')
                streams = {pair:[] for pair in pairs}
                refolded = 0
                if pairs:
                    raw = await _rows(conn,
                        'SELECT events.* FROM fabric_events AS events JOIN '
                        'unnest($1::text[],$2::text[]) AS dirty(kind,identity) '
                        'ON events.entity_type=dirty.kind AND events.entity_id=dirty.identity '
                        'ORDER BY events.ingest_timestamp,events.sequence,events.event_id',
                        [p[0] for p in pairs],[p[1] for p in pairs],maximum=budgets.dirty_cap,
                        explain=lambda: _explain_overflow(conn, pairs, blocked, budgets.dirty_cap,
                                                          hard=budgets.hard_dirty_rows is not None))
                    for row in raw:
                        streams[(row['entity_type'],row['entity_id'])].append(public_event(row))
                    refolded = len(raw)
                    if refolded > budgets.max_dirty_rows:
                        by_type = {}
                        for row in raw:
                            by_type[row['entity_type']] = by_type.get(row['entity_type'], 0) + 1
                        log.warning('projection recovery over the dirty budget; refolding %d dirty streams from the '
                                    'checkpoint instead of a complete replay (%s)', len(pairs),
                                    _budget_summary(pairs, blocked, by_type, budgets.max_dirty_rows))
                coverage = {**point,
                    'raw_rows': prefix['raw_rows'] + sum(1 if row['operation']=='I' else -1 for row in changes),
                    'public_rows': prefix['public_rows'] + sum((1 if row['operation']=='I' else -1)
                                                               for row in changes if feed_visible(row))}
                return dict(checkpoint=checkpoint, full_streams=streams, fold_removed=fold_removed,
                    tail_rows=[public_event(row) for row in raw_tail], structured=structured,
                    coverage=coverage, schema=schema, refolded_rows=refolded, folded_streams=len(fold_removed),
                    scanned=refolded+len(raw_tail))
            except (CheckpointInvalid, KeyError, TypeError, ValueError) as exc:
                log.warning('projection checkpoint candidate rejected: %s%s',type(exc).__name__,_rejection_detail(exc))
        raw = await _rows(conn, 'SELECT * FROM fabric_events ORDER BY '
            '(projection_position IS NOT NULL),projection_position NULLS FIRST,ingest_timestamp,sequence,event_id')
        return dict(checkpoint=None,rows=raw,structured=structured,
                    coverage={**point,'raw_rows':len(raw),'public_rows':sum(feed_visible(row) for row in raw)},
                    schema=schema,scanned=len(raw))


MAX_REASON_CHARS = 200


def _rejection_detail(exc):
    """`: <reason>` for our own validation errors, bounded and printable.

    Only CheckpointInvalid carries static, module-authored messages; other
    exception types may embed row data, so they stay class-only.
    """
    if not isinstance(exc, CheckpointInvalid):
        return ''
    text = ''.join(ch if ch.isprintable() else ' ' for ch in str(exc)).strip()
    if len(text) > MAX_REASON_CHARS:
        text = text[:MAX_REASON_CHARS - 3] + '...'
    return f': {text}' if text else ''


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
    ownership = ProjectionStore(source._conn).publisher(
        wait=getattr(source, 'checkpoint_lock_wait', 0.0))
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
                fold_removed=prepared['fold_removed'],
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
            source_ledger=ledger.export_checkpoint(),
            inspector_snapshot=source._run(_read_inspector_snapshot(source))),capture_event_core=True)
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
        'refolded_rows':prepared.get('refolded_rows',0),'folded_streams':prepared.get('folded_streams',0),
        'elapsed_ms':round((time.monotonic()-start)*1000,2)}
    source._cache=dict(prepared['structured'])
    log.info('projection recovery mode=%s path=%s rows_scanned=%s tail_events=%s refolded_rows=%s '
             'folded_streams=%s elapsed_ms=%s', mode,
             'checkpoint_plus_tail' if mode == 'checkpoint' else 'complete_replay', prepared['scanned'],
             source.recovery_info['tail_events'], source.recovery_info['refolded_rows'],
             source.recovery_info['folded_streams'], source.recovery_info['elapsed_ms'])
    return counts


async def _read_inspector_snapshot(source):
    """The stored inspector snapshot for a complete replay, or None when disabled, absent or unreadable.

    The repo verifies it against the rows it loaded and replays every inspector event on any doubt, so a complete
    replay (checkpoint missing or rejected) no longer re-runs ~all stored inspector rows through redaction."""
    if not getattr(source,'_inspector_snapshots',False):
        return None
    try:
        row=await source._conn.fetchrow('SELECT schema_version, fingerprint, watermark, event_count, body '
                                        'FROM inspector_snapshots WHERE id = 1')
    except Exception as exc:  # noqa: BLE001 - an optimisation; never block a boot
        log.warning('inspector snapshot unavailable (%s); replaying every inspector event',type(exc).__name__)
        return None
    return dict(row) if row is not None else None


async def _read_full_prepared(source):
    async with ProjectionStore(source._conn).snapshot() as point:
        conn=source._conn
        structured={name:[dict(r) for r in await conn.fetch(f'SELECT * FROM {name}')]
                    for name in ('missions','sessions','services','hosts')}
        rows=await _rows(conn,'SELECT * FROM fabric_events ORDER BY '
            '(projection_position IS NOT NULL),projection_position NULLS FIRST,ingest_timestamp,sequence,event_id')
        return dict(rows=rows,structured=structured,schema=await conn.fetchval('SELECT current_schema()'),
            coverage={**point,'raw_rows':len(rows),'public_rows':sum(feed_visible(r) for r in rows)},scanned=len(rows))
