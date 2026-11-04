import copy

import pytest

from herdr_engineering.control_plane.bridge import _load_full_into
from herdr_engineering.control_plane.projection_checkpoint import (
    PreparedSource,
    capture_projection,
    restore_projection,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

STRUCTURED = {name: [] for name in ('missions', 'sessions', 'services', 'hosts')}


def event(identity, sequence, received, payload, kind='HostHeartbeat', entity_type='host'):
    stamp = f'1970-01-01T00:00:0{received}+00:00'
    return dict(event_id=identity, entity_type=entity_type, entity_id='h', sequence=sequence,
                ingest_timestamp=stamp, source_timestamp=stamp, payload=payload, event_type=kind)


def replay(rows):
    repo = ControlPlaneRepo(now=lambda: 1000)
    _load_full_into(repo, PreparedSource(STRUCTURED, copy.deepcopy(rows)), capture_event_core=True)
    return repo


def capsule(repo):
    return capture_projection(repo, repo._checkpoint_event_core, coverage={'position': 1})


def append(checkpoint, rows, full_streams=None):
    repo = ControlPlaneRepo(now=lambda: 1000)
    restore_projection(repo, checkpoint, full_streams=full_streams or {}, tail_rows=copy.deepcopy(rows),
                       structured=STRUCTURED, coverage={'position': 2})
    return repo


def test_equal_ingest_sequence_ties_follow_canonical_event_order():
    prefix = [event('prefix', 0, 1, {'service_name': 'h', 'status': 'healthy'}, entity_type='service')]
    first = event('a', 2, 2, {'status': 'unavailable'}, entity_type='service')
    last = event('z', 2, 2, {'status': 'healthy'}, entity_type='service')
    expected = replay(prefix + [first, last])
    actual = append(capsule(replay(prefix)), [last, first])
    assert actual._checkpoint_event_core == expected._checkpoint_event_core


def test_equal_policy_revision_capsule_uses_replay_order_across_appends():
    prefix = [event('prefix', 0, 1, {'host_name': 'h'})]
    disabled = event('disabled', 1, 3, {'revision': 2, 'mode': 'disabled'}, 'HostPolicyChanged')
    automatic = event('automatic', 2, 2, {'revision': 2, 'mode': 'automatic'}, 'HostPolicyChanged')
    first = append(capsule(replay(prefix)), [disabled, automatic],
                   {('host', 'h'): prefix + [automatic, disabled]})
    report = event('report', 3, 4, {'scheduler_policy': {'revision': 0, 'mode': 'manual_only'}})
    second = append(capsule(first), [report],
                    {('host', 'h'): prefix + [automatic, disabled, report]})
    expected = replay(prefix + [disabled, automatic, report])
    assert second._hosts['h']['scheduler_policy'] == expected._hosts['h']['scheduler_policy']
    assert second._checkpoint_operator_policies == expected._checkpoint_operator_policies


@pytest.mark.parametrize('kind', ['service', 'session'])
def test_backdated_fallback_keeps_maximum_sequence_for_subsequent_writes(kind):
    prefix = [event('high', 100, 1, {'status': 'healthy'}, entity_type=kind),
              event('recent', 1, 3, {'status': 'healthy'}, entity_type=kind)]
    tail = event('late', 0, 2, {'status': 'unavailable'}, entity_type=kind)
    full = [prefix[0], tail, prefix[1]]
    restored = append(capsule(replay(prefix)), [tail], {(kind, 'h'): full})
    expected = replay(full)
    assert restored.record_event(kind, 'h', 'Observation', {})['sequence'] == (
        expected.record_event(kind, 'h', 'Observation', {})['sequence']) == 101


def test_scheduler_observation_telemetry_is_replayed_once_across_checkpoint_cycles():
    host = {'host_id': 'h', 'host_name': 'h', 'scheduler_policy': {'revision': 0, 'mode': 'manual_only'}}
    sample = {'sequence': 1, 'cpu_pct': 5, 'at': '1970-01-01T00:00:01+00:00'}
    observation = event('observation', 1, 1, {'host': host, 'sample': sample},
                        'SchedulerHostObserved', 'scheduler_observation')
    reference = replay([observation])
    checkpoint = capsule(reference)
    for _ in range(3):
        restored = append(checkpoint, [])
        assert restored.list_telemetry('h') == reference.list_telemetry('h')
        assert len(restored.list_telemetry('h')) == 1
        checkpoint = capsule(restored)


def test_agent_projection_export_does_not_depend_on_mapping_insertion_order():
    repo = ControlPlaneRepo(now=lambda: 1000)
    repo.upsert_session('b', name='Beta')
    repo.upsert_session('a', name='Alpha')
    repo.list_sessions()
    expected = repo.export_state()['agent_projections']
    assert len(expected) == 2
    repo._agent_records = dict(reversed(list(repo._agent_records.items())))
    assert repo.export_state()['agent_projections'] == expected
