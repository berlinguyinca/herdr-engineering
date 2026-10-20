"""Metadata observations cannot operate or refresh a running service."""
import copy

import pytest

from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_bridge import FakeSource
from tests.control_plane.test_service_ownership import SID, native, proof


def setup_service():
    clock = [1800000000]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    native(repo)
    sid = repo.register_service('backend', 'host', 43211, source_port=8123, route='http://dev:43211',
                               session_id=SID, ownership=proof(),
                               repository_id='github.com/org/backend', commit_sha='b' * 40)
    return repo, clock, sid


def observe(repo, sid, **changes):
    payload = dict(host_id='host', source_port=8123, observed_at=repo._stamp(), state='observed', scan_complete=True,
                   listener_pid=300, listener_start='1000', ownership=proof(),
                   repository_id='github.com/org/backend', commit_sha='b' * 40)
    payload.update(changes)
    return IngestService(repo).ingest(dict(entity_type='service', entity_id=sid,
        event_type='service.metadata', source='fabric-agent', payload=payload))


def replay(repo):
    clone = ControlPlaneRepo(now=repo._now)
    load_into(clone, FakeSource(events=[e for rows in repo.export_state()['events'].values() for e in rows]))
    return clone


def test_legacy_healthy_heartbeat_omissions_preserve_established_metadata():
    repo, clock, sid = setup_service()
    clock[0] += 10
    IngestService(repo).ingest(dict(entity_type='service', entity_id='legacy', event_type='service.heartbeat',
        payload=dict(host_id='host', service_name='backend', port=43211)))
    for current in (repo, replay(repo)):
        row = current.get_service(sid)
        assert row['catalog']['scope'] == 'herdr'
        assert row['repository_id'] == 'github.com/org/backend'
        assert row['commit_sha'] == 'b' * 40
        assert row['session_id'] == SID


def test_metadata_observation_changes_no_health_endpoint_or_lifecycle_timestamps():
    repo, clock, sid = setup_service()
    before = repo.get_service(sid)
    clock[0] += 10
    observe(repo, sid)
    for current in (repo, replay(repo)):
        row = current.get_service(sid)
        for field in ('status', 'port', 'source_port', 'route', 'url', 'created_at', 'updated_at', 'unavailable_since'):
            assert row.get(field) == before.get(field)
        assert row['metadata_observation'] == {'state': 'observed', 'observed_at': repo._stamp(), 'stale_after_seconds': 90}
        assert row['catalog']['metadata_observation'] == row['metadata_observation']
        assert len(current.list_services()) == 1


def test_missing_listener_preserves_origin_without_declaring_death():
    repo, clock, sid = setup_service()
    clock[0] += 10
    observe(repo, sid, state='missing', listener_pid=None, listener_start=None, ownership=None,
            repository_id=None, commit_sha=None)
    row = replay(repo).get_service(sid)
    assert row['catalog']['scope'] == 'herdr'
    assert row['repository_id'] == 'github.com/org/backend'
    assert row['status'] == 'healthy'
    assert row['metadata_observation']['state'] == 'missing'


def test_new_observed_unproven_process_clears_old_origin_and_git_pair():
    repo, clock, sid = setup_service()
    clock[0] += 10
    observe(repo, sid, listener_pid=400, listener_start='2000', ownership=None, repository_id=None, commit_sha=None)
    for current in (repo, replay(repo)):
        row = current.get_service(sid)
        assert row['catalog']['scope'] == 'unassigned'
        assert row['repository_id'] is None and row['commit_sha'] is None
        assert row['verified_origin'] is None and row['session_id'] is None
        assert row['status'] == 'healthy'


@pytest.mark.parametrize('changes', [{'host_id': 'other'}, {'source_port': 9999},
    {'listener_pid': True}, {'listener_start': ''}, {'ownership': dict(proof(), listener_pid=999)}])
def test_metadata_requires_exact_existing_binding_and_consistent_listener(changes):
    repo, _, sid = setup_service()
    before = copy.deepcopy(repo.export_state())
    with pytest.raises(ValueError):
        observe(repo, sid, **changes)
    assert repo.export_state() == before


def test_metadata_cannot_create_unknown_service():
    repo, _, _ = setup_service()
    with pytest.raises(KeyError):
        observe(repo, 'unknown-service')
    assert len(repo.list_services()) == 1


def test_metadata_only_event_replay_preserves_existing_structured_health():
    repo, clock, sid = setup_service()
    before = repo.get_service(sid)
    clock[0] += 20
    observe(repo, sid, state='missing', listener_pid=None, listener_start=None)
    metadata = repo.stream('service', sid)[-1]
    structured = dict(before, status='unavailable')
    clone = ControlPlaneRepo(now=repo._now)
    load_into(clone, FakeSource(services=[structured], events=[metadata]))
    row = clone.get_service(sid)
    assert row['status'] == 'unavailable'
    assert row['updated_at'] == before['updated_at']
    assert row['metadata_observation']['state'] == 'missing'


def test_legacy_heartbeat_cannot_refresh_metadata_observation_clock():
    repo, clock, sid = setup_service()
    observe(repo, sid)
    observed_at = repo.get_service(sid)['metadata_observation']['observed_at']
    clock[0] += 120
    repo.upsert_service('host', 'backend', 43211)
    row = replay(repo).get_service(sid)
    assert row['updated_at'] == repo._stamp()
    assert row['catalog']['metadata_observation']['observed_at'] == observed_at


def test_explicit_null_regular_heartbeat_clears_origin_while_omission_does_not():
    repo, _, sid = setup_service()
    IngestService(repo).ingest(dict(entity_type='service', entity_id='legacy', event_type='service.heartbeat',
        payload=dict(host_id='host', service_name='backend', port=43211, ownership=None)))
    row = replay(repo).get_service(sid)
    assert row['catalog']['scope'] == 'unassigned'
    assert row['repository_id'] is None and row['commit_sha'] is None


def test_metadata_and_health_reporters_cannot_overwrite_each_others_state():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo, clock, sid = setup_service()
    entered, release = threading.Event(), threading.Event()
    class Sink:
        def record(self, event):
            if event['event_type'] == 'ServiceMetadataObserved':
                entered.set()
                assert release.wait(2)
    repo._sink = Sink()
    clock[0] += 10
    with ThreadPoolExecutor(max_workers=2) as pool:
        metadata = pool.submit(observe, repo, sid, state='missing')
        try:
            assert entered.wait(2)
            clock[0] += 10
            health = pool.submit(repo.upsert_service, 'host', 'backend', 43211, status='unavailable')
        finally:
            release.set()
        metadata.result(timeout=2)
        health.result(timeout=2)
    for current in (repo, replay(repo)):
        row = current.get_service(sid)
        assert row['status'] == 'unavailable'
        assert row['metadata_observation']['state'] == 'missing'
        assert row['catalog']['scope'] == 'herdr'


def test_ambiguous_or_inaccessible_pid_is_unconfirmed_not_absent():
    repo, _, sid = setup_service()
    observe(repo, sid, state='unconfirmed', scan_complete=False, listener_pid=None, listener_start=None, ownership=None)
    row = replay(repo).get_service(sid)
    assert row['metadata_observation']['state'] == 'unconfirmed'
    assert row['catalog']['scope'] == 'herdr' and row['status'] == 'healthy'


def test_missing_metadata_observation_requires_complete_scan_evidence():
    repo, _, sid = setup_service()
    with pytest.raises(ValueError, match='complete'):
        observe(repo, sid, state='missing', scan_complete=False)


def test_future_observation_cannot_revive_old_proof_after_legacy_heartbeats():
    from datetime import UTC, datetime

    repo, clock, sid = setup_service()
    observe(repo, sid)
    original_observation = copy.deepcopy(repo.get_service(sid)['metadata_observation'])
    clock[0] += 100
    before = copy.deepcopy(repo.export_state())
    future = datetime.fromtimestamp(clock[0] + 240, UTC).isoformat()
    with pytest.raises(ValueError, match='timestamp'):
        observe(repo, sid, observed_at=future)
    assert repo.export_state() == before
    clock[0] += 235
    repo.upsert_service('host', 'backend', 43211)
    for current in (repo, replay(repo)):
        row = current.get_service(sid)
        assert row['metadata_observation'] == original_observation
        age = clock[0] - datetime.fromisoformat(row['metadata_observation']['observed_at']).timestamp()
        assert age > row['metadata_observation']['stale_after_seconds']
