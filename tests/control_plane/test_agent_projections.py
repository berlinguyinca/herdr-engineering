import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.mark.parametrize('kind', ['agent_identity', 'agent_summary', 'agent_identity_reconciliation'])
def test_generic_ingestion_cannot_forge_private_agent_projections(kind):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match='not accepted'):
        IngestService(repo).ingest({'entity_type': kind, 'entity_id': 'one',
                                   'event_type': f'{kind}.updated', 'payload': {}})
    assert repo.recent_events() == []


def test_agent_names_are_unique_stable_and_separate_from_provider_names():
    repo = ControlPlaneRepo()
    repo.upsert_session('one', name='Ada', host_id='bender')
    repo.upsert_session('two', name='Ada', host_id='beast')
    first, second = repo.get_session('one'), repo.get_session('two')
    assert first['display_name'] == 'Ada'
    assert first['agent_handle'] != second['agent_handle']
    repo.upsert_session('one', name='New provider title')
    assert repo.get_session('one')['display_name'] == 'Ada'
    assert repo.get_session('one')['name'] == 'New provider title'


def test_names_and_summaries_restore_outside_activity_streams(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    sid = repo.create_session(agent_role='pi')
    before = repo.get_session(sid)
    renamed = repo.rename_agent(sid, 'Grace Hopper', before['name_version'])
    repo.persist_agent_summary({'kind': 'agent_summary', 'session_id': sid,
                                'summary': {'overview': 'Working on tests', 'state': 'ready'}})
    assert not any(e['entity_type'].startswith('agent_') for e in repo.recent_events())
    backup = repo.export_state()
    restored = ControlPlaneRepo()
    restored.import_state(backup)
    assert restored.get_session(sid)['display_name'] == renamed['display_name']
    assert restored.agent_summary_events()[0]['summary']['overview'] == 'Working on tests'
    assert not any(e['entity_type'].startswith('agent_') for e in restored.recent_events())


def test_identity_persistence_failure_does_not_claim_a_name():
    class BrokenSink:
        def record(self, event):
            pass

        def record_required(self, event):
            raise RuntimeError('storage unavailable')

    repo = ControlPlaneRepo(sink=BrokenSink())
    sid = repo.create_session()
    with pytest.raises(RuntimeError, match='storage unavailable'):
        repo.get_session(sid)
    assert repo.agent_identities.get(sid) is None


def test_durable_projection_streams_replay_privately_before_names_allocate():
    repo = ControlPlaneRepo()
    repo.upsert_session('one', name='Ada')
    profile = repo.get_session('one')
    exported = repo.export_state()
    records = exported.pop('agent_projections')
    for event in records:
        exported['events'][f"{event['entity_type']}|{event['entity_id']}"] = [event]
    restored = ControlPlaneRepo()
    restored.import_state(exported)
    assert restored.get_session('one')['display_name'] == profile['display_name']
    assert restored.get_session('one')['name_version'] == profile['name_version']
    assert not restored.stream('agent_identity', 'one')


def test_restore_replaces_names_and_summaries_on_existing_repository():
    repo = ControlPlaneRepo()
    repo.upsert_session('one', name='Ada')
    profile = repo.get_session('one')
    backup = repo.export_state()
    repo.rename_agent('one', 'Grace', profile['name_version'])
    repo.persist_agent_summary({'kind': 'agent_summary', 'session_id': 'one', 'summary': {'overview': 'Later'}})
    repo.import_state(backup)
    assert repo.get_session('one')['display_name'] == 'Ada'
    assert repo.agent_summary_events() == []


def test_list_names_uses_snapshot_while_persistence_allows_new_agents():
    class Sink:
        added = False

        def record(self, event):
            pass

        def record_required(self, event):
            if not self.added:
                self.added = True
                repo.upsert_session('late')

    repo = ControlPlaneRepo(sink=Sink())
    repo.upsert_session('one')
    repo.upsert_session('two')
    assert {s['session_id'] for s in repo.list_sessions()} == {'one', 'two'}
    assert {s['session_id'] for s in repo.list_sessions()} == {'one', 'two', 'late'}


def test_export_preserves_complete_private_identity_history():
    repo = ControlPlaneRepo()
    repo.upsert_session('one', name='Ada')
    profile = repo.get_session('one')
    repo.rename_agent('one', 'Grace', profile['name_version'])
    backup = repo.export_state()
    assert len(backup['agent_identity_history']) == 2
    restored = ControlPlaneRepo()
    restored.import_state(backup)
    assert restored.export_state()['agent_identity_history'] == backup['agent_identity_history']
    assert not any(event['entity_type'].startswith('agent_identity') for event in restored.recent_events())


def test_divergent_history_is_preserved_and_exposed_as_an_unresolved_name():
    from herdr_engineering.control_plane.agent_identity import (
        AgentIdentityConflict,
        AgentIdentityRegistry,
    )

    events = []
    for index, name in enumerate(('Ada', 'Grace')):
        registry = AgentIdentityRegistry(persist=lambda event: events.append(
            {**event, 'event_id': str(index), 'sequence': index}))
        registry.allocate('one', name)
    repo = ControlPlaneRepo()
    repo.import_state({'agent_projections': events,
                       'sessions': {'one': {'session_id': 'one', 'status': 'active'}}})
    restored = repo.get_session('one')
    assert restored['display_name'] == 'one' and restored['agent_handle'] is None
    assert restored['name_error'] and len(repo.export_state()['agent_identity_history']) == 2
    with pytest.raises(AgentIdentityConflict):
        repo.agent_identities.allocate('one')


def test_global_persistence_acknowledgement_keeps_actual_durable_event():
    class Sink:
        def record(self, event):
            pass

        def record_required(self, event):
            return {**event, 'event_id': 'actual-stored-id', 'payload': {
                'identity': 'one', 'display_name': 'Ada Amber', 'handle': 'ada-amber', 'version': 2,
                'reserved_names': ['ada amber'], 'reserved_handles': ['ada-amber'],
            }}

    repo = ControlPlaneRepo(sink=Sink())
    repo.upsert_session('one', name='Ada')
    assert repo.get_session('one')['display_name'] == 'Ada Amber'
    assert repo.export_state()['agent_identity_history'][0]['event_id'] == 'actual-stored-id'
