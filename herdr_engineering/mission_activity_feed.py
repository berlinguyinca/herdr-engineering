"""Bounded, read-only native Pi telemetry bridge. Never emits lifecycle events.

Only private same-user descriptors, authenticated Unix peers and runtime-owned
mission revisions may supply activity. Polling does not touch or reload Pi.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .control_plane.redact import redact_text

MAX_SESSIONS = 64
MAX_MISSIONS = 20
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 65536
# A positive field allowlist excludes raw arguments, prompts and hidden reasoning
# even if a future Pi snapshot starts publishing additional fields.
FIELDS = set('''progress approximatePercent verifiedComplete basis acceptanceCoverage workflowProgress
health currentObjective currentActivity type summary workerId file workers active waiting failed
lastHeartbeatAt lastMeaningfulProgressAt lastOutputAt currentActivityStartedAt waitingReason waitingSince
completionStatus progressHistory at label meaningfulProgress tests running completed total passed skipped
failures suite lastUpdatedAt retries review status blockingOpen findings id findingId severity repaired
workerDetails taskId state model runtime host route node fallbacks inferenceWaitMs startedAt endedAt
lastOutputPreview tokensPerSec repeatedFileReads repeatedToolCalls repeatedErrors repeatedCycles
noProgressSince activity errors example count category code firstAt lastAt recovery action reason
attempt maxAttempts nextAction nextActionAt recoveryAttempt changes branch changedFiles commits
integrationState artifacts task title weight units completedAt assignedWorkerId dependsOn tasks
provider alias backingModel queuePosition queued admitted capacity retryAt retryInMs waitMs
'''.split())


# Pi's public ActivityType contract. New types require an explicit review before
# crossing the wire; an arbitrary typed snapshot record is not public telemetry.
ACTIVITY_TYPES = frozenset("""planning reading_file editing_file creating_file deleting_file
running_command running_test building linting typechecking waiting_for_model waiting_for_queue
waiting_for_worker waiting_for_dependency model_request worker_started worker_completed
tool_invocation artifact_created git_diff git_commit integration review_started review_finding
review_completed repair_started repair_completed validation retry error recovery""".split())


def _public_activity(value) -> bool:
    return (isinstance(value, dict) and isinstance(value.get('type'), str)
            and value['type'] in ACTIVITY_TYPES
            and ('kind' not in value or value['kind'] == value['type']))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def control_root() -> Path:
    if os.environ.get('PI_ENGINEERING_CONTROL_DIR'):
        return Path(os.environ['PI_ENGINEERING_CONTROL_DIR']).absolute()
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime or tempfile.gettempdir()) / ('pi-engineering' if runtime else f'pi-engineering-{os.getuid()}')


def _private(path: Path, kind: str, mode: int | None = None):
    info = path.lstat()
    correct = stat.S_ISDIR(info.st_mode) if kind == 'directory' else stat.S_ISSOCK(info.st_mode)
    if not correct or info.st_uid != os.getuid() or info.st_mode & (0o022 if mode is not None else 0o002):
        raise ValueError('unsafe native path')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError('unsafe native permissions')
    return info


def _read_json(path: Path, limit: int, private: bool = False) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (0o022 if private else 0o002):
            raise ValueError('unsafe native file')
        if private and stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('descriptor is not private')
        if info.st_size > limit:
            raise ValueError('native file too large')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('native file too large')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('invalid native document')
    return data


def discover(root: Path) -> list[dict]:
    """Read at most 64 private descriptors; no subprocesses or project scanning."""
    _private(root, 'directory', 0o700)
    descriptors = []
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 1024 or len(descriptors) >= MAX_SESSIONS:
                break
            if not re.fullmatch(r'[0-9a-f-]{36}\.json', entry.name):
                continue
            try:
                d = _read_json(Path(entry.path), 4096, private=True)
                instance = entry.name[:-5]
                if (d.get('version') != 1 or d.get('instanceId') != instance
                        or d.get('socketPath') != str(root / f'{instance}.sock')
                        or not isinstance(d.get('sessionId'), str) or not d['sessionId']
                        or not isinstance(d.get('cwd'), str) or not Path(d['cwd']).is_absolute()
                        or type(d.get('pid')) is not int or d['pid'] <= 0):
                    continue
                _private(Path(d['socketPath']), 'socket', 0o600)
                descriptors.append(d)
            except (OSError, ValueError, TypeError):
                continue
    return descriptors


def _verify_peer(client: socket.socket, expected_pid: int) -> None:
    if hasattr(socket, 'SO_PEERCRED'):
        pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    elif sys.platform == 'darwin':
        # Darwin exposes getpeereid(2) in libc even when Python omits its wrapper.
        # SDK unistd.h uses uint32_t uid_t/gid_t; sys/un.h defines LOCAL_PEERPID=2.
        # Do not decode platform-dependent xucred structs or trust descriptor UID.
        getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        getpeereid.restype = ctypes.c_int
        uid_value, gid_value = ctypes.c_uint32(), ctypes.c_uint32()
        if getpeereid(client.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, 'native peer credentials unavailable')
        uid = uid_value.value
        pid = client.getsockopt(0, 2)
    else:
        raise ValueError('native peer proof unavailable')
    if uid != os.getuid() or pid != expected_pid:
        raise ValueError('native peer identity mismatch')


def _request(descriptor: dict, request: dict, timeout: float) -> dict:
    path = Path(descriptor['socketPath'])
    _private(path.parent, 'directory', 0o700)
    _private(path, 'socket', 0o600)
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        _verify_peer(client, descriptor['pid'])
        client.sendall(json.dumps(request).encode() + b'\n')
        body = bytearray()
        while b'\n' not in body:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('native session timeout')
            client.settimeout(remaining)
            chunk = client.recv(min(8192, MAX_REPLY_BYTES + 1 - len(body)))
            if not chunk:
                raise ValueError('incomplete native reply')
            body.extend(chunk)
            if len(body) > MAX_REPLY_BYTES:
                raise ValueError('native reply too large')
    reply = json.loads(body.split(b'\n', 1)[0])
    if not isinstance(reply, dict) or reply.get('version') != 1 or reply.get('ok') is not True:
        raise ValueError('native request rejected')
    data = reply.get('data')
    if not isinstance(data, dict) or any(data.get(key) != descriptor[key] for key in ('instanceId', 'sessionId', 'pid', 'cwd')):
        raise ValueError('native session identity mismatch')
    return data


def session_status(descriptor: dict, timeout: float) -> dict:
    nonce = str(uuid.uuid4())
    ping = _request(descriptor, {'version': 1, 'op': 'ping', 'nonce': nonce}, timeout)
    if ping.get('nonce') != nonce:
        raise ValueError('native nonce mismatch')
    return _request(descriptor, {'version': 1, 'op': 'status'}, timeout)


def _sanitize(value, depth=0, field=''):
    if depth > 8:
        return None
    if isinstance(value, str):
        # Redact BEFORE truncation so secrets straddling the boundary are covered.
        value = redact_text(value)
        value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
        return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)[:1024]
    if isinstance(value, dict):
        if field in ('activity', 'currentActivity') and not _public_activity(value):
            return None
        return {key: _sanitize(item, depth + 1, key) for key, item in value.items() if key in FIELDS}
    if isinstance(value, list):
        return [_sanitize(item, depth + 1, field) for item in value[-40:]
                if field != 'activity' or _public_activity(item)]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and abs(value) < 1e20:
        return value
    return None


def _snapshot(cwd: str) -> dict:
    directory = Path(cwd) / '.pi-eng'
    _private(directory, 'directory')
    snapshot = _read_json(directory / 'orchestration-snapshot.json', MAX_SNAPSHOT_BYTES)
    if snapshot.get('contractVersion') != 3:
        raise ValueError('unsupported native snapshot')
    return snapshot


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the collector key to an unconfigured origin.
        return None


class PublicationRejected(ValueError):
    """Only fixed reason codes may enter operator summaries, never reply text."""

    def __init__(self, reason):
        self.reason = reason if isinstance(reason, str) and reason in {
            'out_of_order', 'observation_too_old', 'owner_conflict',
            'snapshot_regression',
        } else 'invalid_acknowledgement'
        super().__init__(self.reason)


def _record_error(result: dict, error: Exception, fallback: str) -> None:
    reason = error.reason if isinstance(error, PublicationRejected) else fallback
    result['errors'] += 1
    result['reasons'][reason] = result['reasons'].get(reason, 0) + 1


class MissionActivityFeed:
    def __init__(self, url: str, *, host_id: str, control_dir: Path | None = None,
                 api_key: str = '', timeout: float = 1.0, heartbeat: float = 15.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('control-plane must be an HTTP(S) URL without credentials/query')
        self.url = url.rstrip('/')
        self.host_id = host_id
        self.root = (control_dir or control_root()).absolute()
        self.api_key = api_key
        self.timeout = max(0.1, min(2.0, timeout))
        self.heartbeat = max(5.0, heartbeat)
        self.known: dict[str, dict] = {}
        self.sent: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._http = urllib.request.build_opener(_NoRedirect())

    def _http_json(self, route: str, body: dict | None = None) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-API-Key'] = self.api_key
        raw = json.dumps(body, allow_nan=False).encode() if body is not None else None
        if raw is not None and len(raw) > 256 * 1024:
            raise ValueError('activity payload too large')
        request = urllib.request.Request(self.url + route, data=raw, headers=headers)
        with self._http.open(request, timeout=3) as response:
            data = response.read(MAX_REPLY_BYTES + 1)
        if len(data) > MAX_REPLY_BYTES:
            raise ValueError('control-plane reply too large')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('invalid control-plane reply')
        return result

    def _collect(self, descriptor: dict) -> list[dict]:
        status = session_status(descriptor, self.timeout)
        if status.get('missionSource') != 'runtime':
            return []
        ids = status.get('missionIds')
        briefs = status.get('missions')
        if not isinstance(ids, list) or not isinstance(briefs, list):
            return []
        owned = {value for value in ids[:MAX_MISSIONS] if isinstance(value, str)}
        try:
            snapshot = _snapshot(descriptor['cwd'])
        except (OSError, ValueError, TypeError):
            snapshot = {}
        missions = snapshot.get('missions', [])
        if not isinstance(missions, list):
            missions = []
        found = []
        for brief in briefs[:MAX_MISSIONS]:
            if not isinstance(brief, dict):
                continue
            mid, revision = brief.get('id'), brief.get('revision')
            if not isinstance(mid, str) or not re.fullmatch(r'MSN-[A-Za-z0-9_-]{1,128}', mid) or mid not in owned or type(revision) is not int or revision < 0:
                continue
            matches = [m for m in missions[:200] if isinstance(m, dict) and m.get('id') == mid]
            match = matches[0] if len(matches) == 1 else {}
            valid = (type(match.get('revision')) is int and match['revision'] == revision
                     and isinstance(match.get('observability'), dict)
                     and isinstance(snapshot.get('generatedAt'), str))
            payload = {
                'contract_version': 1, 'host_id': self.host_id,
                'instance_id': descriptor['instanceId'], 'session_id': descriptor['sessionId'],
                'observed_at': _now(), 'snapshot_at': snapshot['generatedAt'] if valid else None,
                'mission_revision': revision, 'connection': 'connected' if valid else 'updating',
                'mission': {'id': mid, 'title': _sanitize(match.get('title', '')) if valid else '',
                            'status': _sanitize(brief.get('status', 'unknown'))},
                'activity': _sanitize(match['observability']) if valid else {},
            }
            found.append(payload)
        return found

    def _publish(self, payload: dict) -> bool:
        mid = payload['mission']['id']
        fingerprint = hashlib.sha256(json.dumps({key: value for key, value in payload.items() if key != 'observed_at'}, sort_keys=True).encode()).hexdigest()
        prior = self.sent.get(mid)
        if prior and prior[0] == fingerprint and time.monotonic() - prior[1] < self.heartbeat:
            return False
        payload = {**payload, 'observed_at': _now()}
        body = {'source': 'pi', 'entity_type': 'mission_activity', 'entity_id': f'pi:{mid}',
                'event_type': 'mission_activity.snapshot', 'payload': payload}
        response = self._http_json('/api/v1/ingest', body)
        entity = response.get('entity')
        if response.get('ok') is not True or not isinstance(entity, dict) or entity.get('accepted') is not True:
            raise PublicationRejected(entity.get('reason') if isinstance(entity, dict) else None)
        self.sent[mid] = (fingerprint, time.monotonic())
        self.known[mid] = payload
        while len(self.known) > 256:
            oldest = next(iter(self.known))
            del self.known[oldest]
            self.sent.pop(oldest, None)
        return True

    def run_once(self) -> dict:
        result = {'capable': False, 'sessions': 0, 'posted': 0, 'errors': 0, 'reasons': {}}
        if not self._lock.acquire(blocking=False):
            return {**result, 'busy': True}
        try:
            try:
                capabilities = self._http_json('/api/v1/health').get('capabilities', [])
                if 'mission_activity_v1' not in capabilities:
                    return result
                result['capable'] = True
            except (OSError, ValueError, TypeError) as error:
                _record_error(result, error, 'capability_unavailable')
                return result
            try:
                descriptors = discover(self.root)
            except (OSError, ValueError):
                descriptors = []
            result['sessions'] = len(descriptors)
            candidates: dict[str, list[dict]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(self._collect, descriptor) for descriptor in descriptors]
                for future in futures:
                    try:
                        for payload in future.result():
                            candidates.setdefault(payload['mission']['id'], []).append(payload)
                    except (OSError, ValueError, TypeError) as error:
                        _record_error(result, error, 'native_unavailable')
            # Multiple live runtime owners are ambiguous; never guess which wins.
            current = {mid: values[0] for mid, values in candidates.items() if len(values) == 1}
            for mid, previous in list(self.known.items()):
                new = current.get(mid)
                owner_changed = new and any(new[key] != previous[key] for key in ('host_id', 'instance_id', 'session_id'))
                if new is None or owner_changed:
                    disconnected = {**previous, 'connection': 'disconnected', 'observed_at': _now()}
                    try:
                        result['posted'] += self._publish(disconnected)
                    except (OSError, ValueError) as error:
                        _record_error(result, error, 'publication_failed')
                        current.pop(mid, None)
            for payload in current.values():
                try:
                    result['posted'] += self._publish(payload)
                except (OSError, ValueError) as error:
                    _record_error(result, error, 'publication_failed')
            return result
        finally:
            self._lock.release()


def run_feed(url: str, *, host_id: str, control_dir: Path | None = None,
             interval: float = 5, once: bool = False) -> int:
    """One host process, bounded polling and interruptible graceful shutdown."""
    if not 1 <= interval <= 300:
        raise ValueError('interval must be between 1 and 300 seconds')
    collector = MissionActivityFeed(url, host_id=host_id, control_dir=control_dir,
                                    api_key=os.environ.get('HERDR_CP_API_KEY', ''))
    # Separate from Pi's directory: may start before the first Pi session exists.
    state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'herdr-engineering'
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state / 'mission-feed.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('mission feed already running') from None
        stopped = threading.Event()
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
        try:
            while not stopped.is_set():
                result = collector.run_once()
                if once:
                    print(json.dumps(result))
                    return 0 if result['capable'] and result['errors'] == 0 else 1
                stopped.wait(interval)
        finally:
            for sig, handler in old_handlers.items():
                with contextlib.suppress(ValueError):
                    signal.signal(sig, handler)
    return 0
