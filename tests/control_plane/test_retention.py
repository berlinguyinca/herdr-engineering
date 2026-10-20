"""Retention preserves chronological identity when durable sequences have gaps/repeats."""
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.retention import apply_retention


def _repo():
    repo = ControlPlaneRepo()
    repo.import_state({'events': {
        'mission|one': [
            {'sequence': 100, 'event_id': 'expired', 'source_timestamp': '2026-01-01T00:00:00+00:00'},
            {'sequence': 100, 'event_id': 'live', 'source_timestamp': '2026-10-19T00:00:00+00:00'},
            {'sequence': 900, 'event_id': 'latest', 'source_timestamp': '2026-10-20T00:00:00+00:00'},
        ],
        'host|two': [{'sequence': 8000, 'event_id': 'other',
                      'source_timestamp': '2026-10-18T00:00:00+00:00'}],
    }, 'event_order': [('mission', 'one', 100), ('host', 'two', 8000),
                       ('mission', 'one', 100), ('mission', 'one', 900)]})
    return repo


def test_retention_without_expiry_keeps_sparse_and_duplicate_event_references():
    repo = _repo()
    result = apply_retention(repo, events_ttl_days=365, now='2026-10-20T12:00:00+00:00')
    assert result['events'] == 0
    assert [e['event_id'] for e in repo.recent_events()] == ['latest', 'live', 'other', 'expired']
    assert len(repo._event_order) == 4


def test_retention_removes_only_expired_occurrence_of_duplicate_sequence():
    repo = _repo()
    result = apply_retention(repo, now='2026-10-20T12:00:00+00:00')
    assert result['events'] == 1
    assert [e['event_id'] for e in repo.recent_events()] == ['latest', 'live', 'other']
    assert repo._event_order == [('host', 'two', 8000), ('mission', 'one', 100),
                                 ('mission', 'one', 900)]
    repo.record_event('mission', 'one', 'new')
    assert repo.recent_events(1)[0]['sequence'] == 901
