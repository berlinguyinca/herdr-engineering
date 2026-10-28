"""Pure, deterministic reconciliation of the complete private identity journal.

Planning never relaxes ordinary registry hydration and performs no I/O. A repair
marker authorizes replacing only its exact covered history with a recomputed,
validated baseline. Every other identity event remains a strict replay tail,
including transactions whose ingest clocks precede the marker.
"""
from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from collections import defaultdict

from .agent_identity import AgentIdentityRegistry, _handle, _suffix

_IDENTITY = 'agent_identity'
_UPDATED = 'agent_identity.updated'
_MARKER = 'agent_identity_reconciliation'
_RECONCILED = 'agent_identity.reconciled'


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('identity repair requires JSON-serializable events') from exc


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _order(event):
    """Match journal arrival ordering, with an explicit stable event-ID tie break."""
    stamp = event.get('ingest_timestamp') or ''
    sequence = event.get('sequence', 0)
    if not isinstance(stamp, str) or type(sequence) is not int or sequence < 0:
        raise ValueError('invalid identity event arrival metadata')
    return stamp, sequence, event.get('event_id') or ''


def _collect(events, *, require_ids, include_markers=False):
    unique = {}
    result = []
    for source in events:
        if not isinstance(source, dict):
            raise ValueError('identity events must be dictionaries')
        kind = source.get('entity_type')
        if kind == _IDENTITY and source.get('event_type') == _UPDATED:
            pass
        elif include_markers and kind == _MARKER:
            if source.get('event_type') != _RECONCILED:
                raise ValueError('invalid identity reconciliation event type')
        else:
            continue
        # The insertion nonce is private database metadata, not journal content.
        event = copy.deepcopy({k: v for k, v in source.items() if k != 'recovery_instance'})
        event_id = event.get('event_id')
        if event_id is None and not require_ids and kind != _MARKER:
            _order(event)
            result.append(event)
            continue
        if not isinstance(event_id, str) or not event_id:
            raise ValueError('identity repair requires non-empty event IDs')
        _order(event)
        fingerprint = _digest(event)
        if event_id in unique:
            if unique[event_id] != fingerprint:
                raise ValueError('divergent identity event ID content')
            continue
        unique[event_id] = fingerprint
        result.append(event)
    return sorted(result, key=_order)


def _record(event):
    """Validate in isolation so divergent historical versions cannot hide errors."""
    try:
        AgentIdentityRegistry().hydrate([event])
        payload = event['payload']
        name = payload['display_name']
        return {
            'identity': payload['identity'], 'display_name': name,
            'handle': payload['handle'], 'version': payload['version'],
            'reserved_names': sorted(set(payload.get('reserved_names', [name.casefold()]))),
            'reserved_handles': sorted(set(payload.get('reserved_handles', [_handle(name)]))),
        }
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed persisted agent identity') from exc


def _envelope(record, event_id, *, stamp='', sequence=0):
    return {'event_id': event_id, 'entity_type': _IDENTITY,
            'entity_id': record['identity'], 'event_type': _UPDATED,
            'sequence': sequence, 'ingest_timestamp': stamp, 'payload': copy.deepcopy(record)}


def plan_identity_repair(events):
    """Return a JSON plan for all identity updates; ignore other event types.

    Input IDs are mandatory and exact duplicates are coalesced. Winners maximize
    (version, ingest_timestamp, sequence, event_id), independent of input order.
    All historical claims survive as either exclusive reservations or permanent
    blocked aliases. Only changed current names or reservation sets increment a
    winner's maximum version. No correction is needed for unchanged winners.
    """
    history = _collect(events, require_ids=True)
    names = defaultdict(set)
    handles = defaultdict(set)
    latest = {}
    for event in history:
        record = _record(event)
        identity = record['identity']
        # These are reservation keys, not display names: casefold can expand a
        # valid 80-character display name beyond the display-name length limit.
        normalized_names = {' '.join(unicodedata.normalize('NFKC', name).split()).casefold()
                            for name in record['reserved_names']}
        normalized_handles = {_handle(handle) for handle in record['reserved_handles']}
        # A historical display reference also reserves its normalized handle,
        # even if an old payload forgot that reservation.
        normalized_handles.update(_handle(name) for name in normalized_names)
        for name in normalized_names:
            names[name].add(identity)
        for handle in normalized_handles:
            handles[handle].add(identity)
        rank = (record['version'], *_order(event))
        if identity not in latest or rank > latest[identity][0]:
            latest[identity] = (rank, record)

    blocked_names = sorted(name for name, owners in names.items() if len(owners) > 1)
    blocked_handles = sorted(handle for handle, owners in handles.items() if len(owners) > 1)
    owned_names = defaultdict(set)
    owned_handles = defaultdict(set)
    for name, owners in names.items():
        if len(owners) == 1:
            owned_names[next(iter(owners))].add(name)
    for handle, owners in handles.items():
        if len(owners) == 1:
            owned_handles[next(iter(owners))].add(handle)

    records, corrections, changes = [], [], []
    for identity, (_, before) in sorted(latest.items()):
        name = before['display_name']
        base = name
        suffix_index = 0
        while (names.get(name.casefold(), {identity}) != {identity}
               or handles.get(_handle(name), {identity}) != {identity}):
            suffix = _suffix(suffix_index)
            name = f'{base[:79 - len(suffix)].rstrip()} {suffix}'
            suffix_index += 1
        names[name.casefold()].add(identity)
        handles[_handle(name)].add(identity)
        after = {**before, 'display_name': name, 'handle': _handle(name),
                 'reserved_names': sorted(owned_names[identity] | {name.casefold()}),
                 'reserved_handles': sorted(owned_handles[identity] | {_handle(name)})}
        if after != before:
            after['version'] = before['version'] + 1
            corrections.append(copy.deepcopy(after))
            changes.append({'identity': identity, 'before': before, 'after': copy.deepcopy(after)})
        records.append(after)

    # Fresh global hydration proves the proposed baseline is mutually unique.
    AgentIdentityRegistry().hydrate([_envelope(record, f'baseline:{index}')
                                     for index, record in enumerate(records)])
    if any(set(r['reserved_names']) & set(blocked_names)
           or set(r['reserved_handles']) & set(blocked_handles) for r in records):
        raise ValueError('repaired baseline contains blocked aliases')
    covered = sorted(({'event_id': e['event_id'], 'digest': _digest(e)} for e in history),
                     key=lambda entry: entry['event_id'])
    input_digest = _digest(covered)
    return {'policy_version': 1, 'repair_id': f'agent-identity-repair-v1-{input_digest}',
            'input_digest': input_digest, 'covered': covered, 'records': records,
            'corrections': corrections, 'blocked_names': blocked_names,
            'blocked_handles': blocked_handles, 'changes': changes}


def prepare_identity_replay(events):
    """Return (strict identity envelopes, blocked names, blocked handles).

    Without a marker this is ordinary ordered history, including legacy envelopes
    without event IDs. With markers only the latest by arrival/sequence/ID is
    authoritative. Its raw coverage, complete plan, and exact correction payload
    set must validate before a baseline can replace covered events. Uncovered
    events are never filtered using a timestamp watermark. The caller hydrates
    the result with AgentIdentityRegistry and the returned blocked alias sets.
    """
    events = list(events)
    has_marker = any(isinstance(e, dict) and e.get('entity_type') == _MARKER for e in events)
    history = _collect(events, require_ids=has_marker, include_markers=True)
    identities = [e for e in history if e['entity_type'] == _IDENTITY]
    for event in identities:
        _record(event)
    if not has_marker:
        return identities, [], []

    marker = next(e for e in reversed(history) if e['entity_type'] == _MARKER)
    payload = marker.get('payload')
    if not isinstance(payload, dict) or set(payload) != {'plan', 'correction_event_ids'}:
        raise ValueError('malformed identity reconciliation marker')
    plan = payload['plan']
    if not isinstance(plan, dict) or not isinstance(plan.get('covered'), list):
        raise ValueError('malformed identity reconciliation plan')
    by_id = {e['event_id']: e for e in identities}
    covered = {}
    for entry in plan['covered']:
        if not isinstance(entry, dict) or set(entry) != {'event_id', 'digest'}:
            raise ValueError('malformed identity repair coverage')
        event_id = entry['event_id']
        if not isinstance(event_id, str) or event_id in covered or event_id not in by_id:
            raise ValueError('missing or duplicate identity repair coverage')
        event = by_id[event_id]
        if entry['digest'] != _digest(event):
            raise ValueError('identity repair coverage fingerprint mismatch')
        covered[event_id] = event
    expected = plan_identity_repair(covered.values())
    if _json(plan) != _json(expected):
        raise ValueError('identity reconciliation plan does not match covered history')

    correction_ids = payload['correction_event_ids']
    if (not isinstance(correction_ids, list)
            or any(not isinstance(i, str) or not i for i in correction_ids)
            or len(set(correction_ids)) != len(correction_ids)
            or len(correction_ids) != len(expected['corrections'])):
        raise ValueError('invalid identity repair correction event set')
    corrections = {r['identity']: r for r in expected['corrections']}
    for event_id in correction_ids:
        if event_id in covered or event_id not in by_id:
            raise ValueError('missing or covered identity repair correction event')
        event = by_id[event_id]
        record = corrections.pop(event.get('entity_id'), None)
        if record is None or _json(event.get('payload')) != _json(record):
            raise ValueError('identity repair correction payload mismatch')
    if corrections:
        raise ValueError('missing identity repair corrections')

    baseline = [_envelope(record, f"{expected['repair_id']}:{index}",
                          stamp=marker.get('ingest_timestamp') or '',
                          sequence=marker.get('sequence', 0))
                for index, record in enumerate(expected['records'])]
    excluded = set(covered) | set(correction_ids)
    tail = [e for e in identities if e['event_id'] not in excluded]
    return baseline + tail, expected['blocked_names'], expected['blocked_handles']
