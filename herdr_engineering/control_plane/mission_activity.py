"""Bounded ephemeral Pi mission read model; never publishes lifecycle events.

Pi owns durable mission state. Collection freshness describes the transport,
not worker health: a missing collector never manufactures a stalled mission.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

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

    def __init__(self, *, now=None, max_missions=MAX_MISSIONS):
        self._now = now or time.time
        self._max_missions = max(1, min(MAX_MISSIONS, max_missions))
        self._entries: OrderedDict[str, dict] = OrderedDict()
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
            'mission': _sanitize({k: v for k, v in mission.items() if k in MISSION_FIELDS}),
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
        return {'accepted': True, 'mission_key': mission_key}

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
            value['mission'] = {k: v for k, v in value['mission'].items()
                                if k in ('id', 'title', 'status', 'goal', 'canonicalState')}
        return value

    def get(self, mission_key: str) -> dict | None:
        with self._lock:
            now = self._now()
            self._prune(now)
            entry = self._entries.get(mission_key)
            return self._view(entry, now) if entry else None

    def list_summaries(self) -> list[dict]:
        with self._lock:
            now = self._now()
            self._prune(now)
            return [self._view(entry, now, summary=True) for entry in reversed(self._entries.values())]
