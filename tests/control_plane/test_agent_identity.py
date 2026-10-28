"""Stable, durable, globally unique agent display names."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.control_plane.agent_identity import (
    AgentIdentityConflict,
    AgentIdentityRegistry,
    AgentIdentityVersionConflict,
)


def test_preferred_name_is_normalized_stable_and_defensively_copied():
    events = []
    registry = AgentIdentityRegistry(persist=events.append)
    first = registry.allocate('session:one', '  Ada   Lovelace  ')
    assert first == {'identity': 'session:one', 'display_name': 'Ada Lovelace',
                     'handle': 'ada-lovelace', 'version': 1}
    first['display_name'] = 'Tampered'
    assert registry.allocate('session:one', 'Late Provider Name')['display_name'] == 'Ada Lovelace'
    assert len(events) == 1
    assert events[0]['entity_type'] == 'agent_identity'
    assert events[0]['event_type'] == 'agent_identity.updated'
    assert events[0]['entity_id'] == 'session:one'
    assert registry.get('missing') is None
    listing = registry.list()
    listing[0]['handle'] = 'tampered'
    assert registry.get('session:one')['handle'] == 'ada-lovelace'


def test_generated_names_are_deterministic_human_readable_and_unique():
    registry = AgentIdentityRegistry()
    result = registry.allocate('session:one')
    assert result == AgentIdentityRegistry().allocate('session:one')
    assert len(result['display_name'].split()) >= 2
    assert all(word.isalpha() for word in result['display_name'].split())
    names = [registry.allocate(str(index), 'Ada')['display_name'] for index in range(100)]
    assert len(set(names)) == 100
    assert names[0] == 'Ada'
    assert all(all(word.isalpha() for word in name.split()) for name in names)


def test_normalized_names_and_handles_cannot_collide():
    registry = AgentIdentityRegistry()
    first = registry.allocate('first', 'Ａda Lovelace')
    second = registry.allocate('second', 'ada-lovelace')
    third = registry.allocate('third', 'ADA LOVELACE')
    assert first['display_name'] == 'Ada Lovelace'
    assert len({item['handle'] for item in [first, second, third]}) == 3
    with pytest.raises(AgentIdentityConflict):
        registry.rename('second', 'ada lovelace', 1)


def test_concurrent_allocations_reserve_names_atomically():
    registry = AgentIdentityRegistry()
    with ThreadPoolExecutor(max_workers=12) as pool:
        names = list(pool.map(lambda index: registry.allocate(str(index), 'Ada'), range(120)))
    assert len({item['handle'] for item in names}) == 120
    assert len({item['display_name'].casefold() for item in names}) == 120


def test_persistence_failure_does_not_publish_or_reserve_a_name():
    def fail(event):
        assert registry.get(event['entity_id']) is None
        raise RuntimeError('storage unavailable')

    registry = AgentIdentityRegistry(persist=fail)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        registry.allocate('one', 'Ada')
    assert registry.list() == []


def test_rename_optimistic_version_and_acknowledgement():
    events = []
    registry = AgentIdentityRegistry(persist=events.append)
    registry.allocate('one', 'Ada')
    renamed = registry.rename('one', 'Grace Hopper', 1)
    assert renamed == {'identity': 'one', 'display_name': 'Grace Hopper',
                       'handle': 'grace-hopper', 'version': 2}
    with pytest.raises(AgentIdentityVersionConflict):
        registry.rename('one', 'Different', 1)
    assert registry.rename('one', 'Grace Hopper', 2) == renamed
    assert len(events) == 2
    with pytest.raises(KeyError):
        registry.rename('missing', 'Ada', 1)


def test_failed_rename_keeps_previous_name_and_version():
    def persist(event):
        if event['payload']['version'] > 1:
            assert registry.get('one')['display_name'] == 'Ada'
            raise RuntimeError('storage unavailable')

    registry = AgentIdentityRegistry(persist=persist)
    registry.allocate('one', 'Ada')
    with pytest.raises(RuntimeError):
        registry.rename('one', 'Grace', 1)
    assert registry.get('one')['display_name'] == 'Ada'
    assert registry.get('one')['version'] == 1
    assert registry.allocate('two', 'Grace')['display_name'] == 'Grace'


def test_replay_restores_names_versions_and_old_name_reservations():
    events = []
    original = AgentIdentityRegistry(persist=events.append)
    original.allocate('one', 'Ada')
    original.rename('one', 'Grace', 1)
    replayed = AgentIdentityRegistry(persist=lambda event: pytest.fail('replay must not persist'))
    replayed.hydrate(events + events)
    assert replayed.list() == original.list()
    assert replayed.allocate('one', 'Late') == original.get('one')
    registry = AgentIdentityRegistry()
    registry.hydrate(events)
    assert registry.allocate('two', 'Ada')['display_name'] != 'Ada'
    assert registry.allocate('three', 'Grace')['display_name'] != 'Grace'


def test_acknowledged_global_record_wins_over_tentative_local_allocation():
    canonical = {'identity': 'one', 'display_name': 'Ada Amber', 'handle': 'ada-amber',
                 'version': 2, 'reserved_names': ['ada-amber', 'ada amber'],
                 'reserved_handles': ['ada-amber']}
    registry = AgentIdentityRegistry(persist=lambda event: canonical if event['entity_id'] == 'one' else None)
    assert registry.allocate('one', 'Ada') == {key: canonical[key]
                                             for key in ('identity', 'display_name', 'handle', 'version')}
    assert registry.allocate('two', 'Ada Amber')['display_name'] != 'Ada Amber'


def test_explicit_legacy_reset_quarantine_keeps_aliases_and_requires_a_versioned_choice():
    events = []
    for name in ('Ada', 'Grace'):
        AgentIdentityRegistry(persist=events.append).allocate('one', name)
    restored = AgentIdentityRegistry(persist=events.append)
    restored.hydrate(events, quarantine_conflicts=True, tolerate_version_resets=True)
    assert restored.is_conflicted('one')
    with pytest.raises(AgentIdentityConflict):
        restored.allocate('one')
    assert restored.allocate('other', 'Ada')['display_name'] != 'Ada'
    assert restored.allocate('third', 'Grace')['display_name'] != 'Grace'
    choice = restored.get('one')['display_name']
    renamed = restored.rename('one', choice, 1)
    assert renamed['version'] == 2 and not restored.is_conflicted('one')
    replayed = AgentIdentityRegistry()
    replayed.hydrate(events, quarantine_conflicts=True, tolerate_version_resets=True)
    assert replayed.get('one') == renamed and not replayed.is_conflicted('one')


def test_replay_ignores_unrelated_events_but_rejects_conflicting_reservations():
    events = []
    original = AgentIdentityRegistry(persist=events.append)
    original.allocate('one', 'Ada')
    registry = AgentIdentityRegistry()
    registry.hydrate([{'entity_type': 'session', 'payload': {}}])
    registry.allocate('two', 'Ada')
    with pytest.raises(AgentIdentityConflict):
        registry.hydrate(events)
    assert registry.get('one') is None


@pytest.mark.parametrize('name', ['', '   ', '<script>', 'Ada\nLovelace', 'A' * 81, 42])
def test_invalid_explicit_names_are_rejected(name):
    registry = AgentIdentityRegistry()
    registry.allocate('one', 'Ada')
    with pytest.raises(ValueError):
        registry.rename('one', name, 1)


@pytest.mark.parametrize('title', [
    'codex: /home/wohlgemuth/IdeaProjects/herdr-engineering',
    'Claude Code 🤖 — fixing agent summaries',
    '', '   ', 'A' * 81, 'title\nwith controls', 42, '<script>',
])
def test_arbitrary_provider_titles_fall_back_to_stable_friendly_names(title):
    events = []
    registry = AgentIdentityRegistry(persist=events.append)
    expected = AgentIdentityRegistry().allocate('terminal:one')
    assert registry.allocate('terminal:one', title) == expected
    assert registry.allocate('terminal:one', 'Late Provider') == expected
    restored = AgentIdentityRegistry()
    restored.hydrate(events)
    assert restored.allocate('terminal:one', title) == expected


@pytest.mark.parametrize('identity', ['', ' ', None, 123, 'one\nother'])
def test_invalid_identities_are_rejected(identity):
    with pytest.raises(ValueError):
        AgentIdentityRegistry().allocate(identity)


def test_concurrent_renames_only_accept_one_version():
    registry = AgentIdentityRegistry()
    registry.allocate('one', 'Ada')

    def rename(name):
        try:
            return registry.rename('one', name, 1)
        except AgentIdentityVersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rename, ['Grace', 'Linus']))
    assert len([result for result in results if result is not None]) == 1
    assert registry.get('one')['version'] == 2


def test_replay_rejects_divergent_payload_at_same_version():
    events = []
    original = AgentIdentityRegistry(persist=events.append)
    original.allocate('one', 'Ada')
    different = AgentIdentityRegistry()
    different.allocate('one', 'Grace')
    with pytest.raises(AgentIdentityConflict):
        different.hydrate(events)
    assert different.get('one')['display_name'] == 'Grace'


def test_failed_acknowledgement_does_not_publish():
    registry = AgentIdentityRegistry(persist=lambda event: False)
    with pytest.raises(RuntimeError, match='acknowledged'):
        registry.allocate('one', 'Ada')
    assert registry.get('one') is None


def _legacy_identity_events(second_name='Ada'):
    events = []
    for identity, name in [('one', 'Ada'), ('two', second_name), ('three', 'Grace')]:
        AgentIdentityRegistry(persist=events.append).allocate(identity, name)
    return events


@pytest.mark.parametrize('second_name', ['Ada', 'Áda'])
def test_quarantine_recovers_all_identities_without_writes_and_strict_still_rejects(second_name):
    events = _legacy_identity_events(second_name)
    original = copy.deepcopy(events)
    for kwargs in [{}, {'quarantine_conflicts': False}]:
        with pytest.raises(AgentIdentityConflict):
            AgentIdentityRegistry().hydrate(events, **kwargs)
    registry = AgentIdentityRegistry(persist=lambda event: pytest.fail('replay must not persist'))
    registry.hydrate(iter(events + events), quarantine_conflicts=True)
    assert events == original
    assert len(registry.list()) == 3
    assert registry.conflict_count == 2
    assert not registry.is_conflicted('missing')
    assert not registry.is_conflicted('three')
    assert registry.allocate('three')['display_name'] == 'Grace'
    for identity, name in [('one', 'Ada'), ('two', second_name)]:
        assert registry.is_conflicted(identity)
        assert registry.get(identity) == {
            'identity': identity, 'display_name': name, 'handle': 'ada', 'version': 1,
        }
        with pytest.raises(AgentIdentityConflict, match='rename.*stable'):
            registry.allocate(identity, 'Ignored Provider Name')


@pytest.mark.parametrize('second_name', ['Ada', 'Áda'])
def test_quarantine_renames_preserve_versions_and_block_old_aliases_after_snapshot(second_name):
    events = _legacy_identity_events(second_name)
    updates = []

    def persist(event):
        assert registry.get(event['entity_id'])['version'] == event['payload']['version'] - 1
        updates.append(event)

    registry = AgentIdentityRegistry(persist=persist)
    registry.hydrate(events, quarantine_conflicts=True)
    for identity, new_name, remaining in [('one', 'Birch', 1), ('two', 'Cedar', 0)]:
        with pytest.raises(AgentIdentityConflict):
            registry.rename(identity, 'Ada', 1)
        assert registry.rename(identity, new_name, 1)['version'] == 2
        assert registry.conflict_count == remaining
        assert not registry.is_conflicted(identity)
        with pytest.raises(AgentIdentityVersionConflict):
            registry.rename(identity, 'Elm', 1)
        for claimant in ['one', 'two', 'three']:
            with pytest.raises(AgentIdentityConflict):
                registry.rename(claimant, 'Ada', registry.get(claimant)['version'])
    assert len(updates) == 2
    assert updates[0]['payload']['reserved_names'] == ['ada', 'birch']
    assert updates[0]['payload']['reserved_handles'] == ['ada', 'birch']
    snapshot = [updates[0], updates[1], events[2]]
    for replay in [events + updates + events, snapshot, list(reversed(snapshot))]:
        recovered = AgentIdentityRegistry()
        recovered.hydrate(replay, quarantine_conflicts=True)
        assert {r['identity']: r for r in recovered.list()} == {
            r['identity']: r for r in registry.list()
        }
        assert recovered.conflict_count == 0
        for identity in ['one', 'two', 'three']:
            with pytest.raises(AgentIdentityConflict):
                recovered.rename(identity, 'Ada', recovered.get(identity)['version'])
        assert recovered.allocate('four', 'Ada')['handle'] != 'ada'


@pytest.mark.parametrize('acknowledge', [False, 'raise'])
def test_quarantine_failed_rename_keeps_conflict_and_does_not_reserve_new_name(acknowledge):
    def fail(event):
        if event['entity_id'] == 'four':
            return True
        assert registry.conflict_count == 2
        assert registry.get('one')['version'] == 1
        if acknowledge == 'raise':
            raise RuntimeError('storage unavailable')
        return False

    registry = AgentIdentityRegistry(persist=fail)
    registry.hydrate(_legacy_identity_events(), quarantine_conflicts=True)
    with pytest.raises(RuntimeError):
        registry.rename('one', 'Birch', 1)
    assert registry.conflict_count == 2
    assert registry.get('one')['version'] == 1
    # An unacknowledged rename must not make the new name unavailable.
    assert registry.allocate('four', 'Birch')['display_name'] == 'Birch'


@pytest.mark.parametrize('change', [
    {'identity': 'wrong'}, {'display_name': ' Ada'}, {'handle': 'wrong'},
    {'version': True}, {'version': 0}, {'reserved_names': 'ada'},
    {'reserved_handles': []}, {'reserved_names': [42]},
])
def test_quarantine_still_rejects_malformed_events_before_publishing(change):
    events = _legacy_identity_events()
    malformed = copy.deepcopy(events[0])
    malformed['payload'].update(change)
    registry = AgentIdentityRegistry()
    with pytest.raises(ValueError):
        registry.hydrate(events + [malformed], quarantine_conflicts=True)
    assert registry.list() == []


def test_quarantine_rejects_same_identity_divergence_and_preserves_existing_records():
    events = _legacy_identity_events()
    different = []
    AgentIdentityRegistry(persist=different.append).allocate('one', 'Birch')
    registry = AgentIdentityRegistry()
    registry.allocate('existing', 'Elm')
    with pytest.raises(AgentIdentityConflict, match='divergent'):
        registry.hydrate(events + different, quarantine_conflicts=True)
    assert [r['identity'] for r in registry.list()] == ['existing']


def test_quarantine_incremental_replay_preserves_conflicts_and_existing_versions():
    events = _legacy_identity_events()
    registry = AgentIdentityRegistry()
    registry.hydrate(events[:1])
    registry.hydrate(events[1:], quarantine_conflicts=True)
    assert registry.conflict_count == 2
    registry.rename('one', 'Birch', 1)
    registry.hydrate(events, quarantine_conflicts=True)
    assert registry.get('one')['version'] == 2
    assert registry.get('one')['display_name'] == 'Birch'
    assert registry.conflict_count == 1
    with pytest.raises(AgentIdentityConflict):
        registry.rename('one', 'Ada', 2)


def test_quarantine_legacy_duplicate_pairs_preserve_every_identity():
    events = []
    for index in range(46):
        AgentIdentityRegistry(persist=events.append).allocate(
            f'session:{index}', f'Legacy Name {index // 2}'
        )
    registry = AgentIdentityRegistry(persist=lambda event: pytest.fail('replay must not persist'))
    registry.hydrate(events, quarantine_conflicts=True)
    assert registry.conflict_count == 46
    assert {r['identity'] for r in registry.list()} == {f'session:{index}' for index in range(46)}
    assert all(record['version'] == 1 for record in registry.list())


def test_quarantine_rejects_divergent_older_versions_after_newer_event():
    events = []
    original = AgentIdentityRegistry(persist=events.append)
    original.allocate('one', 'Ada')
    original.rename('one', 'Birch', 1)
    AgentIdentityRegistry(persist=events.append).allocate('one', 'Cedar')
    with pytest.raises(AgentIdentityConflict, match='divergent'):
        AgentIdentityRegistry().hydrate(events, quarantine_conflicts=True)
