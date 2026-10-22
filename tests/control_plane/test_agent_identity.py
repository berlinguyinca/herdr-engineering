"""Stable, durable, globally unique agent display names."""
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
