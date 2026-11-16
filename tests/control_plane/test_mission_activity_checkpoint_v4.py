"""B1: a v4 snapshot (created_at / completed_at / links) must survive checkpoint export -> restore, including through the
publication validator, while older checkpoints without those fields stay restorable and bad v4 shapes stay rejected."""
import copy
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.bridge import DurableSource, _load_full_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.mission_activity import MissionActivityStore
from herdr_engineering.control_plane.projection_checkpoint import capture_projection
from herdr_engineering.control_plane.projection_recovery import _validate_publication
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000
LINKS = [{'kind': 'issue', 'repository': 'acme/app', 'number': 3},
         {'kind': 'pull_request', 'repository': 'acme/app', 'number': 9}]


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def snapshot(key='MSN-1', **mission):
    return {'entity_type': 'mission_activity', 'entity_id': f'pi:{key}', 'source': 'pi',
            'event_type': 'mission_activity.snapshot',
            'payload': {'contract_version': 1, 'host_id': 'h', 'instance_id': 'i', 'session_id': 's',
                        'observed_at': stamp(NOW), 'snapshot_at': stamp(NOW), 'mission_revision': 1,
                        'connection': 'connected',
                        'mission': {'id': key, 'title': 'T', 'status': 'RUNNING', **mission},
                        'activity': {'health': 'active'}}}


def v4_repo():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(snapshot(created_at=stamp(NOW - 600), completed_at=stamp(NOW - 5), links=LINKS))
    assert repo.mission_activity.get('pi:MSN-1')['mission']['links'] == LINKS
    return repo


class EmptySource(DurableSource):
    def __init__(self):
        pass

    def missions(self): return []
    def sessions(self): return []
    def hosts(self): return []
    def services(self): return []
    def events(self): return []


def test_v4_checkpoint_roundtrips_through_a_fresh_store():
    state = v4_repo().mission_activity.export_checkpoint()
    fresh = MissionActivityStore(now=lambda: NOW)
    fresh.restore_checkpoint(state)
    mission = fresh.get('pi:MSN-1')['mission']
    assert mission['links'] == LINKS and mission['created_at'] == stamp(NOW - 600)
    assert mission['completed_at'] == stamp(NOW - 5)


def test_v4_checkpoint_passes_the_publication_validator():
    repo = ControlPlaneRepo(now=lambda: NOW)
    _load_full_into(repo, EmptySource(), capture_event_core=True)
    IngestService(repo).ingest(snapshot(created_at=stamp(NOW - 600), links=LINKS))
    capsule = capture_projection(repo, repo._checkpoint_event_core,
                                 coverage={'position': 1, 'legacy_rows': 0, 'public_rows': 0})
    _validate_publication(repo, capsule, {name: [] for name in ('missions', 'sessions', 'hosts', 'services')})


def test_checkpoint_without_v4_fields_still_restores():
    repo = ControlPlaneRepo(now=lambda: NOW)
    IngestService(repo).ingest(snapshot())
    state = repo.mission_activity.export_checkpoint()
    assert 'links' not in state['entries'][0][1]['value']['mission']
    fresh = MissionActivityStore(now=lambda: NOW)
    fresh.restore_checkpoint(state)
    assert fresh.get('pi:MSN-1')['mission']['id'] == 'MSN-1'


@pytest.mark.parametrize('field,bad', [
    ('links', [{'kind': 'issue', 'repository': 'bad repo', 'number': 1}]),
    ('links', 'x'),
    ('created_at', 'yesterday'),
    ('completed_at', 5),
    ('unexpected', 'x'),
])
def test_invalid_v4_shapes_in_a_checkpoint_are_rejected(field, bad):
    state = copy.deepcopy(v4_repo().mission_activity.export_checkpoint())
    state['entries'][0][1]['value']['mission'][field] = bad
    with pytest.raises(ValueError):
        MissionActivityStore(now=lambda: NOW).restore_checkpoint(state)
