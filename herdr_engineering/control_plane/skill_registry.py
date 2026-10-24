"""Durable registry for untrusted skills; never imports, installs or executes them.

Callers pass authenticated Principals and a live ``principal_valid`` callback
(normally CollabService.still_valid). The relational projection must be populated;
unapproved/missing rows fail closed. Maintainers and Ed25519 keys come only from
server configuration. Verification proves integrity/authenticity, NOT code safety.

Packages are bounded canonical UTF-8 JSON (sorted keys, compact separators, not
RFC 8785), with base64 file bytes. SHA-256 and Ed25519 cover the entire unsigned
body including the manifest, file bytes and key ID. Postgres holds only manifests,
references and audit tombstones. S3 conditional writes prohibit replacement.
Both asyncpg connections must be used exclusively on their owning loop. The
journal connection must use autocommit and the same database/schema as the registry.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from botocore.exceptions import ClientError

from .collab import CollabError, Principal
from .collab_storage import _relative_path, _segment, _validate_json
from .collab_store import LIFECYCLE_LOCK

MAX_PACKAGE_BYTES = 16 * 1024 * 1024
_FIELDS = {'name', 'version', 'input_schema', 'output_schema', 'capabilities',
           'supported_file_types', 'fixtures', 'tests', 'dependency_lock', 'provenance'}


def _canonical(value: Any) -> bytes:
    _validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _validate_content(metadata: dict, files: dict) -> None:
    if (not isinstance(files, dict) or not 1 <= len(files) <= 1000
            or not files.get('SKILL.md') or any(type(v) is not bytes for v in files.values())):
        raise ValueError('package requires SKILL.md and byte-valued files')
    for path in files:
        _relative_path(path)
        if len(path) > 1024 or ':' in path:
            raise ValueError('invalid package path')
    if sum(map(len, files.values())) > MAX_PACKAGE_BYTES:
        raise ValueError('package too large')
    for field in ('name', 'version'):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError('name and version are required')
    if len(metadata['name']) > 200 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', metadata['version']):
        raise ValueError('invalid name or version')
    for field in ('input_schema', 'output_schema', 'provenance'):
        if not isinstance(metadata.get(field), dict):
            raise ValueError('schemas and provenance must be objects')
    if not metadata['provenance']:
        raise ValueError('provenance is required')
    for field in ('capabilities', 'supported_file_types', 'fixtures', 'tests'):
        value = metadata.get(field)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
            raise ValueError('expected a list of strings')
        if len(set(value)) != len(value):
            raise ValueError('duplicate manifest entries')
    if not metadata['fixtures'] and not metadata['tests']:
        raise ValueError('fixtures or tests are required')
    references = [*metadata['fixtures'], *metadata['tests'], metadata.get('dependency_lock')]
    if any(not isinstance(p, str) or p not in files for p in references):
        raise ValueError('missing fixture, test or dependency lock')
    _canonical({k: metadata[k] for k in _FIELDS})


@dataclass(frozen=True)
class SkillDraft:
    draft_id: str
    skill_id: str
    version: str


@dataclass(frozen=True)
class SkillVersion:
    skill_id: str
    version: str
    storage_key: str
    sha256: str


@dataclass(frozen=True)
class SkillPackage:
    manifest: dict
    files: dict[str, bytes]
    sha256: str
    signature: str
    key_id: str


@dataclass(frozen=True)
class VerificationReport:
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _body(package: SkillPackage) -> dict:
    return {'manifest': package.manifest, 'key_id': package.key_id,
            'files': {p: base64.b64encode(b).decode('ascii') for p, b in package.files.items()}}


def _encode(package: SkillPackage) -> bytes:
    data = _canonical({**_body(package), 'sha256': package.sha256, 'signature': package.signature})
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError('package too large')
    return data


def verify_skill_package(package: SkillPackage, trusted_keys: dict) -> VerificationReport:
    """Verify a detached package using configured public keys, never its own key."""
    try:
        _validate_content(package.manifest, package.files)
        m = package.manifest
        if (type(m.get('schema_version')) is not int or m['schema_version'] != 1
                or m.get('trust') != 'untrusted'
                or m.get('execution') != 'harness_only'
                or m.get('scope') not in {'private', 'room', 'shared'}
                or (m['scope'] == 'room') != (m.get('room_id') is not None)):
            raise ValueError('invalid manifest')
        for field in ('skill_id', 'creator_id'):
            if not isinstance(m.get(field), str) or not m[field].strip():
                raise ValueError('missing package identity')
        if datetime.fromisoformat(m['created_at']).tzinfo is None:
            raise ValueError('created_at requires a timezone')
        if m['scope'] == 'room' and (not isinstance(m['room_id'], str) or not m['room_id']):
            raise ValueError('missing room identity')
        expected = {p: {'sha256': hashlib.sha256(b).hexdigest(), 'size_bytes': len(b)}
                    for p, b in package.files.items()}
        if m.get('files') != expected:
            return VerificationReport(('file checksum mismatch',))
        body = _canonical(_body(package))
        _encode(package)
        if hashlib.sha256(body).hexdigest() != package.sha256:
            return VerificationReport(('package checksum mismatch',))
        trusted_keys[package.key_id].verify(bytes.fromhex(package.signature), body)
    except Exception:
        return VerificationReport(('invalid package or untrusted signature',))
    return VerificationReport(())


class SkillRegistry:
    def __init__(self, conn, *, journal_conn, s3, bucket: str, environment: str, signing_key,
                 key_id: str, trusted_keys: dict, principal_valid, maintainers=()):
        self.conn, self.s3, self.bucket = conn, s3, bucket
        if journal_conn is conn:
            raise ValueError('package journal requires an independent autocommit connection')
        self.journal_conn = journal_conn
        self.environment = _segment(environment)
        self.signing_key, self.key_id = signing_key, _segment(key_id)
        self.trusted_keys = dict(trusted_keys)
        self.principal_valid = principal_valid
        self.maintainers = frozenset(maintainers)
        if not bucket or not callable(principal_valid):
            raise ValueError('storage and live principal validator are required')
        # Fail startup if configuration cannot verify its own signatures.
        self.trusted_keys[key_id].verify(signing_key.sign(b'skill-registry'), b'skill-registry')

    def _sign(self, manifest: dict, files: dict[str, bytes]) -> SkillPackage:
        unsigned = SkillPackage(manifest, files, '', '', self.key_id)
        body = _canonical(_body(unsigned))
        package = SkillPackage(manifest, files, hashlib.sha256(body).hexdigest(),
                               self.signing_key.sign(body).hex(), self.key_id)
        _encode(package)
        return package

    def verify_skill_package(self, package: SkillPackage) -> VerificationReport:
        return verify_skill_package(package, self.trusted_keys)

    def _key(self, digest: str) -> str:
        return f'env/{self.environment}/skills/sha256/{digest}/package.json'

    def _read_bytes(self, key: str) -> bytes:
        body = self.s3.get_object(Bucket=self.bucket, Key=key)['Body']
        try:
            data = body.read(MAX_PACKAGE_BYTES + 1)
            if len(data) > MAX_PACKAGE_BYTES:
                raise ValueError('package too large')
            return data
        finally:
            body.close()

    async def _write(self, package: SkillPackage, *, request_key: str | None = None) -> str:
        key, data = self._key(package.sha256), _encode(package)
        # This connection must target the same database/schema, and must never
        # participate in the caller's transaction. No PUT before durable ownership.
        if self.journal_conn.is_in_transaction():
            raise CollabError(503, 'package journal requires autocommit')
        # Independent session ownership also survives loss of the registry's
        # transaction connection while S3 is still writing.
        try:
            acquired = await self.journal_conn.fetchval(
                'SELECT pg_try_advisory_lock_shared($1)', LIFECYCLE_LOCK)
        except Exception:
            raise CollabError(503, 'package journal unavailable') from None
        if not acquired:
            raise CollabError(503, 'collaboration lifecycle busy; retry later')
        try:
            try:
                await self.journal_conn.execute(
                    'INSERT INTO collab_skill_package_journal '
                    '(creator_id,storage_bucket,storage_key,request_key) VALUES ($1,$2,$3,$4) '
                    'ON CONFLICT (creator_id,storage_bucket,storage_key) DO NOTHING',
                    package.manifest['creator_id'], self.bucket, key, request_key or 'package:' + package.sha256)
            except Exception:
                raise CollabError(503, 'package journal unavailable') from None
            def put():
                try:
                    self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                                       ContentType='application/json', IfNoneMatch='*')
                except ClientError as exc:
                    if exc.response.get('Error', {}).get('Code') != 'PreconditionFailed':
                        raise
                if self._read_bytes(key) != data:
                    raise ValueError('persisted package differs')
            try:
                # A cancelled asyncio wrapper does not stop a blocking S3 PUT. Keep
                # the transaction/lifecycle lock until the provider actually stops,
                # including repeated cancellation and Runner shutdown task sweeps.
                completion = asyncio.get_running_loop().run_in_executor(None, copy_context().run, put)
                cancelled = False
                while not completion.done():
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        cancelled = True
                    except BaseException:
                        break
                if cancelled:
                    completion.exception()
                    raise asyncio.CancelledError
                completion.result()
            except Exception:
                raise CollabError(503, 'package storage unavailable') from None
            return key
        finally:
            try:
                await self.journal_conn.execute('SELECT pg_advisory_unlock_shared($1)', LIFECYCLE_LOCK)
            except Exception:
                raise CollabError(503, 'package journal unavailable') from None

    async def _load(self, row) -> SkillPackage:
        try:
            if row['storage_key'] != self._key(row['sha256']):
                raise ValueError('unexpected object reference')
            data = await asyncio.to_thread(self._read_bytes, row['storage_key'])
            obj = json.loads(data)
            package = SkillPackage(obj['manifest'],
                                   {p: base64.b64decode(b, validate=True) for p, b in obj['files'].items()},
                                   obj['sha256'], obj['signature'], obj['key_id'])
            manifest = row['manifest']
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            if (data != _encode(package) or package.sha256 != row['sha256']
                    or package.signature != row['signature'] or package.key_id != row['key_id']
                    or package.manifest != manifest or not self.verify_skill_package(package).ok):
                raise ValueError('package verification failed')
            return package
        except Exception:
            raise CollabError(409, 'package verification failed') from None

    async def _principal(self, p: Principal, *, human=False):
        if (p.kind not in {'user', 'agent'} or (human and p.kind != 'user')
                or not self.principal_valid(p)
                or (p.kind == 'user' and p.owner != p.id)):
            raise CollabError(403, 'forbidden')
        status = await self.conn.fetchval(
            'SELECT status FROM collab_users WHERE user_id=$1 FOR SHARE', p.owner)
        if status != 'approved':
            raise CollabError(403, 'forbidden')
        if p.is_agent:
            row = await self.conn.fetchrow(
                'SELECT owner_id,status FROM collab_agents WHERE agent_id=$1 FOR SHARE', p.id)
            if row is None or row['owner_id'] != p.owner or row['status'] != 'active':
                raise CollabError(403, 'forbidden')

    async def _room(self, p: Principal, room_id: str, *, publish=False):
        room = await self.conn.fetchrow(
            'SELECT owner_id,status FROM collab_rooms WHERE room_id=$1 FOR SHARE', room_id)
        member = await self.conn.fetchrow(
            'SELECT status,role,expires_at FROM collab_room_memberships '
            'WHERE room_id=$1 AND user_id=$2 FOR SHARE', room_id, p.owner)
        now = datetime.now(UTC).timestamp()
        if (room is None or room['status'] != 'active' or member is None
                or member['status'] != 'approved'
                or (member['expires_at'] is not None and member['expires_at'] <= now)
                or (publish and (room['owner_id'] != p.id or member['role'] != 'owner'))):
            raise CollabError(404, 'not found')
        if p.is_agent:
            grant = await self.conn.fetchrow(
                'SELECT status,actions,expires_at FROM collab_agent_room_attachments '
                'WHERE room_id=$1 AND agent_id=$2 FOR SHARE', room_id, p.id)
            if (grant is None or grant['status'] != 'active' or 'read' not in grant['actions']
                    or (grant['expires_at'] is not None and grant['expires_at'] <= now)):
                raise CollabError(404, 'not found')

    async def _publication(self, p: Principal, scope: str, room_id: str | None):
        await self._principal(p, human=True)
        if scope not in {'private', 'room', 'shared'}:
            raise CollabError(400, 'invalid scope')
        if scope == 'room':
            await self._room(p, room_id, publish=True)
        if scope == 'shared' and not (p.admin or p.id in self.maintainers):
            raise CollabError(403, 'shared publication requires a maintainer')

    async def _audit(self, p, action, entity_id):
        await self.conn.execute(
            'INSERT INTO collab_audit_events(event_id,actor_id,action,entity_type,entity_id) '
            "VALUES ($1,$2,$3,'skill',$4)", str(uuid4()), p.id, action, entity_id)

    async def create_skill_draft(self, principal: Principal, metadata: dict) -> SkillDraft:
        """Create once per creator/request_key; reject reuse with different canonical input."""
        try:
            if not isinstance(metadata, dict) or set(metadata) - _FIELDS - {'files', 'skill_id', 'room_id', 'request_key'}:
                raise ValueError('unknown metadata fields')
            request_key = metadata.get('request_key')
            if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
                raise ValueError('a caller-supplied request_key is required')
            _validate_content(metadata, metadata.get('files'))
        except (ValueError, TypeError, KeyError):
            raise CollabError(400, 'invalid skill metadata') from None
        # Copy at the boundary: callers cannot mutate content while storage is awaited.
        files = dict(metadata['files'])
        content = json.loads(_canonical({k: metadata[k] for k in _FIELDS}))
        skill_id, draft_id = metadata.get('skill_id') or str(uuid4()), str(uuid4())
        room_id = metadata.get('room_id')
        fingerprint = hashlib.sha256(_canonical({
            **content, 'skill_id': metadata.get('skill_id'), 'room_id': room_id,
            'files': {p: {'sha256': hashlib.sha256(b).hexdigest(), 'size_bytes': len(b)}
                      for p, b in files.items()},
        })).hexdigest()
        if not isinstance(skill_id, str) or (room_id is not None and not isinstance(room_id, str)):
            raise CollabError(400, 'invalid skill or room ID')
        async with self.conn.transaction():
            await self.conn.execute('SELECT pg_advisory_xact_lock_shared($1)', LIFECYCLE_LOCK)
            await self._principal(principal, human=True)
            # Serialize absent-row races as well as committed retries across processes.
            await self.conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                                    _canonical([principal.key, request_key]).decode())
            previous = await self.conn.fetchrow(
                'SELECT * FROM collab_skill_drafts WHERE created_by=$1 AND request_key=$2',
                principal.id, request_key)
            if previous:
                if previous['request_fingerprint'] != fingerprint:
                    raise CollabError(409, 'draft request key conflicts with original input')
                status = await self.conn.fetchval(
                    'SELECT status FROM collab_skills WHERE skill_id=$1', previous['skill_id'])
                if status == 'revoked':
                    raise CollabError(404, 'not found')
                await self._principal(principal, human=True)
                return SkillDraft(previous['draft_id'], previous['skill_id'], previous['version'])
            skill = await self.conn.fetchrow('SELECT * FROM collab_skills WHERE skill_id=$1 FOR UPDATE', skill_id)
            if skill is None:
                if 'skill_id' in metadata:
                    raise CollabError(404, 'not found')
                await self.conn.execute(
                    'INSERT INTO collab_skills(skill_id,owner_id,name) VALUES ($1,$2,$3)',
                    skill_id, principal.id, content['name'])
            elif skill['owner_id'] != principal.id or skill['status'] == 'revoked':
                raise CollabError(404, 'not found')
            manifest = {**content, 'schema_version': 1, 'skill_id': skill_id,
                        'creator_id': principal.id, 'scope': 'private', 'room_id': None,
                        'trust': 'untrusted', 'execution': 'harness_only',
                        'created_at': datetime.now(UTC).isoformat(),
                        'files': {p: {'sha256': hashlib.sha256(b).hexdigest(), 'size_bytes': len(b)}
                                  for p, b in files.items()}}
            package = self._sign(manifest, files)
            key = await self._write(package, request_key='draft:' + request_key)
            await self._principal(principal, human=True)
            await self.conn.execute(
                'INSERT INTO collab_skill_drafts '
                '(draft_id,skill_id,created_by,target_room_id,version,storage_key,sha256,manifest,signature,key_id,request_key,request_fingerprint,storage_bucket) '
                'VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13)', draft_id, skill_id, principal.id,
                room_id, content['version'], key, package.sha256, _canonical(manifest).decode(),
                package.signature, package.key_id, request_key, fingerprint, self.bucket)
            await self._audit(principal, 'skill.drafted', draft_id)
        return SkillDraft(draft_id, skill_id, content['version'])

    async def publish_skill(self, principal: Principal, draft_id: str, scope: str) -> SkillVersion:
        async with self.conn.transaction():
            await self.conn.execute('SELECT pg_advisory_xact_lock_shared($1)', LIFECYCLE_LOCK)
            await self._principal(principal, human=True)
            draft = await self.conn.fetchrow('SELECT * FROM collab_skill_drafts WHERE draft_id=$1', draft_id)
            if draft is None or draft['created_by'] != principal.id:
                raise CollabError(404, 'not found')
            skill = await self.conn.fetchrow('SELECT * FROM collab_skills WHERE skill_id=$1 FOR UPDATE', draft['skill_id'])
            if skill is None or skill['owner_id'] != principal.id or skill['status'] == 'revoked':
                raise CollabError(404, 'not found')
            room_id = draft['target_room_id'] if scope == 'room' else None
            await self._publication(principal, scope, room_id)
            existing = await self.conn.fetchrow(
                'SELECT * FROM collab_skill_versions WHERE skill_id=$1 AND version=$2',
                draft['skill_id'], draft['version'])
            if existing:
                if (existing['source_draft_id'] != draft_id or existing['status'] != 'published'
                        or skill['scope'] != scope or skill['room_id'] != room_id):
                    raise CollabError(409, 'version is immutable')
                await self._load(existing)
                await self._publication(principal, scope, room_id)
                return self._version(existing)
            if skill['status'] == 'published' and (skill['scope'], skill['room_id']) != (scope, room_id):
                raise CollabError(409, 'published scope is immutable; create a new skill')
            draft_package = await self._load(draft)
            package = self._sign({**draft_package.manifest, 'scope': scope, 'room_id': room_id,
                                  'published_by': principal.id, 'published_at': datetime.now(UTC).isoformat()},
                                 draft_package.files)
            key = await self._write(package, request_key='publish:' + draft_id + ':' + scope)
            await self._publication(principal, scope, room_id)
            await self.conn.execute(
                "UPDATE collab_skills SET scope=$2,room_id=$3,status='published' WHERE skill_id=$1",
                draft['skill_id'], scope, room_id)
            row = await self.conn.fetchrow(
                'INSERT INTO collab_skill_versions '
                '(skill_id,version,storage_key,sha256,status,manifest,signature,key_id,source_draft_id,storage_bucket) '
                "VALUES ($1,$2,$3,$4,'published',$5::jsonb,$6,$7,$8,$9) RETURNING *",
                draft['skill_id'], draft['version'], key, package.sha256,
                _canonical(package.manifest).decode(), package.signature, package.key_id, draft_id, self.bucket)
            await self._audit(principal, 'skill.published', f'{draft["skill_id"]}:{draft["version"]}')
            return self._version(row)

    @staticmethod
    def _version(row) -> SkillVersion:
        return SkillVersion(*(row[k] for k in ('skill_id', 'version', 'storage_key', 'sha256')))

    async def get_skill_version(self, skill_id: str, version: str, *, principal: Principal) -> SkillPackage:
        """Principal is mandatory: there is no unauthenticated package retrieval path."""
        async with self.conn.transaction():
            await self.conn.execute('SELECT pg_advisory_xact_lock_shared($1)', LIFECYCLE_LOCK)
            await self._principal(principal)
            skill = await self.conn.fetchrow('SELECT * FROM collab_skills WHERE skill_id=$1 FOR SHARE', skill_id)
            if skill is None or skill['status'] != 'published':
                raise CollabError(404, 'not found')
            if skill['scope'] == 'private' and principal.owner != skill['owner_id']:
                raise CollabError(404, 'not found')
            if skill['scope'] == 'room':
                await self._room(principal, skill['room_id'])
            row = await self.conn.fetchrow(
                "SELECT * FROM collab_skill_versions WHERE skill_id=$1 AND version=$2 AND status='published' FOR SHARE",
                skill_id, version)
            if row is None:
                raise CollabError(404, 'not found')
            package = await self._load(row)
            if (package.manifest['skill_id'] != skill_id or package.manifest['version'] != version
                    or package.manifest['creator_id'] != skill['owner_id']
                    or package.manifest['scope'] != skill['scope'] or package.manifest['room_id'] != skill['room_id']):
                raise CollabError(409, 'package identity mismatch')
            await self._principal(principal)
            if skill['scope'] == 'room':
                await self._room(principal, skill['room_id'])
            return package
