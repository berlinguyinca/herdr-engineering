"""Service catalog projection: reversible cleanup without losing history."""
import json

from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_bridge import FakeSource

DAY = 86400


def test_unavailable_heartbeats_do_not_extend_archive_grace_and_return_revives():
    clock = [1_800_000_000]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    sid = repo.upsert_service('bender', 'python on bender (port 8000)', 18000)
    clock[0] += DAY
    repo.upsert_service('bender', 'python on bender (port 8000)', 18000,
                        status='unavailable')
    since = repo.get_service(sid)['lifecycle']['unavailable_since']
    clock[0] += 7 * DAY
    repo.upsert_service('bender', 'python on bender (port 8000)', 18000,
                        status='unavailable')
    svc = repo.get_service(sid)
    assert svc['lifecycle']['archived'] is True
    assert svc['lifecycle']['unavailable_since'] == since
    assert repo.list_services(include_archived=False) == []
    assert len(repo.list_services()) == 1
    assert len(repo.stream('service', sid)) == 3
    assert repo.upsert_service('bender', svc['service_name'], 18001) == sid
    assert repo.get_service(sid)['lifecycle']['state'] == 'current'
    assert repo.get_service(sid)['lifecycle']['unavailable_since'] is None
    assert len(repo.list_services(include_archived=False)) == 1


def test_healthy_unknown_and_invalid_timestamps_never_autoarchive():
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
    for status, stamp in [('healthy', '2000-01-01T00:00:00Z'),
                          ('unavailable', None), ('unavailable', 'invalid'),
                          ('unknown', '2000-01-01T00:00:00Z')]:
        sid = repo.register_service(status, 'host', 1234, status=status)
        repo._services[sid]['updated_at'] = stamp
        repo._services[sid]['unavailable_since'] = stamp
        assert repo.get_service(sid)['lifecycle']['archived'] is False


def test_legacy_replay_recovers_first_unavailable_report_and_healthy_reset():
    events = []
    for seq, (status, stamp) in enumerate([
        ('healthy', '2026-01-01T00:00:00Z'),
        ('unavailable', '2026-01-02T00:00:00Z'),
        ('unavailable', '2026-01-12T00:00:00Z'),
    ]):
        events.append(dict(event_id=f'e{seq}', sequence=seq,
            entity_type='service', entity_id='svc', event_type='ServiceUpdated',
            payload=json.dumps(dict(service_name='web', status=status)),
            source_timestamp=stamp, ingest_timestamp=stamp))
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
    load_into(repo, FakeSource(events=events))
    svc = repo.get_service('svc')
    assert svc['lifecycle']['unavailable_since'] == '2026-01-02T00:00:00Z'
    assert svc['lifecycle']['archived'] is True
    events[-1]['payload'] = json.dumps(dict(service_name='web', status='healthy'))
    load_into(repo, FakeSource(events=events))
    assert repo.get_service('svc')['lifecycle']['archived'] is False


def test_catalog_explains_process_and_source_port_without_inventing_project():
    repo = ControlPlaneRepo()
    sid = repo.register_service('next-server on bender (port 3000)', 'bender',
                               18090, owner='next-server (v1')
    svc = repo.get_service(sid)
    assert svc['catalog']['category'] == 'development'
    assert 'Next.js' in svc['catalog']['purpose']
    assert svc['catalog']['source_port'] == 3000
    assert svc['catalog']['project'] is None
    assert svc['port'] == 18090


def test_project_uses_only_explicit_session_association():
    repo = ControlPlaneRepo()
    sid = repo.create_session()
    repo._sessions[sid]['cwd'] = '/work/real-project'
    svc = repo.register_service('web', 'bender', 8000, session_id=sid)
    assert repo.get_service(svc)['catalog']['project'] == 'real-project'
    assert repo.get_service(svc)['catalog']['project_source'] == 'session.cwd'


def test_stale_healthy_report_is_unknown_and_archive_days_are_configurable():
    clock = [1_800_000_000]
    repo = ControlPlaneRepo(now=lambda: clock[0], service_archive_days=2)
    healthy = repo.register_service('web', 'host', 1234)
    inactive = repo.register_service('old', 'host', 1235, status='unavailable')
    clock[0] += 2 * DAY
    assert repo.get_service(healthy)['lifecycle']['state'] == 'unknown'
    assert repo.get_service(healthy)['lifecycle']['archived'] is False
    assert repo.get_service(inactive)['lifecycle']['archived'] is True


def test_mark_unavailable_is_durable_and_replay_keeps_project_and_session():
    clock = [1_800_000_000]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    sid = repo.create_session()
    repo._sessions[sid]['cwd'] = '/work/project'
    svc = repo.register_service('web', 'host', 18000, session_id=sid,
                               project='explicit-project', source_port=8000)
    clock[0] += DAY
    assert repo.mark_service_unavailable(svc) is True
    events = repo.stream('service', svc)
    assert len(events) == 2
    clone = ControlPlaneRepo(now=lambda: clock[0] + 7 * DAY)
    load_into(clone, FakeSource(events=events))
    restored = clone.get_service(svc)
    assert restored['lifecycle']['archived'] is True
    assert restored['session_id'] == sid
    assert restored['catalog']['project'] == 'explicit-project'
    assert restored['catalog']['source_port'] == 8000
    # Repeated calls do not turn an unchanged state into event noise.
    assert repo.mark_service_unavailable(svc) is True
    assert len(repo.stream('service', svc)) == 2


def test_malformed_provenance_is_bounded_even_for_legacy_records():
    repo = ControlPlaneRepo()
    sid = repo.register_service('web', 'host', 80, project={'private': 'value'},
                               source_port=True)
    assert repo.get_service(sid)['catalog']['project'] is None
    assert repo.get_service(sid)['catalog']['source_port'] is None
    repo._services[sid]['project'] = '/private/path/to/project\x1b'
    repo._services[sid]['source_port'] = 70000
    assert repo.get_service(sid)['catalog']['project'] == 'project'
    assert repo.get_service(sid)['catalog']['source_port'] is None
    repo._services[sid]['project'] = 'x' * 1000
    assert len(repo.get_service(sid)['catalog']['project']) == 120


def test_newer_service_events_override_old_structured_health_in_both_directions():
    for previous, current in [('healthy', 'unavailable'), ('unavailable', 'healthy')]:
        events = [dict(event_id='event', sequence=1, entity_type='service',
                       entity_id='svc', event_type='ServiceUpdated',
                       payload=dict(status=current, service_name='web'),
                       source_timestamp='2026-01-10T00:00:00Z',
                       ingest_timestamp='2026-01-10T00:00:00Z')]
        structured = [dict(service_id='svc', service_name='web', status=previous,
                           created_at='2026-01-01T00:00:00Z',
                           updated_at='2026-01-01T00:00:00Z')]
        repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
        load_into(repo, FakeSource(services=structured, events=events))
        svc = repo.get_service('svc')
        assert svc['status'] == current
        assert svc['lifecycle']['archived'] is (current == 'unavailable')


def test_newer_structured_service_health_wins_over_older_event():
    event = dict(event_id='event', sequence=1, entity_type='service',
                 entity_id='svc', event_type='ServiceUpdated',
                 payload=dict(status='unavailable', service_name='web'),
                 source_timestamp='2026-01-01T00:00:00Z',
                 ingest_timestamp='2026-01-01T00:00:00Z')
    structured = dict(service_id='svc', service_name='web', status='healthy',
                      created_at='2026-01-01T00:00:00Z',
                      updated_at='2026-01-10T00:00:00Z')
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
    load_into(repo, FakeSource(services=[structured], events=[event]))
    assert repo.get_service('svc')['status'] == 'healthy'
    assert repo.get_service('svc')['lifecycle']['archived'] is False


def test_newer_structured_unavailable_row_uses_its_own_observation():
    event = dict(event_id='event', sequence=1, entity_type='service',
                 entity_id='svc', event_type='ServiceUpdated',
                 payload=dict(status='healthy', service_name='web'),
                 source_timestamp='2026-01-01T00:00:00Z',
                 ingest_timestamp='2026-01-01T00:00:00Z')
    structured = dict(service_id='svc', service_name='web', status='unavailable',
                      created_at='2026-01-01T00:00:00Z',
                      updated_at='2026-01-10T00:00:00Z')
    repo = ControlPlaneRepo(now=lambda: 1_800_000_000)
    load_into(repo, FakeSource(services=[structured], events=[event]))
    svc = repo.get_service('svc')
    assert svc['status'] == 'unavailable'
    assert svc['lifecycle']['unavailable_since'] == '2026-01-10T00:00:00Z'
    assert svc['lifecycle']['archived'] is True


def test_operator_retirement_archives_silent_services_and_a_new_report_restores_them():
    clock = [1_800_000_000]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    old = repo.register_service("old dev server", "host", 8000, status="healthy")
    clock[0] += 10 * 86400
    fresh = repo.register_service("fresh server", "host", 8001, status="healthy")
    assert repo.get_service(old)["lifecycle"]["state"] == "unknown"      # never auto-archived
    dry = repo.retire_services(older_than_days=7)
    assert dry == {"matched": 1, "retired": 0, "dry_run": True, "older_than_days": 7.0, "sample": [old]}
    assert repo.get_service(old)["lifecycle"]["archived"] is False
    done = repo.retire_services(older_than_days=7, dry_run=False)
    assert done["retired"] == 1
    assert repo.get_service(old)["lifecycle"]["state"] == "archived"
    assert "Retired by an operator" in repo.get_service(old)["lifecycle"]["reason"]
    assert repo.get_service(fresh)["lifecycle"]["state"] == "current"
    assert repo.retire_services(older_than_days=7, dry_run=False)["matched"] == 0
    clock[0] += 60
    repo.upsert_service("host", "old dev server", 8000, status="healthy")  # the service reports again
    assert repo.get_service(old)["lifecycle"]["state"] == "current"


def test_retirement_survives_event_replay_without_refreshing_the_service():
    clock = [1_800_000_000]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    sid = repo.register_service("old", "host", 8000, status="healthy")
    clock[0] += 10 * 86400
    repo.retire_services(older_than_days=7, dry_run=False)
    events = [e for e in repo.stream("service", sid)]
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events
    services = _reconstruct_from_events({f"service:{sid}": events})[3]
    assert services[sid]["retired_at"]
