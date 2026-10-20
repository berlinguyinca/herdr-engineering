"""New native-owned listener observations never open routes or alter fabric leases."""
import copy

import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.service_catalog import service_listener_id
from tests.control_plane.test_service_metadata import replay
from tests.control_plane.test_service_ownership import SID, native, proof


def advertise(repo, **changes):
    payload = dict(host_id='host', source_port=49123, listener_pid=300, listener_start='1000',
                   state='observed', observed_at=repo._stamp(), ownership=proof(), owner='node',
                   repository_id='github.com/org/app', commit_sha='b' * 40)
    payload.update(changes)
    sid = service_listener_id(payload['host_id'], payload['listener_pid'], payload['listener_start'], payload['source_port'])
    result = IngestService(repo).ingest(dict(entity_type='service', entity_id=sid,
        event_type='service.listener_observed', source='service-metadata-feed', payload=payload))
    return result['entity_id']


def test_verified_listener_on_dynamic_high_port_is_local_only_and_replays():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    for current in (repo, replay(repo)):
        row = current.get_service(sid)
        assert row['catalog']['scope'] == 'herdr'
        assert row['discovery_source'] == 'native_listener'
        assert row['endpoint_scope'] == row['catalog']['endpoint_scope'] == 'local_only'
        assert row['url'] is None and row['route'] is None
        assert row['source_port'] == row['port'] == 49123
        assert row['repository_id'] == 'github.com/org/app'
        assert row['metadata_observation']['state'] == 'observed'
        assert current.list_leases() == []


def test_unknown_or_unproven_listener_never_creates_record():
    repo = ControlPlaneRepo()
    native(repo)
    with pytest.raises(ValueError):
        advertise(repo, ownership=None)
    with pytest.raises(ValueError):
        advertise(repo, ownership=dict(proof(), terminal_id='wrong'))
    assert repo.list_services() == []


def test_same_instance_updates_but_new_birth_has_distinct_record():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    assert advertise(repo) == sid
    new_id = advertise(repo, listener_start='2000', ownership=dict(proof(), listener_start='2000'))
    assert new_id != sid and len(repo.list_services()) == 2


def test_complete_missing_observation_retains_history_without_fake_stopped():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    advertise(repo, state='missing', scan_complete=True, ownership=None, repository_id=None, commit_sha=None)
    row = replay(repo).get_service(sid)
    assert row['status'] == 'unknown' and row['catalog']['scope'] == 'herdr'
    assert row['metadata_observation']['state'] == 'missing'
    assert row['repository_id'] == 'github.com/org/app'
    assert row['url'] is None


def test_partial_scan_cannot_declare_listener_missing():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    before = copy.deepcopy(repo.get_service(sid))
    with pytest.raises(ValueError):
        advertise(repo, state='missing', scan_complete=False)
    assert repo.get_service(sid) == before


def test_same_verified_process_can_continue_after_owner_pane_ends():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    repo.upsert_session(SID, terminal_id='replacement')
    advertise(repo, ownership=None)
    assert repo.get_service(sid)['catalog']['scope'] == 'herdr'


def test_listener_advertisement_cannot_target_a_legacy_service_id():
    repo = ControlPlaneRepo()
    native(repo)
    sid = repo.register_service('legacy', 'host', 18000, source_port=49123)
    with pytest.raises(ValueError):
        IngestService(repo).ingest(dict(entity_type='service', entity_id=sid,
            event_type='service.listener_observed', payload=dict(host_id='host', source_port=49123,
            listener_pid=300, listener_start='1000', state='observed', observed_at=repo._stamp(), ownership=proof())))
    assert len(repo.list_services()) == 1


def test_uncertain_existing_listener_is_unconfirmed_not_missing_or_stopped():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    advertise(repo, state='unconfirmed', scan_complete=False, ownership=None)
    row = replay(repo).get_service(sid)
    assert row['metadata_observation']['state'] == 'unconfirmed'
    assert row['status'] == 'unknown' and row['catalog']['scope'] == 'herdr'


def test_legacy_fabric_report_cannot_replace_observer_owned_endpoint():
    repo = ControlPlaneRepo()
    native(repo)
    sid = advertise(repo)
    before = copy.deepcopy(repo.get_service(sid))
    legacy = repo.upsert_service('host', before['service_name'], 18000,
                                source_port=49123, route='http://dev:18000')
    assert legacy != sid
    assert repo.get_service(sid) == before
    assert repo.get_service(legacy)['url'] == 'http://dev:18000'


def test_unknown_uncertain_listener_cannot_create_placeholder():
    repo = ControlPlaneRepo()
    native(repo)
    with pytest.raises(KeyError):
        advertise(repo, state='unconfirmed', ownership=None)
    assert repo.list_services() == []


def test_future_listener_advertisement_cannot_create_a_record():
    from datetime import UTC, datetime

    repo = ControlPlaneRepo()
    native(repo)
    before = copy.deepcopy(repo.export_state())
    future = datetime.fromtimestamp(repo._now() + 240, UTC).isoformat()
    with pytest.raises(ValueError, match='timestamp'):
        advertise(repo, observed_at=future)
    assert repo.export_state() == before
