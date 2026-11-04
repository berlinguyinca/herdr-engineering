"""Explicit projection codecs: JSON ownership, recovery and runtime boundaries."""
import copy
import json
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.envelope import EnvelopeConflict, SourceLedger
from herdr_engineering.control_plane.host_metrics_store import HostMetricsStore
from herdr_engineering.control_plane.inspector import InspectorStore
from herdr_engineering.control_plane.mission_activity import MissionActivityStore

T0 = 1_800_000_000.0


def stamp(t=T0):
    return datetime.fromtimestamp(t, UTC).isoformat()


def roundtrip(store):
    return json.loads(json.dumps(store.export_checkpoint(), allow_nan=False))


def env(seq, actor='a', eid=None):
    return dict(event_id=eid or str(seq), source='pi', source_sequence=seq,
                actor={'id': actor}, content_hash=str(seq), received_at=stamp())


def test_ledger_roundtrip_tail_preserves_cache_gaps_and_principal_ownership():
    calls = []
    source = SourceLedger(now=lambda: T0, max_events=3)
    for e in (env(1), env(4), env(1, 'b')):
        source.commit(e)
    state = roundtrip(source)
    target = SourceLedger(now=lambda: T0, max_events=3, persist=calls.append)
    for _ in range(2):
        target.restore_checkpoint(state)
        assert target.source_states() == source.source_states()
        assert target.check(env(1)) == 'duplicate'
        with pytest.raises(EnvelopeConflict):
            target.check(env(4, eid='other'))
    target.replay_tail([dict(event_id='2', source='pi', source_sequence=2,
                             actor={'id': 'a'}, ingest_timestamp=stamp(T0-10),
                             payload={'_envelope': {'content_sha256': '2'}}, replay_position=8)])
    assert target.check(env(2)) == 'duplicate'
    assert target.check(env(4)) == 'duplicate'
    assert target.source_state('pi', 'a')['gaps'][0]['from'] == 3
    assert calls == []
    state['events'].clear()
    assert target.check(env(4)) == 'duplicate'


def populate_inspector(store):
    events = [
        {'kind': 'capabilities', 'reasoning': 'summary'},
        {'kind': 'message', 'event_id': 'm', 'role': 'user', 'text': 'hello'},
        {'kind': 'reasoning', 'text': 'private'},
        {'kind': 'tool', 'event_id': 'tool', 'phase': 'start', 'call_id': 'c', 'tool': 'bash'},
        {'kind': 'test', 'phase': 'start', 'run_id': 'r'},
        {'kind': 'usage', 'input_tokens': 12, 'output_tokens': 3},
        {'kind': 'model', 'model': 'x'},
        {'kind': 'association', 'mission_id': 'm1'},
    ]
    result = store.ingest('w1:p1', {'events': events}, persist=False)
    assert result['accepted'] == len(events), result
    store.set_prefs({'view': 'all'}, persist=False)


def without_epoch(view):
    return {k: v for k, v in view.items() if k != 'epoch'}


def test_inspector_roundtrip_views_aliases_retry_ids_and_runtime_boundaries():
    calls = []
    source = InspectorStore(clock=lambda: T0)
    populate_inspector(source)
    source.subscribe('w1:p1')
    state = roundtrip(source)
    target = InspectorStore(clock=lambda: T0, persist=calls.append)
    old_epoch = target.epoch
    for _ in range(2):
        target.restore_checkpoint(state)
        assert target.epoch != source.epoch and target.epoch != old_epoch
        for role in ('viewer', 'operator'):
            assert without_epoch(target.query('w1:p1', role)) == without_epoch(source.query('w1:p1', role))
        assert not target._sessions['w1:p1'].subs
        sess = target._sessions['w1:p1']
        assert sess.tools['c'] is sess.by_seq[sess.tools['c']['seq']]
        assert sess.tests['r'] is sess.by_seq[sess.tests['r']['seq']]
    assert target.ingest('w1:p1', {'kind': 'message', 'event_id': 'm', 'role': 'user', 'text': 'retry'}, persist=False)['duplicates'] == 1
    target.ingest('w1:p1', {'kind': 'tool', 'phase': 'end', 'call_id': 'c', 'exit_code': 0}, persist=False)
    assert len([e for e in target._sessions['w1:p1'].events if e['kind'] == 'tool']) == 1
    assert calls == []
    stricter = InspectorStore(clock=lambda: T0, reasoning_roles='operator', sensitive_roles='operator')
    stricter.restore_checkpoint(state)
    assert all(e['data'].get('withheld') and e['data'].get('text') is None
               for e in stricter.query('w1:p1', 'viewer')['events'] if e['kind'] == 'reasoning')


def test_host_metrics_roundtrip_open_bucket_and_ttl_without_writes():
    clock = [T0]
    calls = []
    source = HostMetricsStore(now=lambda: clock[0])
    source.record('h', {'cpu': 10})
    clock[0] += 300
    source.record('h', {'cpu': 20})
    source.observe_state('h', {'maintenance': True}, {'cpu': 20})
    state = roundtrip(source)
    target = HostMetricsStore(now=lambda: clock[0], persist=calls.append)
    for _ in range(2):
        target.restore_checkpoint(state)
        for window in ('1h', '24h', '7d'):
            assert target.history('h', window) == source.history('h', window)
    assert calls == []
    clock[0] += 300
    target.record('h', {'cpu': 30})
    assert target.rollup_count('h') == 2
    assert len(calls) == 1
    calls.clear()
    clock[0] += 8 * 86400
    target.restore_checkpoint(state)
    assert not target.has('h')
    assert calls == []


def activity_payload():
    return dict(contract_version=1, host_id='h', instance_id='i', session_id='s',
                mission_revision=1, connection='connected', observed_at=stamp(),
                snapshot_at=stamp(), mission={'id': 'm', 'revision': 1}, activity={'progress': 12})


def test_activity_roundtrip_keeps_receive_clock_order_and_expiry():
    clock = [T0]
    source = MissionActivityStore(now=lambda: clock[0])
    assert source.ingest('pi:m', activity_payload(), source='pi')['accepted']
    state = roundtrip(source)
    clock[0] += 40
    target = MissionActivityStore(now=lambda: clock[0])
    for _ in range(2):
        target.restore_checkpoint(state)
        assert target.get('pi:m') == source.get('pi:m')
        assert target.get('pi:m')['freshness']['state'] == 'stale'
    clock[0] += 86400
    target.restore_checkpoint(state)
    assert target.get('pi:m') is None


@pytest.mark.parametrize('factory', [SourceLedger, InspectorStore, HostMetricsStore, MissionActivityStore])
@pytest.mark.parametrize('mutation', [lambda s: s.update(version=2), lambda s: s.update(version=True),
                                     lambda s: s.update(domain='wrong'), lambda s: s.update(extra=1)])
def test_invalid_header_rejected_atomically(factory, mutation):
    store = factory()
    state = roundtrip(store)
    bad = copy.deepcopy(state)
    mutation(bad)
    with pytest.raises(ValueError):
        store.restore_checkpoint(bad)
    assert store.export_checkpoint() == state


def populated(factory):
    if factory is SourceLedger:
        store = factory(now=lambda: T0)
        store.commit(env(1))
        store.commit(env(4))
    elif factory is InspectorStore:
        store = factory(clock=lambda: T0)
        populate_inspector(store)
    elif factory is HostMetricsStore:
        store = factory(now=lambda: T0)
        store.record('h', {'cpu': 42})
    else:
        store = factory(now=lambda: T0)
        store.ingest('pi:m', activity_payload(), source='pi')
    return store


@pytest.mark.parametrize(('factory', 'corrupt'), [
    (SourceLedger, lambda s: s['events'][0].__setitem__(5, 'wrong-owner')),
    (SourceLedger, lambda s: s['events'].append(s['events'][0])),
    (SourceLedger, lambda s: s['sources'][0]['gaps'][0].update(to=999)),
    (SourceLedger, lambda s: s['sources'][0].update(last_sequence=True)),
    (InspectorStore, lambda s: s['sessions'][0]['tools'].update(c=999)),
    (InspectorStore, lambda s: s['sessions'][0]['events'][0].update(seq=True)),
    (InspectorStore, lambda s: s['sessions'][0].update(usage_totals={})),
    (InspectorStore, lambda s: s['sessions'][0]['events'][0].update(recv=float('nan'))),
    (HostMetricsStore, lambda s: s['raw']['h'][0].update(ts='bad')),
    (HostMetricsStore, lambda s: s['open']['h'].update(points={})),
    (HostMetricsStore, lambda s: s.update(sequence=True)),
    (MissionActivityStore, lambda s: s['entries'][0][1].update(received_epoch=T0+1)),
    (MissionActivityStore, lambda s: s['entries'][0][1]['value'].update(source='other')),
    (MissionActivityStore, lambda s: s['entries'][0][1]['value']['activity'].update(prompt='private')),
])
def test_nested_malformed_state_is_rejected_without_partial_replacement(factory, corrupt):
    store = populated(factory)
    baseline = roundtrip(store)
    bad = copy.deepcopy(baseline)
    corrupt(bad)
    with pytest.raises(ValueError):
        store.restore_checkpoint(bad)
    assert roundtrip(store) == baseline


@pytest.mark.parametrize('factory', [SourceLedger, InspectorStore, HostMetricsStore, MissionActivityStore])
def test_replaces_stale_state_owns_json_and_recreates_lock(factory):
    store = populated(factory)
    empty = factory()
    checkpoint = roundtrip(empty)
    lock_name = 'lock' if factory is SourceLedger else '_lock'
    old_lock = getattr(store, lock_name)
    store.restore_checkpoint(checkpoint)
    assert getattr(store, lock_name) is not old_lock
    assert roundtrip(store) == checkpoint
    checkpoint['version'] = 99
    assert store.export_checkpoint()['version'] == 1


def test_ledger_tail_uses_replay_order_not_backdated_timestamps_and_never_persists():
    ledger = SourceLedger(now=lambda: T0, persist=lambda _: pytest.fail('persistence'))
    rows = [dict(event_id=str(seq), source='pi', source_sequence=seq, actor={'id': 'a'},
                 replay_position=pos, ingest_timestamp=stamp(T0-offset),
                 payload={'_envelope': {'content_sha256': str(seq)}})
            for seq, pos, offset in [(1, 1, 0), (4, 2, 5), (2, 3, 10)]]
    ledger.replay_tail(rows)
    assert ledger.source_state('pi', 'a')['gaps'] == [
        {'from': 3, 'to': 3, 'seen_at': stamp(T0-5)}]
    assert ledger.source_state('pi', 'a')['last_seen_at'] == stamp(T0-10)
    before = roundtrip(ledger)
    ledger.replay_tail(rows)
    assert roundtrip(ledger) == before


def test_inspector_current_policy_ttl_and_subscriber_teardown():
    source = InspectorStore(clock=lambda: T0, reasoning_roles='viewer', sensitive_roles='viewer')
    populate_inspector(source)
    state = roundtrip(source)
    target = InspectorStore(clock=lambda: T0, store_reasoning=False)
    populate = target.ingest('other', {'kind': 'message', 'role': 'user', 'text': 'stale'}, persist=False)
    assert populate['accepted'] == 1
    sub = target.subscribe('other')
    target.restore_checkpoint(state)
    assert sub.closed
    assert 'other' not in target._sessions
    assert all(e['kind'] != 'reasoning' for e in target._sessions['w1:p1'].events)
    assert target.store_reasoning is False
    expired = InspectorStore(clock=lambda: T0 + 1000 * 3600)
    expired.restore_checkpoint(state)
    assert expired.query('w1:p1')['events'] == []
    assert not expired._sessions['w1:p1'].tools
    assert not expired._sessions['w1:p1'].tests


def test_inspector_test_end_mutates_existing_ring_entry_and_preserves_usage_summary():
    source = InspectorStore(clock=lambda: T0)
    populate_inspector(source)
    target = InspectorStore(clock=lambda: T0)
    target.restore_checkpoint(roundtrip(source))
    target.ingest('w1:p1', {'kind': 'test', 'run_id': 'r', 'phase': 'end', 'passed': 3}, persist=False)
    tests = [e for e in target._sessions['w1:p1'].events if e['kind'] == 'test']
    assert len(tests) == 1 and tests[0]['data']['passed'] == 3
    assert target.query('w1:p1')['summary']['usage']['input_tokens'] == 12


@pytest.mark.parametrize(('factory', 'corrupt'), [
    (InspectorStore, lambda s: s['sessions'][0]['events'][0].update(kind=[])),
    (InspectorStore, lambda s: s['sessions'][0]['prefs'].update(view=[])),
    (HostMetricsStore, lambda s: s['raw']['h'][0].update(ts=10**400)),
    (HostMetricsStore, lambda s: s['raw']['h'][0].update(unknown_metric=1)),
    (SourceLedger, lambda s: s['sources'][0]['gaps'].append(copy.deepcopy(s['sources'][0]['gaps'][0]))),
])
def test_schema_errors_are_value_errors(factory, corrupt):
    store = populated(factory)
    state = roundtrip(store)
    corrupt(state)
    with pytest.raises(ValueError):
        store.restore_checkpoint(state)


def test_activity_roundtrip_accepts_submicrosecond_server_clock():
    clock = T0 + .1234567
    store = MissionActivityStore(now=lambda: clock)
    store.ingest('pi:m', activity_payload(), source='pi')
    restored = MissionActivityStore(now=lambda: clock)
    restored.restore_checkpoint(roundtrip(store))
    assert restored.get('pi:m') == store.get('pi:m')


def test_restore_applies_current_domain_budgets():
    source = populated(SourceLedger)
    target = SourceLedger(max_events=1, now=lambda: T0)
    target.restore_checkpoint(roundtrip(source))
    assert len(target._events) == len(target._sequences) == 1
    assert target.check(env(4)) == 'duplicate'
    inspector = InspectorStore(clock=lambda: T0, limits={'max_events_per_session': 2})
    inspector.restore_checkpoint(roundtrip(populated(InspectorStore)))
    assert len(inspector.query('w1:p1')['events']) == 2
    activity = populated(MissionActivityStore)
    payload = activity_payload()
    payload['mission']['id'] = 'other'
    activity.ingest('pi:other', payload, source='pi')
    limited = MissionActivityStore(now=lambda: T0, max_missions=1)
    limited.restore_checkpoint(roundtrip(activity))
    assert limited.get('pi:m') is None
    assert limited.get('pi:other') is not None


def test_inspector_restore_retains_current_auth_policy_and_hides_sensitive_search():
    source = InspectorStore(clock=lambda: T0, sensitive_roles='viewer', reasoning_roles='viewer')
    source.ingest('s', {'kind': 'tool', 'call_id': 'secret', 'phase': 'end',
                        'tool': 'bash', 'args_preview': 'cat .env', 'output': 'private-result'}, persist=False)
    assert source.query('s', 'viewer')['events'][0]['data']['output'] == 'private-result'
    target = InspectorStore(clock=lambda: T0)
    target.restore_checkpoint(roundtrip(source))
    event = target.query('s', 'viewer')['events'][0]
    assert event['data']['withheld'] and event['data']['output'] is None
    assert target.query('s', 'viewer', q='private-result')['events'] == []
    assert target.query('s', 'operator')['events'][0]['data']['output'] == 'private-result'


@pytest.mark.parametrize('factory', [SourceLedger, InspectorStore, HostMetricsStore, MissionActivityStore])
@pytest.mark.parametrize('bad', [None, [], {}, {'version': 1},
                               {'version': 1, 'domain': 'x', 'bad': (1, 2)}])
def test_incomplete_or_non_json_schemas_raise_value_error(factory, bad):
    with pytest.raises(ValueError):
        factory().restore_checkpoint(bad)


@pytest.mark.parametrize('factory', [SourceLedger, InspectorStore, HostMetricsStore, MissionActivityStore])
def test_mutating_export_and_restore_inputs_cannot_mutate_store(factory):
    source = populated(factory)
    expected = roundtrip(source)
    exported = source.export_checkpoint()
    exported.clear()
    assert roundtrip(source) == expected
    target = populated(factory)
    target.restore_checkpoint(expected)
    snapshot = roundtrip(target)
    for value in expected.values():
        if isinstance(value, (dict, list)):
            value.clear()
    assert roundtrip(target) == snapshot


def test_ledger_hydrate_preserve_order_is_opt_in_and_does_not_publish_sql_metadata():
    rows = [dict(event_id=str(seq), source='pi', source_sequence=seq, actor={'id': 'a'},
                 projection_position=position, ingest_timestamp=stamp(T0-offset),
                 payload={'_envelope': {'content_sha256': str(seq)}})
            for seq, position, offset in [(1, 1, 0), (4, 2, 5), (2, 3, 10)]]
    calls = []
    supplied = SourceLedger(now=lambda: T0, persist=calls.append)
    supplied.commit(env(99, 'old'), persist=False)
    supplied.hydrate(rows, preserve_order=True)
    incremental = SourceLedger(now=lambda: T0)
    incremental.replay_tail(rows)
    assert roundtrip(supplied) == roundtrip(incremental)
    assert supplied.last_sequence({'id': 'old'}, 'pi') is None
    assert 'projection_position' not in json.dumps(roundtrip(supplied))
    legacy = SourceLedger(now=lambda: T0)
    legacy.hydrate(rows)
    explicit_legacy = SourceLedger(now=lambda: T0)
    explicit_legacy.hydrate(rows, preserve_order=False)
    assert roundtrip(legacy) == roundtrip(explicit_legacy)
    assert legacy.source_state('pi', 'a')['last_seen_at'] == stamp(T0)
    assert supplied.source_state('pi', 'a')['last_seen_at'] == stamp(T0-10)
    assert calls == []
