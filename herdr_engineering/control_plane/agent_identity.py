"""Durable human-readable names, independent of provider routing identities.

The callback must acknowledge durable storage before returning; failures propagate
without publishing the reservation. Restore events before allocating new names.
"""
from __future__ import annotations

import copy
import hashlib
import re
import threading
import unicodedata

# A quarantined reservation is unavailable even to its original claimants.
_CONFLICTED = object()

_WORDS = (
    'Amber', 'Birch', 'Cedar', 'Dawn', 'Elm', 'Fern', 'Glen', 'Hazel',
    'Iris', 'Jade', 'Lake', 'Maple', 'North', 'Oak', 'Pine', 'Quartz',
    'River', 'Sage', 'Teal', 'Vale', 'Willow', 'Aster', 'Bay', 'Clover',
    'Dune', 'Ember', 'Flint', 'Grove', 'Heath', 'Juniper', 'Laurel', 'Moss',
)
_QUALITIES = (
    'Bright', 'Calm', 'Clear', 'Clever', 'Gentle', 'Kind', 'Lively', 'Nimble',
    'Patient', 'Quiet', 'Ready', 'Steady', 'Swift', 'Warm', 'Wise', 'Brave',
)


class AgentIdentityConflict(ValueError):
    """The requested name or handle belongs to another agent."""


class AgentIdentityVersionConflict(AgentIdentityConflict):
    """The identity was changed after the caller last read it."""


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError('identity must be a non-empty string of at most 1024 characters')
    if not value.isprintable():
        raise ValueError('identity must contain printable characters')
    return value


def _name(value):
    if not isinstance(value, str) or not value.isprintable():
        raise ValueError('name must contain printable characters')
    name = ' '.join(unicodedata.normalize('NFKC', value).split())
    if not name or len(name) > 80 or not any(c.isalpha() for c in name):
        raise ValueError('name must contain letters and be at most 80 characters')
    if any(not (c.isalnum() or c in " '-.") for c in name):
        raise ValueError('name may contain letters, numbers, spaces, apostrophes, hyphens and periods')
    return name


def _handle(name):
    # Accents and compatibility forms share the same reference handle.
    folded = ''.join(c for c in unicodedata.normalize('NFKD', name.casefold())
                     if not unicodedata.combining(c))
    return re.sub(r'-+', '-', ''.join(c if c.isalnum() else '-' for c in folded)).strip('-')


def _suffix(index):
    words = []
    while True:
        words.append(_WORDS[index % len(_WORDS)])
        index = index // len(_WORDS) - 1
        if index < 0:
            return ' '.join(reversed(words))


class AgentIdentityRegistry:
    """Serialize global name reservations and preserve them across renames.

    Public records contain identity, display_name, handle and optimistic version.
    Persisted payloads additionally retain all historical name/handle reservations
    so an old reference is never reassigned to a different agent.
    """

    def __init__(self, persist=None):
        self._persist = persist
        self._lock = threading.RLock()
        self._records = {}
        self._names = {}
        self._handles = {}

    def get(self, identity):
        with self._lock:
            record = self._records.get(identity)
            return self._public(record) if record else None

    def list(self):
        with self._lock:
            return [self._public(record) for record in self._records.values()]

    def is_conflicted(self, identity):
        with self._lock:
            record = self._records.get(identity)
            return record is not None and not self._available(identity, record['display_name'])

    @property
    def conflict_count(self):
        with self._lock:
            return sum(self.is_conflicted(identity) for identity in self._records)

    @staticmethod
    def _public(record):
        return {key: record[key] for key in ('identity', 'display_name', 'handle', 'version')}

    def _available(self, identity, name):
        return (self._names.get(name.casefold()) in (None, identity)
                and self._handles.get(_handle(name)) in (None, identity))

    def allocate(self, identity, preferred_name=None):
        identity = _identity(identity)
        with self._lock:
            if identity in self._records:
                if self.is_conflicted(identity):
                    raise AgentIdentityConflict(
                        'agent name is ambiguous; explicitly rename to an available name '
                        f'or use stable identity ID {identity!r}'
                    )
                return self._public(self._records[identity])
            try:
                base = _name(preferred_name)
            except ValueError:
                # Provider metadata can be an arbitrary terminal title or path.
                # Explicit operator renames remain strictly validated below.
                seed = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], 'big')
                base = f'{_QUALITIES[seed % len(_QUALITIES)]} {_WORDS[seed // len(_QUALITIES) % len(_WORDS)]}'
            name = base
            index = 0
            while not self._available(identity, name):
                suffix = _suffix(index)
                name = f'{base[:79 - len(suffix)].rstrip()} {suffix}'
                index += 1
            return self._commit(identity, name, 1)

    def rename(self, identity, name, expected_version):
        identity = _identity(identity)
        name = _name(name)
        with self._lock:
            previous = self._records.get(identity)
            if previous is None:
                raise KeyError(identity)
            if type(expected_version) is not int or expected_version != previous['version']:
                raise AgentIdentityVersionConflict('agent name has changed; refresh before renaming')
            if not self._available(identity, name):
                raise AgentIdentityConflict('agent name is already reserved')
            if previous['display_name'] == name:
                return self._public(previous)
            return self._commit(identity, name, expected_version + 1)

    def _commit(self, identity, name, version):
        previous = self._records.get(identity, {})
        handle = _handle(name)
        record = {'identity': identity, 'display_name': name, 'handle': handle, 'version': version,
                  'reserved_names': sorted(set(previous.get('reserved_names', [])) | {name.casefold()}),
                  'reserved_handles': sorted(set(previous.get('reserved_handles', [])) | {handle})}
        event = {'entity_type': 'agent_identity', 'entity_id': identity,
                 'event_type': 'agent_identity.updated', 'payload': copy.deepcopy(record)}
        if self._persist is not None and self._persist(event) is False:
            raise RuntimeError('agent identity persistence was not acknowledged')
        self._publish(record)
        return self._public(record)

    def _publish(self, record):
        identity = record['identity']
        for reservations, aliases in ((self._names, record['reserved_names']),
                                      (self._handles, record['reserved_handles'])):
            for alias in aliases:
                owner = reservations.get(alias)
                # Cumulative aliases on a rename must never steal a reservation
                # from another identity or replace a previously blocked alias.
                reservations[alias] = identity if owner in (None, identity) else _CONFLICTED
        self._records[identity] = record

    def _hydrate_quarantined(self, events):
        grouped = {}
        for event in events:
            if (event.get('entity_type') != 'agent_identity'
                    or event.get('event_type') != 'agent_identity.updated'):
                continue
            # Validate every envelope, including malformed duplicate/older
            # versions that an ordinary replay would otherwise ignore.
            validator = AgentIdentityRegistry()
            validator.hydrate([event])
            identity = event['payload']['identity']
            grouped.setdefault(identity, []).append(event)

        validated = []
        for identity, history in grouped.items():
            previous = self._records.get(identity)
            if previous is not None:
                history.append({'entity_type': 'agent_identity', 'entity_id': identity,
                                'event_type': 'agent_identity.updated', 'payload': previous})
            isolated = AgentIdentityRegistry()
            # Ascending versions also expose divergent older versions; global
            # collisions are the only validation failures quarantine tolerates.
            isolated.hydrate(sorted(history, key=lambda event: event['payload']['version']))
            validated.append(isolated._records[identity])
        # Publish only after every identity has passed strict validation. The
        # cumulative reservations reconstruct blocked aliases from snapshots.
        for record in validated:
            self._publish(record)

    def hydrate(self, events, *, quarantine_conflicts=False):
        """Replay ordered durable envelopes without generating new events.

        Duplicate/older versions are ignored. Conflicting or malformed identity
        events fail closed rather than silently permitting duplicate references.
        Opt-in quarantine retains validated identities but blocks ambiguous
        reservations until operators explicitly rename the affected identities.
        Historical ambiguous aliases remain blocked after those renames.
        """
        with self._lock:
            if quarantine_conflicts:
                self._hydrate_quarantined(events)
                return
            for event in events:
                if event.get('entity_type') != 'agent_identity':
                    continue
                if event.get('event_type') != 'agent_identity.updated':
                    continue
                record = copy.deepcopy(event['payload'])
                identity = _identity(record['identity'])
                name = _name(record['display_name'])
                if (identity != event.get('entity_id') or name != record['display_name']
                        or record.get('handle') != _handle(name)
                        or type(record.get('version')) is not int or record['version'] < 1):
                    raise ValueError('invalid persisted agent identity')
                previous = self._records.get(identity)
                if previous and previous['version'] == record['version']:
                    if self._public(previous) != self._public(record):
                        raise AgentIdentityConflict('divergent persisted agent identity version')
                    continue
                if previous and previous['version'] > record['version']:
                    continue
                names = record.setdefault('reserved_names', [name.casefold()])
                handles = record.setdefault('reserved_handles', [_handle(name)])
                if (not isinstance(names, list) or not isinstance(handles, list)
                        or any(not isinstance(item, str) or not item for item in names + handles)
                        or name.casefold() not in names or _handle(name) not in handles):
                    raise ValueError('invalid persisted agent reservations')
                if any(self._names.get(item) not in (None, identity) for item in names):
                    raise AgentIdentityConflict('persisted agent name is already reserved')
                if any(self._handles.get(item) not in (None, identity) for item in handles):
                    raise AgentIdentityConflict('persisted agent handle is already reserved')
                self._publish(record)
