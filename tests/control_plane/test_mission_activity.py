"""Live mission telemetry is a bounded read model, never a lifecycle event."""
import copy
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000
KEY = 'pi:MSN-one'


def stamp(epoch=NOW):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def envelope(**overrides):
    payload = {
        'contract_version': 1, 'host_id': 'bender', 'instance_id': 'i1',
        'session_id': 's1', 'observed_at': stamp(), 'snapshot_at': stamp(),
        'mission_revision': 3, 'connection': 'connected',
        'mission': {'id': 'MSN-one', 'title': 'Fix login', 'status': 'active'},
        'activity': {'health': 'healthy', 'currentObjective': 'Run tests',
                     'lastHeartbeatAt': stamp(NOW - 100),
                     'workerDetails': [{'id': 'worker', 'toolOutputPreview': 'passed'}]},
    }
    payload.update(overrides)
    return {'entity_type': 'mission_activity', 'entity_id': KEY,
            'event_type': 'mission_activity.snapshot', 'source': 'pi', 'payload': payload}


def test_activity_ingestion_never_changes_missions_or_emits_events():
    written, notified = [], []
    class Sink:
        def record(self, event):
            written.append(event)
    repo = ControlPlaneRepo(now=lambda: NOW, sink=Sink())
    repo.upsert_mission('MSN-one', title='Separate existing mission', source='herdr')
    before = copy.deepcopy(repo.list_missions())
    written.clear()
    repo.subscribe(notified.append)
    before_events = list(repo._event_order)
    result = IngestService(repo).ingest(envelope())
    assert result['accepted'] is True
    assert repo.list_missions() == before
    assert repo._event_order == before_events
    assert written == notified == []
    view = repo.mission_activity.get(KEY)
    assert view['mission']['title'] == 'Fix login'
    assert repo.mission_activity.get('MSN-one') is None


def test_received_clock_and_copies_do_not_fabricate_source_progress():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    body = envelope()
    IngestService(repo).ingest(body)
    body['payload']['activity']['workerDetails'][0]['id'] = 'mutated'
    view = repo.mission_activity.get(KEY)
    assert view['received_at'] == stamp()
    assert view['activity']['lastHeartbeatAt'] == stamp(NOW - 100)
    assert view['freshness']['state'] == 'live'
    view['activity']['workerDetails'][0]['id'] = 'mutated too'
    assert repo.mission_activity.get(KEY)['activity']['workerDetails'][0]['id'] == 'worker'
    clock[0] += 31
    assert repo.mission_activity.get(KEY)['freshness']['state'] == 'stale'
    clock[0] += 100
    assert repo.mission_activity.get(KEY)['freshness']['state'] == 'disconnected'
    assert repo.mission_activity.get(KEY)['mission']['status'] == 'active'


@pytest.mark.parametrize('changes', [
    {'mission_revision': 2}, {'observed_at': stamp(NOW - 1)},
    {'host_id': 'other'}, {'instance_id': 'other'}, {'session_id': 'other'},
])
def test_rejects_regression_and_live_identity_takeover(changes):
    repo = ControlPlaneRepo(now=lambda: NOW)
    service = IngestService(repo)
    service.ingest(envelope())
    result = service.ingest(envelope(**changes))
    assert result['accepted'] is False
    assert repo.mission_activity.get(KEY)['mission_revision'] == 3
    assert repo.mission_activity.get(KEY)['host_id'] == 'bender'


@pytest.mark.parametrize('changes', [
    {'observed_at': stamp(NOW + 1000)}, {'snapshot_at': 'bad'},
    {'mission_revision': -1}, {'mission_revision': True}, {'connection': 'crashed'},
    {'mission': {'id': 'MSN-other'}}, {'host_id': 'bad\x00'},
    {'contract_version': 2}, {'activity': []},
])
def test_invalid_payload_rejected(changes):
    repo = ControlPlaneRepo(now=lambda: NOW)
    with pytest.raises(ValueError):
        IngestService(repo).ingest(envelope(**changes))


def test_source_event_and_namespace_are_strict():
    service = IngestService(ControlPlaneRepo(now=lambda: NOW))
    for field, value in [('source', 'herdr'), ('entity_id', 'MSN-one'),
                         ('event_type', 'mission_activity.created')]:
        body = envelope()
        body[field] = value
        with pytest.raises(ValueError):
            service.ingest(body)


def test_redaction_and_bounds_apply_before_storage_and_preserve_test_counts():
    repo = ControlPlaneRepo(now=lambda: NOW)
    activity = {'tests': {'passed': 4}, 'reasoning': 'private thoughts',
                'workerDetails': [{'id': 'w', 'thinking': 'private', 'api_key': 'raw-secret',
                   'toolOutputPreview': 'Bearer abc123\x00\x1b[31m password=hunter2'}] * 200}
    IngestService(repo).ingest(envelope(activity=activity))
    view = repo.mission_activity.get(KEY)
    text = json.dumps(view)
    assert 'private' not in text and 'hunter2' not in text and 'abc123' not in text
    assert 'raw-secret' not in text and '\\u0000' not in text and '\\u001b' not in text
    assert view['activity']['tests']['passed'] == 4
    assert len(view['activity']['workerDetails']) <= 100
    summaries = repo.mission_activity.list_summaries()
    assert 'toolOutputPreview' not in summaries[0]['activity']['workerDetails'][0]
    summaries[0]['mission']['title'] = 'overwrite'
    assert repo.mission_activity.get(KEY)['mission']['title'] == 'Fix login'


def test_updating_and_disconnected_keep_last_known_detail():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    service = IngestService(repo)
    service.ingest(envelope())
    clock[0] += 5
    service.ingest(envelope(connection='updating', activity={}, observed_at=stamp(clock[0])))
    assert repo.mission_activity.get(KEY)['activity']['currentObjective'] == 'Run tests'
    clock[0] += 5
    service.ingest(envelope(connection='disconnected', activity={}, observed_at=stamp(clock[0])))
    assert repo.mission_activity.get(KEY)['freshness']['state'] == 'disconnected'
    assert repo.mission_activity.get(KEY)['activity']['currentObjective'] == 'Run tests'


@pytest.fixture
def server():
    srv = ControlPlaneServer(ControlPlaneRepo(now=lambda: NOW),
                             api_keys={'viewer': 'viewer', 'ingest': 'ingest'})
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    yield srv
    srv.shutdown()


def request(server, path, key='viewer', body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{server.port}{path}',
        headers={'X-API-Key': key, 'Content-Type': 'application/json'},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_http_routes_roles_empty_detail_and_capability(server):
    assert request(server, '/api/v1/mission-activity')[1]['activities'] == []
    assert request(server, '/api/v1/missions/pi%3AMSN-one/activity')[1] == {'activity': None}
    assert request(server, '/api/v1/ingest', body=envelope())[0] == 403
    assert request(server, '/api/v1/ingest', key='ingest', body=envelope())[0] == 200
    assert request(server, '/api/v1/missions/pi%3AMSN-one/activity')[1]['activity']['mission_key'] == KEY
    assert request(server, '/api/v1/mission-activity', key='bad')[0] == 401
    assert 'mission_activity_v1' in request(server, '/api/v1/health')[1]['capabilities']
    assert server.repo.list_missions() == []
    assert server.repo._event_order == []


def test_initial_updating_snapshot_has_no_fabricated_source_timestamp():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(envelope(connection='updating', snapshot_at=None, activity={}))
    value = repo.mission_activity.get(KEY)
    assert value['snapshot_at'] is None
    assert value['activity'] == {}


def test_retention_eviction_and_delayed_collection():
    from herdr_engineering.control_plane.mission_activity import MissionActivityStore
    clock = [NOW]
    store = MissionActivityStore(now=lambda: clock[0], max_missions=2)
    for number in range(3):
        body = envelope()
        body['payload']['mission']['id'] = f'MSN-{number}'
        store.ingest(f'pi:MSN-{number}', body['payload'], source='pi')
    assert store.get('pi:MSN-0') is None
    assert len(store.list_summaries()) == 2
    clock[0] += 130
    assert store.ingest(KEY, envelope()['payload'], source='pi')['accepted'] is False
    clock[0] += 86400
    assert store.list_summaries() == []


def test_explicit_disconnect_permits_new_owner_without_faking_progress():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    service = IngestService(repo)
    service.ingest(envelope())
    clock[0] += 1
    service.ingest(envelope(connection='disconnected', observed_at=stamp(clock[0])))
    clock[0] += 1
    assert service.ingest(envelope(instance_id='i2', session_id='s2',
                                  observed_at=stamp(clock[0])))['accepted'] is True
    assert repo.mission_activity.get(KEY)['instance_id'] == 'i2'


def test_oversized_payload_rejected_and_never_visible():
    repo = ControlPlaneRepo(now=lambda: NOW)
    with pytest.raises(ValueError, match='too large'):
        IngestService(repo).ingest(envelope(activity={'currentObjective': 'x' * 600_000}))
    assert repo.mission_activity.list_summaries() == []


def test_delayed_collection_is_stale_even_when_just_received():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(envelope(observed_at=stamp(NOW - 60), snapshot_at=stamp(NOW - 60)))
    view = repo.mission_activity.get(KEY)
    assert view['received_at'] == stamp()
    assert view['freshness']['state'] == 'stale'
    assert view['freshness']['age_seconds'] == 60


def test_private_reasoning_activity_entries_are_not_a_backdoor():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(envelope(activity={'activity': [
        {'type': 'reasoning', 'summary': 'private thoughts'},
        {'type': 'tool', 'summary': 'pytest'},
    ]}))
    value = repo.mission_activity.get(KEY)
    assert 'private thoughts' not in json.dumps(value)
    assert value['activity']['activity'] == [{'type': 'tool', 'summary': 'pytest'}]


def test_hostile_numeric_values_cannot_crash_sanitization():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(envelope(activity={'tests': {'total': 10 ** 500}}))
    assert repo.mission_activity.get(KEY)['activity']['tests']['total'] is None


def test_tool_output_preserves_lines_and_tabs_but_strips_unsafe_controls():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(envelope(activity={'workerDetails': [
        {'toolOutputPreview': 'first\n\tsecond\x1b[31m\x00\x07'}]}))
    assert repo.mission_activity.get(KEY)['activity']['workerDetails'][0]['toolOutputPreview'] == 'first\n\tsecond'


def test_updating_new_revision_never_presents_old_activity_as_live():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    service = IngestService(repo)
    service.ingest(envelope(mission_revision=1))
    clock[0] += 10
    service.ingest(envelope(mission_revision=2, connection='updating', activity={},
                            observed_at=stamp(clock[0]), snapshot_at=None))
    value = repo.mission_activity.get(KEY)
    assert value['freshness']['state'] == 'updating'
    assert value['mission_revision'] == 2
    assert value['activity_revision'] == 1
    assert value['activity_snapshot_at'] == stamp()
    clock[0] += 10
    service.ingest(envelope(mission_revision=2, connection='updating',
                            activity={'currentObjective': 'Unproven work'},
                            observed_at=stamp(clock[0]), snapshot_at=stamp(clock[0])))
    value = repo.mission_activity.get(KEY)
    assert value['freshness']['state'] == 'updating'
    assert value['activity_revision'] == 1
    assert value['activity_snapshot_at'] == stamp()
    assert value['activity']['currentObjective'] == 'Run tests'
    clock[0] += 10
    service.ingest(envelope(mission_revision=2, observed_at=stamp(clock[0]),
                            snapshot_at=stamp(clock[0])))
    value = repo.mission_activity.get(KEY)
    assert value['freshness']['state'] == 'live'
    assert value['activity_revision'] == 2
    assert value['activity_snapshot_at'] == stamp(clock[0])


@pytest.mark.parametrize('workers', [None, 'not-an-array', 42, True,
                                      [None, 'bad', 3, {'workerId': 'valid'}]])
def test_malformed_worker_details_cannot_break_bulk_mission_reads(workers):
    repo = ControlPlaneRepo(now=lambda: NOW)
    service = IngestService(repo)
    service.ingest(envelope(activity={'workerDetails': workers, 'currentObjective': 'visible'}))
    other = envelope()
    other['entity_id'] = 'pi:MSN-other'
    other['payload']['mission']['id'] = 'MSN-other'
    service.ingest(other)
    summaries = repo.mission_activity.list_summaries()
    assert len(summaries) == 2
    summary = next(item for item in summaries if item['mission_key'] == KEY)
    assert summary['activity']['currentObjective'] == 'visible'
    assert summary['activity']['workerDetails'] == (
        [{'workerId': 'valid'}] if isinstance(workers, list) else [])


def test_current_activity_cannot_expose_private_reasoning_by_type():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(envelope(activity={
        'currentActivity': {'type': 'thinking', 'summary': 'private chain of thought'},
        'currentObjective': 'Public task',
    }))
    assert 'private chain of thought' not in json.dumps(repo.mission_activity.get(KEY))
    assert 'private chain of thought' not in json.dumps(repo.mission_activity.list_summaries())
    assert repo.mission_activity.get(KEY)['activity']['currentObjective'] == 'Public task'
