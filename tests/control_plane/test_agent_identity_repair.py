"""Pure, exact-coverage repair of historically conflicting identity journals."""
from copy import deepcopy
from itertools import permutations

import pytest

from herdr_engineering.control_plane.agent_identity import (
    AgentIdentityConflict,
    AgentIdentityRegistry,
)
from herdr_engineering.control_plane.agent_identity_repair import (
    plan_identity_repair,
    prepare_identity_replay,
)


def event(identity, name, number, *, version=1, stamp=None):
    captured = []
    AgentIdentityRegistry(persist=captured.append).allocate(identity, name)
    result = captured[0]
    result['payload']['version'] = version
    result.update(event_id=f'e{number}', sequence=number,
                  ingest_timestamp=stamp or f'2026-10-28T00:00:{number:02d}Z')
    return result


def repaired(history, *, number=50):
    plan = plan_identity_repair(history)
    corrections = []
    for index, record in enumerate(plan['corrections']):
        correction = event(record['identity'], record['display_name'], number + index)
        correction['payload'] = deepcopy(record)
        corrections.append(correction)
    marker = {'event_id': f'm{number}', 'entity_type': 'agent_identity_reconciliation',
              'entity_id': plan['repair_id'], 'event_type': 'agent_identity.reconciled',
              'sequence': number + len(corrections), 'ingest_timestamp': '2026-10-28T01:00:00Z',
              'payload': {'plan': plan, 'correction_event_ids': [e['event_id'] for e in corrections]}}
    return deepcopy(history) + corrections + [marker]


def records(plan):
    return {record['identity']: record for record in plan['records']}


def hydrate(events):
    replay, names, handles = prepare_identity_replay(events)
    registry = AgentIdentityRegistry(blocked_names=names, blocked_handles=handles)
    registry.hydrate(replay)
    return registry, names, handles


def test_contested_name_has_no_first_owner_and_unrelated_identity_is_unchanged():
    history = [event('a', 'Ada', 1), event('b', 'ADA', 2), event('c', 'Grace', 3)]
    before = deepcopy(history)
    plan = plan_identity_repair(history)
    assert plan['policy_version'] == 1
    assert plan['blocked_names'] == ['ada']
    assert plan['blocked_handles'] == ['ada']
    assert len(plan['records']) == 3
    assert {r['identity'] for r in plan['corrections']} == {'a', 'b'}
    assert records(plan)['c'] == history[2]['payload']
    for identity in ['a', 'b']:
        record = records(plan)[identity]
        assert record['display_name'].casefold() != 'ada'
        assert record['version'] == 2
        assert 'ada' not in record['reserved_names'] + record['reserved_handles']
    assert len({r['handle'] for r in plan['records']}) == 3
    assert {c['identity'] for c in plan['changes']} == {'a', 'b'}
    assert all(c['before']['version'] == 1 and c['after']['version'] == 2
               for c in plan['changes'])
    assert history == before
    assert hydrate(repaired(history))[0].get('c')['display_name'] == 'Grace'


def test_different_names_with_same_normalized_handle_are_both_disambiguated():
    history = [event('a', 'José', 1), event('b', 'Jose', 2)]
    plan = plan_identity_repair(history)
    assert plan['blocked_names'] == []
    assert plan['blocked_handles'] == ['jose']
    assert all(r['handle'] != 'jose' for r in plan['records'])
    assert 'josé' in records(plan)['a']['reserved_names']
    assert 'jose' in records(plan)['b']['reserved_names']


def test_missing_historical_reservations_are_restored_without_renaming():
    history = [event('a', 'Ada', 1), event('a', 'Grace', 2, version=4)]
    plan = plan_identity_repair(history)
    record = records(plan)['a']
    assert record == {'identity': 'a', 'display_name': 'Grace', 'handle': 'grace', 'version': 5,
                      'reserved_names': ['ada', 'grace'], 'reserved_handles': ['ada', 'grace']}
    assert plan['corrections'] == [record]
    replayed, _, _ = hydrate(repaired(history))
    assert replayed.allocate('b', 'Ada')['display_name'] != 'Ada'


def test_divergent_v1_uses_latest_arrival_and_preserves_all_aliases():
    history = [event('a', 'Ada', 1), event('a', 'Grace', 2), event('a', 'Linus', 3)]
    with pytest.raises(AgentIdentityConflict):
        hydrate(history)
    plan = plan_identity_repair(history)
    assert records(plan)['a']['display_name'] == 'Linus'
    assert records(plan)['a']['reserved_names'] == ['ada', 'grace', 'linus']
    assert records(plan)['a']['version'] == 2
    assert hydrate(repaired(history))[0].get('a')['display_name'] == 'Linus'


def test_maximum_version_precedes_arrival_and_sequence_then_event_id_break_ties():
    history = [event('a', 'Ada', 1, version=7), event('a', 'Grace', 2, version=2)]
    assert records(plan_identity_repair(history))['a']['display_name'] == 'Ada'
    history = [event('a', 'Ada', 1, stamp='same'), event('a', 'Grace', 2, stamp='same')]
    assert records(plan_identity_repair(history))['a']['display_name'] == 'Grace'
    history[0]['sequence'] = 2
    assert records(plan_identity_repair(history))['a']['display_name'] == 'Grace'


def test_plan_and_marker_replay_are_deterministic_under_permutation():
    history = [event('a', 'Ada', 1), event('b', 'Ada', 2), event('a', 'Grace', 3)]
    expected = plan_identity_repair(history)
    for permutation in permutations(history):
        assert plan_identity_repair(permutation) == expected
    journal = repaired(history)
    assert prepare_identity_replay(reversed(journal)) == prepare_identity_replay(journal)


def test_uncontested_current_names_do_not_change_when_only_old_alias_is_contested():
    history = [event('a', 'Ada', 1), event('b', 'Ada', 2),
               event('a', 'Grace', 3, version=2), event('b', 'Linus', 4, version=2)]
    plan = plan_identity_repair(history)
    assert plan['blocked_names'] == ['ada']
    assert plan['corrections'] == []
    assert [r['display_name'] for r in plan['records']] == ['Grace', 'Linus']


def test_reserved_aliases_participate_even_when_no_current_name_uses_them():
    a, b = event('a', 'Grace', 1), event('b', 'Linus', 2)
    for item in [a, b]:
        item['payload']['reserved_names'].append('ada')
        item['payload']['reserved_handles'].append('ada')
    plan = plan_identity_repair([a, b])
    assert plan['blocked_names'] == ['ada']
    assert plan['blocked_handles'] == ['ada']
    assert all(r['version'] == 2 for r in plan['records'])
    assert [r['display_name'] for r in plan['records']] == ['Grace', 'Linus']


def test_plan_is_json_serializable_and_digest_excludes_only_database_instance_metadata():
    import json

    history = [event('a', 'Ada', 1)]
    plan = plan_identity_repair(history)
    assert json.loads(json.dumps(plan)) == plan
    history[0]['recovery_instance'] = 'replacement-row'
    assert plan_identity_repair(history) == plan
    history[0]['sequence'] = 2
    assert plan_identity_repair(history)['input_digest'] != plan['input_digest']
    history[0]['sequence'] = 1
    history[0]['payload']['audit'] = 'changed'
    assert plan_identity_repair(history)['input_digest'] != plan['input_digest']


def test_duplicate_ids_are_deduplicated_only_for_identical_fingerprints():
    one = event('a', 'Ada', 1)
    duplicate = deepcopy(one)
    duplicate['recovery_instance'] = 'different'
    assert plan_identity_repair([one, duplicate]) == plan_identity_repair([one])
    duplicate['payload']['display_name'] = 'ADA'
    for operation in [plan_identity_repair, prepare_identity_replay]:
        with pytest.raises(ValueError):
            operation([one, duplicate])


@pytest.mark.parametrize('mutation', [
    lambda e: e['payload'].update(version=True),
    lambda e: e['payload'].update(version=0),
    lambda e: e['payload'].update(handle='wrong'),
    lambda e: e.update(entity_id='other'),
    lambda e: e['payload'].update(reserved_names=[]),
    lambda e: e['payload'].update(reserved_handles='ada'),
    lambda e: e.update(payload=None),
    lambda e: e['payload'].pop('identity'),
])
def test_malformed_record_rejected_even_when_a_newer_valid_record_exists(mutation):
    one = event('a', 'Ada', 1)
    mutation(one)
    with pytest.raises(ValueError):
        plan_identity_repair([one, event('a', 'Grace', 2, version=2)])


def test_ordinary_legacy_envelopes_without_ids_still_use_strict_registry():
    captured = []
    original = AgentIdentityRegistry(persist=captured.append)
    original.allocate('a', 'Ada')
    original.rename('a', 'Grace', 1)
    replay, names, handles = prepare_identity_replay(captured)
    assert replay == captured
    assert names == handles == []
    assert hydrate(captured)[0].get('a') == original.get('a')
    with pytest.raises(ValueError):
        plan_identity_repair(captured)


def test_no_marker_keeps_ordinary_conflicts_strict_and_orders_history():
    history = [event('a', 'Ada', 1), event('b', 'Ada', 2)]
    replay, names, handles = prepare_identity_replay(reversed(history))
    assert replay == history
    assert names == handles == []
    with pytest.raises(AgentIdentityConflict):
        AgentIdentityRegistry().hydrate(replay)


@pytest.mark.parametrize('mutation', [
    lambda rows: rows.pop(0),
    lambda rows: rows[0].update(sequence=999),
    lambda rows: rows[0]['payload'].update(audit='tampered'),
    lambda rows: rows[-1]['payload']['plan'].update(policy_version=2),
    lambda rows: rows[-1]['payload']['plan']['records'][0].update(display_name='Tampered'),
    lambda rows: rows[-1]['payload']['plan'].update(blocked_names=[]),
    lambda rows: rows[-1]['payload']['plan'].update(input_digest='wrong'),
    lambda rows: rows[-1]['payload']['plan'].update(repair_id='wrong'),
    lambda rows: rows[-1]['payload']['plan']['covered'].append(
        deepcopy(rows[-1]['payload']['plan']['covered'][0])),
    lambda rows: rows[-1]['payload'].pop('plan'),
    lambda rows: rows[-1]['payload'].update(correction_event_ids=[]),
    lambda rows: rows[-1]['payload']['correction_event_ids'].append('missing'),
    lambda rows: rows[-1]['payload']['correction_event_ids'].append(
        rows[-1]['payload']['correction_event_ids'][0]),
    lambda rows: rows[-1]['payload'].update(correction_event_ids=['e1', 'e2']),
    lambda rows: rows.pop(2),
    lambda rows: rows[2]['payload'].update(version=99),
    lambda rows: rows[2].update(event_type='unrelated'),
])
def test_marker_requires_exact_coverage_plan_and_correction_set(mutation):
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    mutation(journal)
    with pytest.raises(ValueError):
        prepare_identity_replay(journal)


def test_legitimate_backdated_tail_applies_after_baseline_and_conflicting_tail_stays_strict():
    history = [event('a', 'Ada', 1), event('b', 'Ada', 2)]
    journal = repaired(history)
    baseline = records(journal[-1]['payload']['plan'])['a']
    tail = event('a', 'Grace', 60, version=baseline['version'] + 1,
                 stamp='2020-01-01T00:00:00Z')
    tail['payload']['reserved_names'] += baseline['reserved_names']
    tail['payload']['reserved_handles'] += baseline['reserved_handles']
    registry, names, handles = hydrate(journal + [tail])
    assert registry.get('a')['display_name'] == 'Grace'
    assert names == handles == ['ada']
    with pytest.raises(AgentIdentityConflict):
        hydrate(journal + [tail, event('c', 'Grace', 61)])


def test_backdated_uncovered_conflict_is_never_silently_discarded():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    record = journal[-1]['payload']['plan']['records'][0]
    conflict = event(record['identity'], 'Grace', 60, version=record['version'],
                     stamp='2020-01-01T00:00:00Z')
    with pytest.raises(AgentIdentityConflict):
        hydrate(journal + [conflict])


def test_latest_marker_repeated_repair_preserves_original_blocked_alias_claims():
    first = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    second = repaired(first, number=70)
    second[-1]['ingest_timestamp'] = '2026-10-28T02:00:00Z'
    plan = second[-1]['payload']['plan']
    assert plan['blocked_names'] == plan['blocked_handles'] == ['ada']
    assert plan['corrections'] == []
    assert len(plan['covered']) == 4
    assert hydrate(second)[0].list() == hydrate(first)[0].list()
    assert prepare_identity_replay(reversed(second)) == prepare_identity_replay(second)


def test_empty_history_and_unrelated_events_are_noops():
    plan = plan_identity_repair([{'entity_type': 'session'}])
    assert plan['records'] == plan['covered'] == plan['corrections'] == plan['changes'] == []
    assert prepare_identity_replay([]) == ([], [], [])


def test_generated_names_cannot_steal_an_uncontested_historical_reservation():
    history = [event('a', 'Ada', 1), event('b', 'Ada', 2),
               event('c', 'Ada Amber', 3), event('c', 'Grace', 4, version=2)]
    plan = plan_identity_repair(history)
    assert records(plan)['a']['display_name'] == 'Ada Birch'
    assert records(plan)['b']['display_name'] == 'Ada Cedar'
    assert 'ada amber' in records(plan)['c']['reserved_names']


def test_alias_claims_use_existing_unicode_name_and_handle_normalization():
    first = event('a', 'Grace', 1)
    first['payload']['reserved_names'].append('ＡDA')
    first['payload']['reserved_handles'].append('ＡDA')
    plan = plan_identity_repair([first, event('b', 'Ada', 2)])
    assert plan['blocked_names'] == plan['blocked_handles'] == ['ada']
    assert records(plan)['a']['display_name'] == 'Grace'
    assert records(plan)['b']['display_name'] != 'Ada'
    assert 'ＡDA' not in records(plan)['a']['reserved_names']


def test_replay_outputs_are_defensive_copies_and_do_not_mutate_marker_history():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    original = deepcopy(journal)
    replay, names, handles = prepare_identity_replay(journal)
    replay[0]['payload']['display_name'] = 'Tampered'
    names.append('tampered')
    handles.append('tampered')
    assert journal == original
    assert prepare_identity_replay(journal)[1:] == (['ada'], ['ada'])


def test_marker_correction_id_order_is_a_set_not_an_event_order_requirement():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    expected = prepare_identity_replay(journal)
    journal[-1]['payload']['correction_event_ids'].reverse()
    assert prepare_identity_replay(journal) == expected


def test_latest_invalid_marker_never_falls_back_to_an_older_valid_marker():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    bad = deepcopy(journal[-1])
    bad.update(event_id='new-marker', sequence=100)
    bad['payload']['plan']['input_digest'] = 'tampered'
    with pytest.raises(ValueError):
        prepare_identity_replay([bad, *journal])


def test_tampered_duplicate_marker_id_is_rejected():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    bad = deepcopy(journal[-1])
    bad['payload']['plan']['input_digest'] = 'tampered'
    with pytest.raises(ValueError):
        prepare_identity_replay([*journal, bad])


def test_replayed_blocked_aliases_cannot_be_reassigned_or_resurrected_by_tail():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    registry, _, _ = hydrate(journal)
    assert registry.allocate('c', 'Ada')['display_name'] != 'Ada'
    with pytest.raises(AgentIdentityConflict):
        registry.rename('a', 'Ada', registry.get('a')['version'])
    with pytest.raises(AgentIdentityConflict):
        hydrate(journal + [event('c', 'Ada', 60)])


@pytest.mark.parametrize('with_marker', [False, True])
def test_malformed_duplicate_version_tail_cannot_hide_behind_version_deduplication(with_marker):
    history = [event('a', 'Ada', 1)]
    journal = repaired(history) if with_marker else history
    bad = event('a', 'Ada', 2)
    bad['payload']['reserved_names'] = []
    with pytest.raises(ValueError):
        prepare_identity_replay(journal + [bad])


def test_malformed_correction_entity_is_a_validation_error():
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    journal[2]['entity_id'] = ['invalid']
    with pytest.raises(ValueError):
        prepare_identity_replay(journal)


def test_valid_maximum_length_unicode_name_can_have_longer_casefolded_aliases():
    one = event('a', 'ß' * 80, 1)
    plan = plan_identity_repair([one])
    assert plan['records'] == [one['payload']]
    assert plan['corrections'] == []
    assert hydrate(repaired([one]))[0].get('a')['display_name'] == 'ß' * 80


@pytest.mark.parametrize('entity_id', ['another-repair', None, 123])
def test_marker_entity_must_bind_to_recomputed_repair_id(entity_id):
    journal = repaired([event('a', 'Ada', 1)])
    journal[-1]['entity_id'] = entity_id
    with pytest.raises(ValueError, match='marker.*identity'):
        prepare_identity_replay(journal)


def test_uncovered_backdated_older_version_cannot_hide_a_new_display_alias():
    journal = repaired([event('a', 'Grace', 1, version=2)])
    tail = event('a', 'Ada', 2, stamp='2020-01-01T00:00:00Z')
    with pytest.raises(AgentIdentityConflict, match='unrepresented'):
        prepare_identity_replay(journal + [tail])


@pytest.mark.parametrize('field', ['reserved_names', 'reserved_handles'])
@pytest.mark.parametrize('version', [1, 2])
def test_uncovered_stale_versions_cannot_hide_extra_reservations(field, version):
    journal = repaired([event('a', 'Grace', 1, version=2)])
    tail = event('a', 'Grace', 2, version=version)
    tail['payload'][field].append('ada')
    with pytest.raises(AgentIdentityConflict, match='unrepresented'):
        prepare_identity_replay(journal + [tail])


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('field', ['reserved_names', 'reserved_handles'])
@pytest.mark.parametrize('alias', ['ada', 'ＡDA'])
def test_uncovered_stale_versions_cannot_hide_blocked_alias_claims(version, field, alias):
    journal = repaired([event('a', 'Ada', 1), event('b', 'Ada', 2)])
    baseline = records(journal[-1]['payload']['plan'])['a']
    tail = event('a', baseline['display_name'], 3, version=version)
    tail['payload'][field].append(alias)
    with pytest.raises(AgentIdentityConflict, match='blocked'):
        prepare_identity_replay(journal + [tail])


def test_stale_alias_represented_only_by_another_identity_is_rejected():
    journal = repaired([event('a', 'Grace', 1, version=2), event('b', 'Ada', 2)])
    with pytest.raises(AgentIdentityConflict, match='unrepresented'):
        prepare_identity_replay(journal + [event('a', 'Ada', 3)])


def test_stale_tail_with_only_already_reserved_aliases_is_kept_and_safe_to_ignore():
    journal = repaired([event('a', 'Ada', 1), event('a', 'Grace', 2, version=2)])
    tail = event('a', 'Ada', 3, stamp='2020-01-01T00:00:00Z')
    replay, _, _ = prepare_identity_replay(journal + [tail])
    assert replay[-1] == tail
    registry, _, _ = hydrate(journal + [tail])
    assert registry.get('a')['display_name'] == 'Grace'
    assert registry.allocate('b', 'Ada')['display_name'] != 'Ada'


def test_same_version_tail_with_already_reserved_aliases_remains_idempotent():
    original = event('a', 'Grace', 1, version=2)
    journal = repaired([original])
    tail = event('a', 'Grace', 2, version=2)
    assert prepare_identity_replay(journal + [tail])[0][-1] == tail
    assert hydrate(journal + [tail])[0].get('a') == {
        'identity': 'a', 'display_name': 'Grace', 'handle': 'grace', 'version': 2,
    }


def test_stale_tail_is_checked_against_versions_advanced_by_prior_tail_events():
    journal = repaired([event('a', 'Grace', 1, version=2)])
    advanced = event('a', 'Linus', 2, version=4)
    advanced['payload']['reserved_names'].append('grace')
    advanced['payload']['reserved_handles'].append('grace')
    stale = event('a', 'Ada', 3, version=3)
    with pytest.raises(AgentIdentityConflict, match='unrepresented'):
        prepare_identity_replay(journal + [advanced, stale])


def test_prior_tail_reservations_can_explain_a_later_stale_tail():
    journal = repaired([event('a', 'Grace', 1, version=2)])
    advanced = event('a', 'Linus', 2, version=4)
    advanced['payload']['reserved_names'].append('ada')
    advanced['payload']['reserved_handles'].append('ada')
    stale = event('a', 'Ada', 3, version=3)
    older = event('a', 'Grace', 4, version=1)
    replay, _, _ = prepare_identity_replay(journal + [advanced, stale, older])
    assert replay[-3:] == [advanced, stale, older]
    registry, _, _ = hydrate(journal + [advanced, stale, older])
    assert registry.get('a')['display_name'] == 'Linus'
    assert registry.allocate('b', 'Ada')['display_name'] != 'Ada'
    assert registry.allocate('c', 'Grace')['display_name'] != 'Grace'


def test_no_marker_legacy_replay_keeps_existing_stale_version_semantics():
    history = [event('a', 'Grace', 1, version=2), event('a', 'Ada', 2)]
    for item in history:
        item.pop('event_id')
    replay, names, handles = prepare_identity_replay(history)
    assert replay == history
    assert names == handles == []
    registry, _, _ = hydrate(history)
    assert registry.get('a')['display_name'] == 'Grace'
    assert registry.allocate('b', 'Ada')['display_name'] == 'Ada'
