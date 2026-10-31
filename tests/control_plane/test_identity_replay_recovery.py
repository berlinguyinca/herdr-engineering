import copy
import threading

import pytest

from herdr_engineering.control_plane.agent_identity import AgentIdentityRegistry
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def legacy_conflicted_state():
    repo = ControlPlaneRepo()
    events = []
    for identity, name in [('one', 'Ada'), ('two', 'Ada'), ('three', 'Grace')]:
        repo.upsert_session(identity, name=name)
        AgentIdentityRegistry(persist=events.append).allocate(identity, name)
    state = repo.export_state()
    state['agent_projections'] = events
    return state


def test_legacy_name_conflicts_do_not_discard_sessions_or_reassign_aliases():
    state = legacy_conflicted_state()
    original = copy.deepcopy(state['agent_projections'])
    repo = ControlPlaneRepo()
    repo.import_state(state)
    sessions = {row['session_id']: row for row in repo.list_sessions()}
    assert set(sessions) == {'one', 'two', 'three'}
    assert sessions['one']['display_name'] == 'one'
    assert sessions['two']['display_name'] == 'two'
    assert sessions['one']['agent_handle'] is None
    assert sessions['one']['name_version'] == 1
    assert 'conflict' in sessions['one']['name_error']
    assert sessions['three']['display_name'] == 'Grace'
    assert repo.agent_identities.conflict_count == 2
    assert repo.export_state()['agent_projections'] == original
    assert not any(e['entity_type'] == 'agent_identity' for e in repo.recent_events())
    new = repo.agent_identities.allocate('four', 'Ada')
    assert new['display_name'] != 'Ada'


def test_explicit_rename_recovers_labels_without_releasing_ambiguous_history():
    repo = ControlPlaneRepo()
    repo.import_state(legacy_conflicted_state())
    assert repo.rename_agent('one', 'Ada Byron', 1)['version'] == 2
    assert repo.rename_agent('two', 'Ada Lovelace', 1)['version'] == 2
    assert repo.agent_identities.conflict_count == 0
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert restored.get_session('one')['display_name'] == 'Ada Byron'
    assert restored.get_session('two')['display_name'] == 'Ada Lovelace'
    assert restored.agent_identities.allocate('four', 'Ada')['display_name'] != 'Ada'


def test_malformed_private_identity_still_blocks_restore():
    state = legacy_conflicted_state()
    state['agent_projections'][0]['payload']['handle'] = 'forged'
    with pytest.raises(ValueError, match='invalid persisted'):
        ControlPlaneRepo().import_state(state)


def test_health_reports_name_degradation_while_session_api_remains_available():
    import json
    import urllib.request

    repo = ControlPlaneRepo()
    repo.import_state(legacy_conflicted_state())
    server = ControlPlaneServer(repo)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.port}/api/v1/health') as response:
            health = json.load(response)
        assert health['status'] == 'ok'
        assert health['agent_identity_conflicts'] == 2
        with urllib.request.urlopen(f'http://127.0.0.1:{server.port}/api/v1/sessions') as response:
            assert len(json.load(response)['sessions']) == 3
    finally:
        server.shutdown()
        thread.join(3)
