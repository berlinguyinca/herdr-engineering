"""Bounded ephemeral Pi mission read model; never publishes lifecycle events.

Pi owns durable mission state. Collection freshness describes the transport,
not worker health: a missing collector never manufactures a stalled mission.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from .envelope import (
    _checkpoint_header,
    _checkpoint_int,
    _checkpoint_json,
    _checkpoint_object,
    _checkpoint_require,
    _checkpoint_stamp,
    _checkpoint_time,
)
from .redact import redact_text

MAX_PAYLOAD_BYTES = 512 * 1024
MAX_PROJECTION_BYTES = 128 * 1024
MAX_MISSIONS = 128
RETENTION_SECONDS = 24 * 60 * 60
STALE_SECONDS = 30
DISCONNECT_SECONDS = 120
_ID = re.compile(r'^[A-Za-z0-9_.:@-]{1,160}$')
_MISSION_KEY = re.compile(r'^pi:[A-Za-z0-9_.-]{1,128}$')
_PRIVATE = re.compile(r'reasoning|thinking|chain.?of.?thought|analysis|prompt|messages?', re.I)
_SECRET = re.compile(r'password|passwd|secret|token|api.?key|authorization|credential|private.?key|dsn', re.I)
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]')
_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
SUMMARY_FIELDS = frozenset((
    'progress', 'acceptanceCoverage', 'workflowProgress', 'health', 'currentObjective',
    'currentActivity', 'currentActivityStartedAt', 'workers', 'lastHeartbeatAt',
    'lastOutputAt', 'lastMeaningfulProgressAt', 'waitingReason', 'waitingSince',
    'completionStatus', 'action', 'reason', 'nextAction', 'nextActionAt', 'owner',
    'repository', 'task', 'recoveryAttempt',
))
ACTIVITY_FIELDS = SUMMARY_FIELDS | {
    'workerDetails', 'progressHistory', 'tests', 'review', 'activity', 'errors',
    'recovery', 'changes', 'artifacts', 'preservedWork', 'tasks', 'waits', 'timestamps',
}
MISSION_FIELDS = frozenset((
    'id', 'title', 'status', 'goal', 'revision', 'workflowClass', 'canonicalState',
    'tasks', 'acceptanceCriteria', 'stop',
))
# Snapshot contract v4 (additive): validated separately by clean_pi_fields, never by the generic sanitizer.
PI_V4_FIELDS = ('created_at', 'completed_at', 'links')
SUMMARY_MISSION_FIELDS = ('id', 'title', 'status', 'goal', 'canonicalState', 'created_at', 'completed_at')
MAX_LINKS = 20
# G8-C durable mirror: a private stream, one bounded row per accepted snapshot (throttled per mission).
ENTITY_TYPE = 'mission_activity_store'
STORED_EVENT = 'MissionActivityStored'
PERSIST_INTERVAL_SECONDS = 30
log = logging.getLogger(__name__)
LINK_KINDS = frozenset(('issue', 'pull_request'))
_REPOSITORY = re.compile(r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
MAX_LINK_NUMBER = 2 ** 31 - 1
WORKER_SUMMARY_FIELDS = frozenset((
    'workerId', 'taskId', 'health', 'state', 'model', 'route', 'currentActivity',
    'lastOutputAt', 'lastMeaningfulProgressAt', 'lastHeartbeatAt',
))


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _timestamp(value: Any, name: str, now: float) -> float:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f'{name} must be an ISO timestamp')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        epoch = dt.timestamp()
        if dt.tzinfo is None or not math.isfinite(epoch) or epoch > now + 60:
            raise ValueError()
        return epoch
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f'{name} must be a timezone-qualified timestamp, not in the future') from exc


def _pi_time(value: Any, now: float) -> str | None:
    try:
        return iso(_timestamp(value, 'time', now))
    except ValueError:
        return None


def _pi_link(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind, repository, number = raw.get('kind'), raw.get('repository'), raw.get('number')
    if (kind not in LINK_KINDS or not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository)
            or repository.split('/')[0] in ('.', '..') or repository.split('/')[1] in ('.', '..')
            or type(number) is not int or not 1 <= number <= MAX_LINK_NUMBER):
        return None
    return {'kind': kind, 'repository': repository, 'number': number}


def clean_pi_fields(mission: Any, *, now: float) -> dict:
    """Validate the contract-v4 mission fields. Malformed or absent entries are dropped, never repaired or inferred."""
    if not isinstance(mission, dict):
        return {}
    out: dict = {}
    for name in ('created_at', 'completed_at'):
        value = _pi_time(mission.get(name), now)
        if value is not None:
            out[name] = value
    raw = mission.get('links')
    if isinstance(raw, list):
        links, seen = [], set()
        for item in raw[:MAX_LINKS * 4]:
            link = _pi_link(item)
            if link is None:
                continue
            identity = (link['kind'], link['repository'].lower(), link['number'])
            if identity in seen:
                continue
            seen.add(identity)
            links.append(link)
            if len(links) >= MAX_LINKS:
                break
        if links:
            out['links'] = links
    return out


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f'invalid {name}')
    return value


def _sanitize(value: Any, *, depth=0, budget=None) -> Any:
    """Bound and redact before storage, with no private-reasoning surface."""
    budget = [6000] if budget is None else budget
    budget[0] -= 1
    if depth > 8 or budget[0] < 0:
        return None
    if isinstance(value, str):
        # Redact before truncation so a boundary cannot split a secret marker.
        return redact_text(_CONTROL.sub('', _ANSI.sub('', value)))[:4096]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if abs(value) <= 2 ** 53 and math.isfinite(value) else None
    if isinstance(value, list):
        return [_sanitize(v, depth=depth + 1, budget=budget) for v in value[:100]
                if not (isinstance(v, dict) and any(
                    _PRIVATE.search(str(v.get(k, ''))) for k in ('type', 'kind')))]
    if isinstance(value, dict):
        if any(_PRIVATE.search(str(value.get(k, ''))) for k in ('type', 'kind')):
            return None
        result = {}
        for key, val in list(value.items())[:100]:
            if not isinstance(key, str) or len(key) > 80 or any(ord(ch) < 32 for ch in key) or _CONTROL.search(key) or _PRIVATE.search(key):
                continue
            result[key] = ('[REDACTED]' if _SECRET.search(key) else
                           _sanitize(val, depth=depth + 1, budget=budget))
        return result
    return None


class MissionActivityStore:
    """Latest read-only projection per namespaced mission, under a fixed memory cap."""

    def __init__(self, *, now=None, max_missions=MAX_MISSIONS, persist=None):
        self._now = now or time.time
        self._persist = persist           # callable(event dict) or None; best-effort, never breaks ingest
        self._persisted: dict[str, tuple[float, str]] = {}   # key -> (epoch, connection) of the last mirrored row
        self._max_missions = max(1, min(MAX_MISSIONS, max_missions))
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.RLock()

    def export_checkpoint(self) -> dict:
        """Capture the transient read model, not a new durable mission payload."""
        with self._lock:
            return _checkpoint_json({"version": 1, "domain": "mission_activity",
                                     "transient": True,
                                     "entries": [[key, entry] for key, entry in self._entries.items()]})

    def restore_checkpoint(self, state) -> None:
        state = _checkpoint_header(state, "mission_activity", ("transient", "entries"))
        _checkpoint_require(state["transient"] is True and type(state["entries"]) is list
                            and len(state["entries"]) <= MAX_MISSIONS)
        entries = OrderedDict()
        fields = ("contract_version", "mission_key", "source", "host_id", "instance_id", "session_id",
                  "observed_at", "snapshot_at", "received_at", "mission_revision", "connection",
                  "activity_revision", "activity_snapshot_at", "mission", "activity")
        for row in state["entries"]:
            _checkpoint_require(type(row) is list and len(row) == 2)
            key, entry = row
            _checkpoint_require(type(key) is str and _MISSION_KEY.fullmatch(key) is not None
                                and key not in entries)
            _checkpoint_object(entry, ("value", "received_epoch", "observed_epoch", "snapshot_epoch"))
            _checkpoint_time(entry["received_epoch"])
            _checkpoint_time(entry["observed_epoch"])
            if entry["snapshot_epoch"] is not None:
                _checkpoint_time(entry["snapshot_epoch"])
            value = entry["value"]
            _checkpoint_object(value, fields)
            _checkpoint_require(type(value["contract_version"]) is int and value["contract_version"] == 1
                                and value["mission_key"] == key and value["source"] == "pi"
                                and value["connection"] in ("connected", "disconnected", "updating"))
            for field in ("host_id", "instance_id", "session_id"):
                _identifier(value[field], field)
            _checkpoint_int(value["mission_revision"])
            _checkpoint_require(value["mission_revision"] <= 2 ** 53)
            if value["activity_revision"] is not None:
                _checkpoint_int(value["activity_revision"])
                _checkpoint_require(value["activity_revision"] <= value["mission_revision"])
            for field, epoch in (("received_at", "received_epoch"), ("observed_at", "observed_epoch"),
                                 ("snapshot_at", "snapshot_epoch")):
                if value[field] is None:
                    _checkpoint_require(entry[epoch] is None)
                else:
                    _checkpoint_stamp(value[field])
                    _checkpoint_require(abs(datetime.fromisoformat(value[field]).timestamp() - entry[epoch]) <= 1e-6)
            if value["activity_snapshot_at"] is not None:
                _checkpoint_stamp(value["activity_snapshot_at"])
            _checkpoint_require(type(value["mission"]) is dict and type(value["activity"]) is dict
                                and value["mission"].get("id") == key[3:]
                                and value["mission"].keys() <= MISSION_FIELDS
                                and value["activity"].keys() <= ACTIVITY_FIELDS)
            # The saved value has already crossed the sanitizing boundary; do not
            # ingest it again or manufacture a new receive timestamp.
            _checkpoint_require(_sanitize(value["mission"]) == value["mission"]
                                and _sanitize(value["activity"]) == value["activity"]
                                and len(json.dumps(value).encode()) <= MAX_PROJECTION_BYTES)
            entries[key] = entry
        now = self._now()
        entries = OrderedDict((key, value) for key, value in entries.items()
                              if now - value["received_epoch"] <= RETENTION_SECONDS)
        while len(entries) > self._max_missions:
            entries.popitem(last=False)
        with self._lock:
            self._entries = entries
            self._lock = threading.RLock()

    def _prune(self, now):
        for key in list(self._entries):
            if now - self._entries[key]['received_epoch'] > RETENTION_SECONDS:
                del self._entries[key]

    def ingest(self, mission_key: str, payload: dict, *, source: str) -> dict:
        if source != 'pi' or not _MISSION_KEY.fullmatch(mission_key):
            raise ValueError('mission activity requires an explicit pi mission key and source')
        try:
            size = len(json.dumps(payload, allow_nan=False).encode())
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError('invalid mission activity payload') from exc
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError('mission activity payload is too large')
        if type(payload.get('contract_version')) is not int or payload['contract_version'] != 1:
            raise ValueError('unsupported mission activity contract_version')
        identity = tuple(_identifier(payload.get(k), k) for k in ('host_id', 'instance_id', 'session_id'))
        revision = payload.get('mission_revision')
        if type(revision) is not int or not 0 <= revision <= 2 ** 53:
            raise ValueError('invalid mission_revision')
        connection = payload.get('connection')
        if connection not in ('connected', 'disconnected', 'updating'):
            raise ValueError('invalid connection')
        mission, activity = payload.get('mission'), payload.get('activity')
        if not isinstance(mission, dict) or mission.get('id') != mission_key[3:]:
            raise ValueError('mission.id does not match mission key')
        if not isinstance(activity, dict):
            raise ValueError('activity must be an object')
        if 'revision' in mission and mission['revision'] != revision:
            raise ValueError('mission revision does not match payload')
        now = self._now()
        observed = _timestamp(payload.get('observed_at'), 'observed_at', now)
        snapshot = (None if payload.get('snapshot_at') is None and connection != 'connected'
                    else _timestamp(payload.get('snapshot_at'), 'snapshot_at', now))
        if observed < now - DISCONNECT_SECONDS:
            return {'accepted': False, 'reason': 'observation_too_old'}
        if snapshot is not None and snapshot > observed + 60:
            raise ValueError('snapshot_at is newer than observation')
        clean = {
            'contract_version': 1, 'mission_key': mission_key, 'source': 'pi',
            'host_id': identity[0], 'instance_id': identity[1], 'session_id': identity[2],
            'observed_at': iso(observed), 'snapshot_at': iso(snapshot) if snapshot is not None else None,
            'received_at': iso(now), 'mission_revision': revision, 'connection': connection,
            'activity_revision': revision if connection == 'connected' else None,
            'activity_snapshot_at': iso(snapshot) if connection == 'connected' else None,
            'mission': {**_sanitize({k: v for k, v in mission.items() if k in MISSION_FIELDS}),
                        **clean_pi_fields(mission, now=now)},
            'activity': (_sanitize({k: v for k, v in activity.items() if k in ACTIVITY_FIELDS})
                         if connection == 'connected' else {}),
        }
        if 'workerDetails' in clean['activity']:
            workers = clean['activity']['workerDetails']
            clean['activity']['workerDetails'] = (
                [worker for worker in workers if isinstance(worker, dict)]
                if isinstance(workers, list) else [])
        with self._lock:
            self._prune(now)
            previous = self._entries.get(mission_key)
            if previous:
                old = previous['value']
                if revision < old['mission_revision'] or observed <= previous['observed_epoch']:
                    return {'accepted': False, 'reason': 'out_of_order'}
                old_identity = tuple(old[k] for k in ('host_id', 'instance_id', 'session_id'))
                if (identity != old_identity and old['connection'] != 'disconnected'
                        and now - previous['received_epoch'] < DISCONNECT_SECONDS):
                    return {'accepted': False, 'reason': 'owner_conflict'}
                if (snapshot is not None and previous['snapshot_epoch'] is not None
                        and snapshot < previous['snapshot_epoch'] and connection == 'connected'):
                    return {'accepted': False, 'reason': 'snapshot_regression'}
                if connection != 'connected' and identity == old_identity:
                    clean['activity'] = copy.deepcopy(old['activity'])
                    clean['activity_revision'] = old['activity_revision']
                    clean['activity_snapshot_at'] = old['activity_snapshot_at']
                    clean['snapshot_at'] = old['snapshot_at']
                    snapshot = previous['snapshot_epoch']
            if len(json.dumps(clean).encode()) > MAX_PROJECTION_BYTES:
                raise ValueError('mission activity projection is too large')
            self._entries[mission_key] = {'value': clean, 'received_epoch': now,
                                         'observed_epoch': observed, 'snapshot_epoch': snapshot}
            self._entries.move_to_end(mission_key)
            while len(self._entries) > self._max_missions:
                self._entries.popitem(last=False)
            row = self._storable(mission_key, self._entries[mission_key], now)
        if row is not None:
            try:
                self._persist(row)
            except Exception:  # noqa: BLE001 - the mirror is best-effort; the in-memory projection already holds the snapshot
                log.warning('mission activity persistence failed for %s', mission_key, exc_info=True)
        return {'accepted': True, 'mission_key': mission_key}

    def _storable(self, key, entry, now):
        """The event to mirror for this accepted snapshot, or None (no sink, or throttled). Caller holds the lock."""
        if self._persist is None:
            return None
        connection = entry['value']['connection']
        last = self._persisted.get(key)
        if last is not None and last[1] == connection and now - last[0] < PERSIST_INTERVAL_SECONDS:
            return None
        self._persisted[key] = (now, connection)
        while len(self._persisted) > 4 * self._max_missions:
            self._persisted.pop(next(iter(self._persisted)))
        return {'entity_type': ENTITY_TYPE, 'entity_id': key, 'event_type': STORED_EVENT,
                'payload': {'value': copy.deepcopy(entry['value']), 'received_epoch': entry['received_epoch'],
                            'observed_epoch': entry['observed_epoch'], 'snapshot_epoch': entry['snapshot_epoch']}}

    def hydrate(self, rows) -> dict:
        """Rebuild the projection from mirrored rows after a restart. Newest row per mission wins; rows that are expired,
        corrupt, of another kind or not shaped like what this store writes are skipped, never fatal. Returns counts."""
        now = self._now()
        best: dict[str, tuple[float, dict]] = {}
        skipped = 0
        for row in rows:
            entry = self._restore(row, now)
            if entry is None:
                skipped += 1
                continue
            key = row['entity_id']
            if key not in best or entry['received_epoch'] >= best[key][0]:
                best[key] = (entry['received_epoch'], entry)
        with self._lock:
            self._entries.clear()
            self._persisted.clear()
            for key, (_, entry) in sorted(best.items(), key=lambda item: item[1][0])[-self._max_missions:]:
                self._entries[key] = entry
                self._persisted[key] = (entry['received_epoch'], entry['value']['connection'])
        return {'loaded': len(self._entries), 'skipped': skipped}

    @staticmethod
    def _restore(row, now):
        try:
            if row.get('entity_type') != ENTITY_TYPE or row.get('event_type') != STORED_EVENT:
                return None
            key, payload = row.get('entity_id'), row.get('payload')
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(key, str) or not _MISSION_KEY.fullmatch(key) or not isinstance(payload, dict):
                return None
            value = payload['value']
            if (not isinstance(value, dict) or value.get('mission_key') != key or value.get('source') != 'pi'
                    or value.get('contract_version') != 1
                    or value.get('connection') not in ('connected', 'disconnected', 'updating')
                    or not isinstance(value.get('mission'), dict) or value['mission'].get('id') != key[3:]
                    or not isinstance(value.get('activity'), dict)
                    or type(value.get('mission_revision')) is not int
                    or len(json.dumps(value, allow_nan=False).encode()) > MAX_PROJECTION_BYTES):
                return None
            epochs = {}
            for name in ('received_epoch', 'observed_epoch', 'snapshot_epoch'):
                number = payload.get(name)
                if number is None and name == 'snapshot_epoch':
                    epochs[name] = None
                    continue
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                    return None
                epochs[name] = float(number)
            if now - epochs['received_epoch'] > RETENTION_SECONDS or epochs['received_epoch'] > now + 60:
                return None
            value['mission'] = {**{k: v for k, v in value['mission'].items() if k not in PI_V4_FIELDS},
                                **clean_pi_fields(value['mission'], now=now)}
            return {'value': value, **epochs}
        except (KeyError, TypeError, ValueError, RecursionError, AttributeError):
            return None

    def _view(self, entry, now, *, summary=False):
        value = copy.deepcopy(entry['value'])
        age = max(0, now - entry['received_epoch'], now - entry['observed_epoch'])
        state = ('disconnected' if value['connection'] == 'disconnected' or age >= DISCONNECT_SECONDS
                 else 'stale' if age >= STALE_SECONDS
                 else 'updating' if value['connection'] == 'updating' else 'live')
        value['freshness'] = {'state': state, 'age_seconds': round(age, 1),
                              'stale_after_seconds': STALE_SECONDS,
                              'disconnect_after_seconds': DISCONNECT_SECONDS}
        if summary:
            activity = value['activity']
            value['activity'] = {k: v for k, v in activity.items() if k in SUMMARY_FIELDS}
            value['activity']['workerDetails'] = [
                {k: v for k, v in worker.items() if k in WORKER_SUMMARY_FIELDS}
                for worker in activity.get('workerDetails', []) if isinstance(worker, dict)
            ]
            value['mission'] = {k: v for k, v in value['mission'].items() if k in SUMMARY_MISSION_FIELDS}
        return value

    def get(self, mission_key: str) -> dict | None:
        with self._lock:
            now = self._now()
            self._prune(now)
            entry = self._entries.get(mission_key)
            return self._view(entry, now) if entry else None

    def links_by_mission(self) -> dict[str, list[dict]]:
        """{mission key: Pi-reported links} for retained snapshots that carry any; copies, newest data only."""
        with self._lock:
            self._prune(self._now())
            return {key: copy.deepcopy(entry['value']['mission']['links'])
                    for key, entry in self._entries.items() if entry['value']['mission'].get('links')}

    def list_summaries(self) -> list[dict]:
        with self._lock:
            now = self._now()
            self._prune(now)
            return [self._view(entry, now, summary=True) for entry in reversed(self._entries.values())]
