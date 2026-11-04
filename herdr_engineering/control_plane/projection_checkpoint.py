"""Complete durable projection capsules with database-backed historical streams.

Only high-volume core history is compacted here. Every effect remains in the
event-derived core, while raw rows stay in Postgres. Small reducer/graph streams
and private owner capsules retain the state needed by their existing reducers.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .envelope import SourceLedger
from .host_metrics_store import ENTITY_TYPE as HOST_METRICS_ENTITY
from .inspector import InspectorStore
from .projection_store import CheckpointInvalid

COMPACT_TYPES = frozenset({'service', 'session', 'host'})
CORE_FIELDS = ('missions', 'sessions', 'hosts', 'services', 'artifacts')


def code_fingerprint():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for name in ('cli.py', 'host_policy.py', 'llm_session_id.py'):
        path = root.parent / name
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def policy_fingerprint(repo):
    value = {'inspector_limits': repo.inspector.limits,
             'reasoning': repo.inspector._min_reasoning,
             'sensitive': repo.inspector._min_sensitive,
             'store_reasoning': repo.inspector.store_reasoning,
             'archive_days': repo._service_archive_days,
             'host_aliases': repo._host_aliases}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def capture_projection(repo, event_core, *, coverage):
    """Capture only explicit data; no subscribers, locks, transports or commands."""
    state = repo.export_state(include_history=False)
    for name in CORE_FIELDS:
        state[name] = copy.deepcopy(event_core[name])
    state['source_ledger'] = repo.ledger.export_checkpoint()
    # Delivery and stage projections are recomputed from the small complete
    # reducer capsules, never from the compacted service/session/host history.
    state.pop('delivery', None)
    return {'schema': 1, 'coverage': copy.deepcopy(coverage), 'repo': state,
            'inspector': repo.inspector.export_checkpoint(),
            'host_metrics': repo.host_metrics.export_checkpoint(),
            'host_projection': copy.deepcopy(getattr(repo, '_checkpoint_host_projection', {})),
            'mission_activity': repo.mission_activity.export_checkpoint(),
            'collab': copy.deepcopy(repo._collab_replay + repo.collab_quarantine)}


def _event_streams(rows):
    from .bridge import _rebuild_events
    return _rebuild_events(rows)[0]


def _inspector_after_tail(repo, state, full_streams, tail_rows):
    dirty = {identity for kind, identity in full_streams if kind == 'inspector'}
    if not dirty:
        return copy.deepcopy(state)
    raw = [row for (kind, _), rows in full_streams.items() if kind == 'inspector' for row in rows]
    scratch = InspectorStore(limits=repo.inspector.limits, clock=repo.inspector._clock,
                             store_reasoning=repo.inspector.store_reasoning)
    scratch.hydrate(raw)
    fresh = scratch.export_checkpoint()
    merged = copy.deepcopy(state)
    sessions = {row['session_id']: row for row in merged['sessions'] if row['session_id'] not in dirty}
    sessions.update({row['session_id']: row for row in fresh['sessions']})
    # The domain's actual retention/role policy is reapplied on restore.
    merged['sessions'] = sorted(sessions.values(), key=lambda row: (row['last_ingest'], row['session_id']))
    if len(merged['sessions']) > repo.inspector.limits['max_sessions']:
        cutoff = len(merged['sessions']) - repo.inspector.limits['max_sessions']
        if merged['sessions'][cutoff-1]['last_ingest'] == merged['sessions'][cutoff]['last_ingest']:
            raise CheckpointInvalid('ambiguous inspector eviction boundary requires complete replay')
        merged['sessions'] = merged['sessions'][cutoff:]
    # Counters describe hydration work, not a second execution of historical
    # commands. Apply only actual tail receipts to the existing diagnostic state.
    counters = InspectorStore(limits=repo.inspector.limits, clock=repo.inspector._clock,
                              store_reasoning=repo.inspector.store_reasoning)
    counters.restore_checkpoint(state)
    counters.hydrate([row for row in tail_rows if row.get('entity_type') == 'inspector'])
    merged['stats'] = counters.export_checkpoint()['stats']
    if fresh['global_prefs']:
        merged['global_prefs'] = fresh['global_prefs']
    return merged


def _host_metrics_after_tail(repo, state, host_projection, full_streams):
    dirty = {identity for kind, identity in full_streams if kind == HOST_METRICS_ENTITY}
    if not dirty:
        return copy.deepcopy(state), copy.deepcopy(host_projection)
    scratch = repo._new_host_metrics()
    projected = scratch.hydrate([row for (kind, _), rows in full_streams.items()
                                 if kind == HOST_METRICS_ENTITY for row in rows])
    fresh = scratch.export_checkpoint()
    merged, views = copy.deepcopy(state), copy.deepcopy(host_projection)
    for key in ('raw', 'rollups', 'open', 'state'):
        for identity in dirty:
            merged[key].pop(identity, None)
        merged[key].update(fresh[key])
    for identity in dirty:
        views.pop(identity, None)
    views.update(projected)
    merged['sequence'] = max(merged['sequence'], fresh['sequence'])
    return merged, views


def restore_projection(repo, checkpoint, *, full_streams, tail_rows, structured, coverage):
    """Prepare a fresh repository. It is not published until every owner restores."""
    from .bridge import _reconstruct_from_events, _load_full_into

    if checkpoint.get('schema') != 1 or not isinstance(checkpoint.get('repo'), dict):
        raise CheckpointInvalid('invalid complete projection capsule')
    state = copy.deepcopy(checkpoint['repo'])
    event_core = {name: state[name] for name in CORE_FIELDS}
    # Late/backdated rows can carry partial fields; recompute the entire affected
    # core stream instead of incorrectly overlaying only the last tail payload.
    dirty_core = {key: rows for key, rows in full_streams.items() if key[0] in COMPACT_TYPES or key[0] in {'mission', 'artifact'}}
    if dirty_core:
        rebuilt = _reconstruct_from_events({f'{kind}|{identity}': rows for (kind, identity), rows in dirty_core.items()})
        by_type = {'mission': 'missions', 'session': 'sessions', 'host': 'hosts', 'service': 'services', 'artifact': 'artifacts'}
        for (kind, identity) in dirty_core:
            event_core[by_type[kind]].pop(identity, None)
        for name, records in zip(CORE_FIELDS, rebuilt):
            event_core[name].update(records)

    streams = state['events']
    for (kind, identity), rows in full_streams.items():
        key = f'{kind}|{identity}'
        if kind in {'inspector', HOST_METRICS_ENTITY}:
            continue
        if rows:
            streams[key] = copy.deepcopy(rows[-1:] if kind in COMPACT_TYPES else rows)
        else:
            streams.pop(key, None)
    # These private owner streams are not in the shared event cache. Reassemble
    # them explicitly so the existing classifier handles every row identically.
    collab = _event_streams(checkpoint.get('collab', []))
    for (kind, identity), rows in full_streams.items():
        if kind == 'collab':
            collab[f'{kind}|{identity}'] = rows
    streams.update(collab)

    ledger = SourceLedger(now=repo._now)
    ledger.restore_checkpoint(state['source_ledger'])
    ledger.replay_tail(tail_rows)
    state['source_ledger'] = ledger.export_checkpoint()
    inspector = _inspector_after_tail(repo, checkpoint['inspector'], full_streams, tail_rows)
    metrics, host_projection = _host_metrics_after_tail(repo, checkpoint['host_metrics'],
                                                       checkpoint['host_projection'], full_streams)
    rows = [row for stream in streams.values() for row in stream]
    rows.sort(key=lambda row: (row.get('ingest_timestamp') or '', row.get('sequence') or 0,
                              row.get('event_id') or ''))
    source = PreparedSource(structured, rows, source_ledger=state['source_ledger'])
    # Carry explicit non-event core ownership such as scheduler state. Full
    # reducer capsules rebuild delivery/stages without executing commands.
    counts = _load_full_into(repo, source, core_override=event_core,
                            projection_state=state, capture_event_core=True)
    repo.inspector.restore_checkpoint(inspector)
    repo.host_metrics.restore_checkpoint(metrics)
    repo.mission_activity.restore_checkpoint(checkpoint['mission_activity'])
    repo._checkpoint_host_projection = host_projection
    for identity, view in host_projection.items():
        if identity in repo._hosts:
            repo._hosts[identity].update({key: copy.deepcopy(value) for key, value in view.items() if value is not None})
    repo._checkpoint_coverage = copy.deepcopy(coverage)
    return counts


class PreparedSource:
    """Transferred snapshot rows; no subsequent database query is implicit."""
    def __init__(self, structured, rows, *, source_ledger=None):
        self.structured = structured
        self.rows = rows
        self.source_ledger = source_ledger

    def missions(self):
        return self.structured['missions']

    def sessions(self):
        return self.structured['sessions']

    def services(self):
        return self.structured['services']

    def hosts(self):
        return self.structured['hosts']

    def events(self):
        return self.rows

    def take_events(self):
        rows, self.rows = self.rows, []
        return rows
