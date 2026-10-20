"""Application scope requires actual native launch/session ownership evidence."""
from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_bridge import FakeSource

SID = 'herdr:host:default:w1%3Ap1'


def native(repo, host='host'):
    repo.upsert_session(SID, host_id=host, pane_id='w1:p1', terminal_id='terminal', herdr_session='default')


def proof(**changes):
    return dict(kind='herdr_process_ancestry', host_id='host', session_id=SID,
                herdr_session='default', pane_id='w1:p1', terminal_id='terminal',
                listener_pid=300, shell_pid=100, ancestor_pids=[300, 200, 100],
                listener_start='1000', shell_start='500', observed_at='2026-10-20T12:00:00Z', **changes)


def test_unrelated_listener_in_repo_cwd_is_unassigned():
    repo = ControlPlaneRepo()
    sid = repo.register_service('HerdR app', 'host', 43211, owner='node', project='herdr-engineering')
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'


def test_exact_native_session_association_without_launch_proof_is_unassigned():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('app', 'host', 43211, session_id=SID)
    view = repo.get_service(sid)['catalog']
    assert view['scope'] == 'unassigned'
    assert view['ownership'] is None
    assert 'no verified launch' in view['scope_reason']


def test_unknown_or_wrong_host_session_is_unassigned():
    repo = ControlPlaneRepo()
    native(repo, host='different-host')
    sid = repo.register_service('app', 'host', 43211, session_id=SID)
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'
    sid = repo.register_service('other', 'host', 43212, session_id='unknown')
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'


def test_native_process_proof_requires_matching_terminal_and_host():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('app', 'host', 43211, session_id=SID, ownership=proof())
    assert repo.get_service(sid)['catalog']['scope'] == 'herdr'
    wrong = proof()
    wrong['terminal_id'] = 'different-terminal'
    repo.upsert_service('host', 'app', 43211, session_id=SID, ownership=wrong)
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'


def test_system_scope_requires_explicit_system_unit_evidence():
    repo = ControlPlaneRepo()
    a = repo.register_service('postgres', 'host', 55555, owner='postgres')
    b = repo.register_service('worker', 'host', 55556, ownership={
        'kind': 'system_unit', 'host_id': 'host', 'unit': 'example.service',
        'listener_pid': 301, 'listener_start': '1000', 'observed_at': '2026-10-20T12:00:00Z'})
    assert repo.get_service(a)['catalog']['scope'] == 'unassigned'
    assert repo.get_service(b)['catalog']['scope'] == 'system'


def test_ownership_ingest_and_clear_survive_replay_without_deleting_history():
    repo = ControlPlaneRepo()
    native(repo)
    ingest = IngestService(repo)
    envelope = {'entity_type': 'service', 'entity_id': 'listener', 'event_type': 'service.registered',
                'source': 'fabric-agent', 'payload': {'service_name': 'app', 'host_id': 'host',
                'port': 43211, 'session_id': SID, 'ownership': proof()}}
    service_id = ingest.ingest(envelope)['entity_id']
    replay = ControlPlaneRepo()
    load_into(replay, FakeSource(events=[event for rows in repo.export_state()['events'].values() for event in rows]))
    assert replay.get_service(service_id)['catalog']['scope'] == 'herdr'
    envelope['payload'].update(session_id=None, ownership=None)
    ingest.ingest(envelope)
    load_into(replay, FakeSource(events=[event for rows in repo.export_state()['events'].values() for event in rows]))
    assert replay.get_service(service_id)['catalog']['scope'] == 'unassigned'
    assert len(replay.list_services()) == 1
    assert len(replay.stream('service', service_id)) == 2


def test_service_repository_is_its_own_checkout_and_shared_project_maps(tmp_path, monkeypatch):
    import json
    path = tmp_path / 'projects.json'
    path.write_text(json.dumps({'github.com/org/backend': 'product', 'github.com/org/frontend': 'product'}))
    monkeypatch.setenv('HERDR_ENGINEERING_PROJECT_REPOS_FILE', str(path))
    repo = ControlPlaneRepo()
    native(repo)
    repo.upsert_session(SID, repository_id='github.com/org/frontend', commit_sha='a' * 40)
    sid = repo.register_service('backend', 'host', 43211, session_id=SID, ownership=proof(),
                               repository_id='https://github.com/Org/Backend.git', commit_sha='B' * 40)
    row = repo.get_service(sid)
    assert row['catalog']['scope'] == 'herdr'
    assert row['repository_id'] == row['catalog']['repository_id'] == 'github.com/org/backend'
    assert row['commit_sha'] == 'b' * 40
    assert row['project_id'] == row['catalog']['project_id'] == 'product'


def test_service_git_advertisement_replays_and_listener_reuse_clears_old_pair():
    repo = ControlPlaneRepo()
    native(repo)
    ingest = IngestService(repo)
    body = {'entity_type': 'service', 'entity_id': 'service', 'event_type': 'service.updated',
            'payload': {'service_name': 'backend', 'host_id': 'host', 'port': 43211,
                        'session_id': SID, 'ownership': proof(),
                        'repo_url': 'git@github.com:Org/Backend.git', 'commit_sha': 'b' * 40}}
    sid = ingest.ingest(body)['entity_id']
    clone = ControlPlaneRepo()
    def reload():
        load_into(clone, FakeSource(events=[event for rows in repo.export_state()['events'].values() for event in rows]))
    reload()
    assert clone.get_service(sid)['repository_id'] == 'github.com/org/backend'
    assert clone.get_service(sid)['commit_sha'] == 'b' * 40
    body['payload'].update(ownership=None, session_id=None, repo_url=None, commit_sha=None)
    ingest.ingest(body)
    reload()
    row = clone.get_service(sid)
    assert row['repository_id'] is None and row['commit_sha'] is None and row['project_id'] is None
    assert row['catalog']['scope'] == 'unassigned'


def test_service_rejects_invalid_git_advertisement_before_mutation():
    import pytest
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError):
        IngestService(repo).ingest({'entity_type': 'service', 'entity_id': 'app', 'event_type': 'service.updated',
                                  'payload': {'host_id': 'host', 'repository_id': 'github.com/org/app', 'commit_sha': 'short'}})
    assert repo.list_services() == []


def test_unavailable_owned_service_remains_herdr_history_with_git_identity():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('backend', 'host', 43211, session_id=SID, ownership=proof(),
                               repository_id='github.com/org/backend', commit_sha='b' * 40)
    repo.mark_service_unavailable(sid)
    clone = ControlPlaneRepo()
    load_into(clone, FakeSource(events=[event for rows in repo.export_state()['events'].values() for event in rows]))
    row = clone.get_service(sid)
    assert row['catalog']['scope'] == 'herdr'
    assert row['lifecycle']['state'] == 'unavailable'
    assert row['repository_id'] == 'github.com/org/backend' and row['commit_sha'] == 'b' * 40


def test_verified_launch_history_survives_owner_terminal_replacement_and_replay():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('backend', 'host', 43211, session_id=SID, ownership=proof(),
                               repository_id='github.com/org/backend', commit_sha='b' * 40)
    repo.mark_service_unavailable(sid)
    repo.upsert_session(SID, terminal_id='replacement-terminal')
    assert repo.get_service(sid)['catalog']['scope'] == 'herdr'
    clone = ControlPlaneRepo()
    load_into(clone, FakeSource(events=[event for rows in repo.export_state()['events'].values() for event in rows]))
    row = clone.get_service(sid)
    assert row['catalog']['scope'] == 'herdr'
    assert row['lifecycle']['state'] == 'unavailable'
    assert row['repository_id'] == 'github.com/org/backend'
    repo.upsert_service('host', 'backend', 43211, status='healthy', ownership=None)
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'
    assert repo.get_service(sid)['repository_id'] is None


def test_client_cannot_supply_verified_origin_receipt():
    repo = ControlPlaneRepo()
    native(repo)
    ingest = IngestService(repo)
    sid = ingest.ingest({'entity_type': 'service', 'entity_id': 'new', 'event_type': 'service.updated',
                        'payload': {'service_name': 'unowned', 'host_id': 'host', 'port': 50000,
                                    'verified_origin': {'scope': 'herdr', 'ownership': proof()},
                                    'verified': True, 'session_id': SID}})['entity_id']
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'
    assert repo.get_service(sid).get('verified_origin') is None


def test_minimal_unavailable_report_preserves_verified_origin_and_git_history():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('backend', 'host', 43211, session_id=SID, ownership=proof(),
                               repository_id='github.com/org/backend', commit_sha='b' * 40)
    repo.upsert_session(SID, terminal_id='replacement-terminal')
    repo.upsert_service('host', 'backend', 43211, status='unavailable')
    row = repo.get_service(sid)
    assert row['catalog']['scope'] == 'herdr'
    assert row['repository_id'] == 'github.com/org/backend'
    assert row['commit_sha'] == 'b' * 40


def test_same_observed_process_retains_origin_after_owner_identity_changes():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('backend', 'host', 43211, session_id=SID, ownership=proof())
    repo.upsert_session(SID, terminal_id='replacement-terminal')
    continued = proof()
    continued['observed_at'] = '2026-10-20T12:01:00Z'
    repo.upsert_service('host', 'backend', 43211, session_id=SID, ownership=continued)
    assert repo.get_service(sid)['catalog']['scope'] == 'herdr'
    reused = dict(continued, listener_start='different-birth')
    repo.upsert_service('host', 'backend', 43211, session_id=SID, ownership=reused)
    assert repo.get_service(sid)['catalog']['scope'] == 'unassigned'
