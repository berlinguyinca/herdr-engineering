"""G8-B: Pi snapshot contract v4 (additive): created_at, completed_at, links. Real store, no mocks.

Pi-reported times are evidence only. They never feed historyComplete/coverage (see handoffs/G8.md)."""
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.mission_activity import (
    MAX_LINKS,
    clean_pi_fields,
)
from herdr_engineering.control_plane.mission_outcome import (
    issue_refs_by_mission,
    pr_refs_by_mission,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000


def stamp(epoch=NOW):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def payload(key='MSN-1', **mission_extra):
    return {'contract_version': 1, 'host_id': 'h', 'instance_id': 'i', 'session_id': 's', 'observed_at': stamp(),
            'snapshot_at': stamp(), 'mission_revision': 1, 'connection': 'connected',
            'mission': {'id': key, 'title': 't', 'status': 'RUNNING', **mission_extra}, 'activity': {'health': 'active'}}


def ingest(repo, key='MSN-1', **extra):
    return IngestService(repo).ingest({'entity_type': 'mission_activity', 'entity_id': f'pi:{key}', 'source': 'pi',
                                       'event_type': 'mission_activity.snapshot', 'payload': payload(key, **extra)})


def link(kind='pull_request', repository='acme/app', number=7):
    return {'kind': kind, 'repository': repository, 'number': number}


def test_valid_fields_are_kept_normalized():
    out = clean_pi_fields({'created_at': '2026-11-01T10:00:00Z', 'completed_at': '2026-11-02T11:30:00+00:00',
                           'links': [link(), link('issue', 'Acme/App', 12)]}, now=NOW * 2)
    assert out['created_at'] == '2026-11-01T10:00:00+00:00' and out['completed_at'] == '2026-11-02T11:30:00+00:00'
    assert out['links'] == [link(), link('issue', 'Acme/App', 12)]


@pytest.mark.parametrize('bad', [None, '', 'yesterday', 12, '2026-11-01T10:00:00', '2026-11-01', 'x' * 100, ['a'], {}])
def test_malformed_timestamps_are_dropped_never_inferred(bad):
    assert clean_pi_fields({'created_at': bad, 'completed_at': bad}, now=NOW * 2) == {}


def test_future_timestamps_are_dropped():
    assert clean_pi_fields({'completed_at': stamp(NOW + 3600)}, now=NOW) == {}


@pytest.mark.parametrize('bad', [
    link('branch'), link('Issue'), link(None), link(repository='noslash'), link(repository='a/b/c'), link(repository='a b/c'),
    link(repository='../x'), link(repository=''), link(repository=5), link(number=0), link(number=-3), link(number=True),
    link(number='7'), link(number=1.5), link(number=2 ** 40), 'str', None, 7, {}, {'kind': 'issue'},
])
def test_malformed_links_are_dropped_and_good_ones_survive(bad):
    out = clean_pi_fields({'links': [bad, link('issue', 'acme/app', 9)]}, now=NOW)
    assert out == {'links': [link('issue', 'acme/app', 9)]}


def test_links_must_be_a_list_and_are_deduplicated_and_bounded():
    assert clean_pi_fields({'links': 'acme/app#7'}, now=NOW) == {}
    assert clean_pi_fields({'links': {'kind': 'issue'}}, now=NOW) == {}
    dup = clean_pi_fields({'links': [link(), link(repository='ACME/app'), link()]}, now=NOW)
    assert len(dup['links']) == 1
    many = clean_pi_fields({'links': [link(number=n) for n in range(1, 200)]}, now=NOW)
    assert len(many['links']) == MAX_LINKS


def test_store_keeps_cleaned_fields_and_drops_unknown_mission_keys():
    repo = ControlPlaneRepo(now=lambda: NOW)
    ingest(repo, created_at='2026-11-01T10:00:00Z', completed_at='nope', links=[link(), link('commit')], secret_note='x')
    mission = repo.mission_activity.get('pi:MSN-1')['mission']
    assert mission['created_at'] == '2026-11-01T10:00:00+00:00'
    assert 'completed_at' not in mission and mission['links'] == [link()] and 'secret_note' not in mission
    summary = repo.mission_activity.list_summaries()[0]['mission']
    assert summary['created_at'] == '2026-11-01T10:00:00+00:00' and 'links' not in summary


def test_absent_fields_leave_the_stored_mission_exactly_as_before():
    repo = ControlPlaneRepo(now=lambda: NOW)
    ingest(repo)
    mission = repo.mission_activity.get('pi:MSN-1')['mission']
    assert not {'created_at', 'completed_at', 'links'} & set(mission)
    assert pr_refs_by_mission(repo) == {} and issue_refs_by_mission(repo) == {}


def test_pi_pr_links_merge_into_pr_refs_for_running_missions_deduped_against_herdr_records():
    repo = ControlPlaneRepo(now=lambda: NOW)
    repo.upsert_mission('MSN-1', source='pi', title='Pi mission', stage='implementation')
    repo.upsert_pull_request('acme/app#7', mission_id='pi:MSN-1', repository='acme/app', number=7, status='open')
    ingest(repo, links=[link(number=7), link(number=8), link('issue', 'acme/app', 3)])
    refs = pr_refs_by_mission(repo)
    assert [(r['repository'], r['number']) for r in refs['pi:MSN-1']] == [('acme/app', 7), ('acme/app', 8)]
    assert issue_refs_by_mission(repo) == {'pi:MSN-1': [{'repository': 'acme/app', 'number': 3}]}


def test_pi_pr_link_for_a_mission_with_no_herdr_record_still_verifies():
    repo = ControlPlaneRepo(now=lambda: NOW)
    ingest(repo, 'ONLY', links=[link(number=21)])
    assert pr_refs_by_mission(repo) == {'pi:ONLY': [{'repository': 'acme/app', 'number': 21}]}


def test_links_from_a_disconnected_update_are_kept_with_the_retained_snapshot_and_expire_with_it():
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    ingest(repo, links=[link(number=5)])
    clock[0] += 25 * 3600
    assert pr_refs_by_mission(repo) == {}
