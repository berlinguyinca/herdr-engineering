import copy
import json

from herdr_engineering.control_plane.bridge import DurableSource, _load_full_into
from herdr_engineering.control_plane.projection_checkpoint import capture_projection, restore_projection
from herdr_engineering.control_plane.projection_store import decode_checkpoint, encode_checkpoint
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class Sink:
    def __init__(self):
        self.rows = []

    def record(self, event):
        self.rows.append(copy.deepcopy(event))

    record_required = record


class Source(DurableSource):
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def missions(self): return []
    def sessions(self): return []
    def hosts(self): return []
    def services(self): return []
    def events(self): return self.rows


def sample():
    sink = Sink()
    repo = ControlPlaneRepo(now=lambda: 1000, sink=sink)
    mission = repo.create_mission('Checkpoint mission')
    repo.upsert_session('agent-one', name='Ada', mission_id=mission)
    repo.get_session('agent-one')
    repo.rename_agent('agent-one', 'Grace Hopper', 1)
    repo.upsert_service('host-one', 'App', 8000, status='healthy', description='Original description')
    repo.inspector.ingest('agent-one', {'kind': 'message', 'role': 'assistant',
                                      'text': 'Retained private message', 'message_id': 'message-one'})
    repo.record_token_usage('project', 'model', 11, 7, request_id='usage-one', mission_id=mission)
    return repo, sink


def projected(repo):
    return {'missions': repo.list_missions(), 'sessions': repo.list_sessions(),
            'services': repo.list_services(), 'usage': repo.token_analytics(),
            'names': repo.agent_identities.list(), 'inspector': repo.inspector.health()}


def test_compact_checkpoint_roundtrip_preserves_complete_owned_projections_without_writes():
    _, sink = sample()
    reference = ControlPlaneRepo(now=lambda: 1000)
    _load_full_into(reference, Source(sink.rows), capture_event_core=True)
    checkpoint = capture_projection(reference, reference._checkpoint_event_core,
                                    coverage={'position': 10, 'legacy_rows': 0, 'public_rows': len(reference._event_order)})
    packed, checksum = encode_checkpoint(checkpoint)
    restored = ControlPlaneRepo(now=lambda: 1000, sink=Sink())
    restore_projection(restored, decode_checkpoint(packed, checksum), full_streams={},
                       tail_rows=[], structured={name: [] for name in ('missions','sessions','hosts','services')},
                       coverage=checkpoint['coverage'])
    assert projected(restored) == projected(reference)
    assert restored._sink.rows == []
    assert all(len(stream) == 1 for key, stream in checkpoint['repo']['events'].items()
               if key.startswith(('service|','session|','host|')))
    assert restored.get_session('agent-one')['display_name'] == 'Grace Hopper'
    assert 'Retained private message' in json.dumps(restored.inspector.export_checkpoint())
    assert not any(e['entity_type'].startswith('agent_') for e in restored.recent_events())


def test_dirty_core_stream_reconstruction_preserves_fields_and_legacy_history():
    live, sink = sample()
    prefix = copy.deepcopy(sink.rows)
    reference = ControlPlaneRepo(now=lambda: 1000)
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    checkpoint = capture_projection(reference, reference._checkpoint_event_core,
                                    coverage={'position': 10, 'legacy_rows': 0, 'public_rows': len(reference._event_order)})
    service_id = next(iter(live._services))
    live.upsert_service('host-one', 'App', 8000, status='unavailable')
    final = ControlPlaneRepo(now=lambda: 1000)
    _load_full_into(final, Source(sink.rows), capture_event_core=True)
    dirty = {('service',service_id): [copy.deepcopy(row) for row in sink.rows
                                      if row['entity_type']=='service' and row['entity_id']==service_id]}
    restored = ControlPlaneRepo(now=lambda: 1000)
    restore_projection(restored, checkpoint, full_streams=dirty, tail_rows=sink.rows[len(prefix):],
                       structured={name: [] for name in ('missions','sessions','hosts','services')},
                       coverage={'position': 11, 'legacy_rows': 0, 'public_rows': len(final._event_order)})
    assert projected(restored) == projected(final)
    assert restored.get_service(service_id)['description'] == 'Original description'


def assert_domain_equivalence(actual, expected):
    """Compare durable views; inspector epochs identify processes, not state."""
    assert projected(actual) == projected(expected)
    for session in ('agent-one', 'quiet-agent'):
        for role in ('viewer', 'operator'):
            left = actual.inspector.query(session, role)
            right = expected.inspector.query(session, role)
            assert {k: v for k, v in left.items() if k != 'epoch'} == {
                k: v for k, v in right.items() if k != 'epoch'}
    assert actual.host_metrics.export_checkpoint() == expected.host_metrics.export_checkpoint()
    assert actual.ledger.export_checkpoint() == expected.ledger.export_checkpoint()
    assert actual.stage_analytics() == expected.stage_analytics()
    assert actual.list_test_runs() == expected.list_test_runs()
    assert actual.graph.snapshot() == expected.graph.snapshot()
    assert actual._collab_replay == expected._collab_replay
    assert actual.collab_quarantine == expected.collab_quarantine
    assert actual.collab_quarantine_reasons == expected.collab_quarantine_reasons


def domain_rows():
    """Use real writers, as in the bridge and owner-codec replay fixtures."""
    from herdr_engineering.control_plane.ingest import IngestService
    live, sink = sample()
    ingest = IngestService(live)
    live.upsert_mission('M1', source='pi', title='Stages', stage='planning')
    live.set_mission_stage('pi:M1', 'implementation')
    live.inspector.ingest('quiet-agent', {'kind': 'message', 'role': 'user', 'text': 'quiet'})
    live.inspector.ingest('agent-one', {'events': [
        {'kind': 'tool', 'phase': 'start', 'call_id': 'c', 'tool': 'bash'},
        {'kind': 'test', 'phase': 'start', 'run_id': 'r'},
        {'kind': 'usage', 'input_tokens': 12, 'output_tokens': 3},
        {'kind': 'reasoning', 'text': 'private reasoning'},
    ]})
    live.host_metrics.observe_state('host-one', {'maintenance': True}, {'cpu': 12})
    ingest.ingest({'source': 'pi', 'entity_type': 'test_run', 'entity_id': 'run1',
                   'event_type': 'test_run.started', 'payload': {'mission_id': 'M1', 'status': 'running'}})
    principal = {'kind': 'agent', 'id': 'key:writer', 'authenticated_by': 'api_key'}
    envelope = {'schema_version': 2, 'event_id': 'graph-1', 'source': 'pi',
                'source_sequence': 1, 'entity_type': 'lineage', 'entity_id': 'task:t1',
                'event_type': 'node.upsert', 'visibility_scope': 'org',
                'payload': {'node_type': 'task', 'node_id': 'task:t1', 'label': 'Original'}}
    ingest.ingest(envelope, principal)
    prefix = copy.deepcopy(sink.rows)
    live.rename_agent('agent-one', 'Katherine Johnson', 2)
    live.set_mission_stage('pi:M1', 'review')
    live.inspector.ingest('agent-one', {'events': [
        {'kind': 'tool', 'phase': 'end', 'call_id': 'c', 'exit_code': 0, 'output': 'private output'},
        {'kind': 'test', 'phase': 'end', 'run_id': 'r', 'passed': 3},
        {'kind': 'usage', 'input_tokens': 5, 'output_tokens': 2},
    ]})
    live.host_metrics.observe_state('host-one', {'maintenance': False}, {'cpu': 30})
    ingest.ingest({'source': 'pi', 'entity_type': 'test_run', 'entity_id': 'run1',
                   'event_type': 'test_run.completed', 'payload': {'mission_id': 'M1', 'passed': 2}})
    ingest.ingest({**envelope, 'event_id': 'graph-4', 'source_sequence': 4,
                   'payload': {**envelope['payload'], 'label': 'Updated'}}, principal)
    return prefix, sink.rows


def test_dirty_private_and_noncore_domains_match_full_replay_without_writes():
    prefix, rows = domain_rows()
    reference = ControlPlaneRepo(now=lambda: 1000)
    _load_full_into(reference, Source(prefix), capture_event_core=True)
    coverage = {'position': len(prefix), 'legacy_rows': 0, 'public_rows': len(reference._event_order)}
    packed, checksum = encode_checkpoint(capture_projection(reference, reference._checkpoint_event_core,
                                                            coverage=coverage))
    tail = rows[len(prefix):]
    dirty = {(r['entity_type'], r['entity_id']) for r in tail}
    streams = {key: [r for r in rows if (r['entity_type'], r['entity_id']) == key] for key in dirty}
    expected = ControlPlaneRepo(now=lambda: 1000)
    _load_full_into(expected, Source(rows), capture_event_core=True)
    restored = ControlPlaneRepo(now=lambda: 1000, sink=Sink())
    restore_projection(restored, decode_checkpoint(packed, checksum), full_streams=streams, tail_rows=tail,
                       structured={name: [] for name in ('missions', 'sessions', 'hosts', 'services')},
                       coverage={**coverage, 'position': len(rows)})
    assert_domain_equivalence(restored, expected)
    assert restored.get_session('agent-one')['display_name'] == 'Katherine Johnson'
    assert restored.graph.snapshot()['nodes']
    assert restored.list_test_runs()
    assert restored.inspector.query('quiet-agent', 'operator')['events']
    assert restored._sink.rows == []
