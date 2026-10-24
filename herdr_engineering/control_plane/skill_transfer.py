"""Authorized skill transfers and an explicitly harness-side local install contract.

The control plane issues 60-second GET capabilities in MCP _meta, never model text.
It never invokes HarnessSkillInstaller. The harness fetches bytes with a bounded,
no-redirect transport, keeps the URL out of logs, and supplies them to install().
Public verification keys come from trusted harness configuration, not the package.
Installation does not execute code, install dependencies, or grant capabilities.
A harness may report completion separately; that receipt is a claim, not attestation.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .collab import CollabError
from .collab_store import LIFECYCLE_LOCK
from .skill_registry import MAX_PACKAGE_BYTES, SkillPackage, _encode, verify_skill_package

TRANSFER_TTL = 60


@dataclass(frozen=True)
class SignedPackageRef:
    skill_id: str
    version: str
    sha256: str
    transport_sha256: str
    size_bytes: int
    signature: str
    key_id: str
    expires_at: float
    url: str = field(repr=False)


class SkillTransfer:
    def __init__(self, registry, *, now=time.time):
        self.registry = registry
        self.conn = registry.conn
        self.now = now

    @staticmethod
    def _identity(skill_id, version):
        if (not isinstance(skill_id, str) or not skill_id.strip() or len(skill_id) > 200
                or not isinstance(version, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', version)):
            raise CollabError(400, 'invalid skill identity')

    async def _package(self, principal, skill_id, version):
        self._identity(skill_id, version)
        return await self.registry.get_skill_version(skill_id, version, principal=principal)

    @staticmethod
    def _view(package):
        return {'skill_id': package.manifest['skill_id'], 'version': package.manifest['version'],
                'sha256': package.sha256, 'manifest': package.manifest}

    async def get_skill(self, principal, skill_id, version):
        return self._view(await self._package(principal, skill_id, version))

    async def search_skills(self, principal, query, scope):
        if (not isinstance(query, str) or len(query) > 200
                or not isinstance(scope, str) or scope not in {'private', 'room', 'shared'}):
            raise CollabError(400, 'invalid skill search')
        async with self.conn.transaction():
            await self.conn.execute('SELECT pg_advisory_xact_lock_shared($1)', LIFECYCLE_LOCK)
            await self.registry._principal(principal)
            # Filter authorization BEFORE the limit, so hidden rows cannot crowd out results.
            rows = await self.conn.fetch("""
                SELECT s.skill_id, v.version FROM collab_skills s
                JOIN collab_skill_versions v USING (skill_id)
                WHERE s.status='published' AND v.status='published' AND s.scope=$1
                AND strpos(lower(s.name), lower($2)) > 0
                AND (s.scope='shared' OR (s.scope='private' AND s.owner_id=$3)
                  OR (s.scope='room' AND EXISTS (
                    SELECT 1 FROM collab_rooms r
                    JOIN collab_room_memberships m USING (room_id)
                    WHERE r.room_id=s.room_id AND r.status='active'
                      AND m.user_id=$3 AND m.status='approved'
                      AND (m.expires_at IS NULL OR m.expires_at>$5)
                      AND (NOT $6 OR EXISTS (
                        SELECT 1 FROM collab_agent_room_attachments a
                        WHERE a.room_id=r.room_id AND a.agent_id=$4 AND a.status='active'
                          AND 'read'=ANY(a.actions)
                          AND (a.expires_at IS NULL OR a.expires_at>$5))))))
                ORDER BY s.skill_id, v.version LIMIT 50
            """, scope, query, principal.owner, principal.id, self.now(), principal.is_agent)
            result = []
            for row in rows:
                try:
                    result.append(await self.get_skill(principal, row['skill_id'], row['version']))
                except CollabError as exc:
                    if exc.status != 404:
                        raise
            # Membership expiry can advance while other packages in the batch load.
            visible = []
            for item in result:
                try:
                    if item['manifest']['scope'] == 'room':
                        await self.registry._room(principal, item['manifest']['room_id'])
                    visible.append(item)
                except CollabError as exc:
                    if exc.status != 404:
                        raise
            await self.registry._principal(principal)
            return visible

    async def _audit(self, principal, action, skill_id, version):
        # Audit actor FK references users, not agents. No URLs/credentials/package bytes.
        await self.conn.execute(
            'INSERT INTO collab_audit_events(event_id,actor_id,action,entity_type,entity_id) '
            "VALUES ($1,$2,$3,'skill',$4)", str(uuid4()), principal.owner, action,
            f'{skill_id}:{version}:{principal.id}')

    async def _grant(self, principal, skill_id, version, *, install):
        async with self.conn.transaction():
            # The nested registry transaction retains lifecycle/row locks until this commits.
            package = await self._package(principal, skill_id, version)
            if install and not principal.is_agent:
                raise CollabError(403, 'agent required')
            data = _encode(package)
            expires_at = self.now() + TRANSFER_TTL
            try:
                url = await asyncio.to_thread(
                    self.registry.s3.generate_presigned_url, 'get_object',
                    Params={'Bucket': self.registry.bucket, 'Key': self.registry._key(package.sha256)},
                    ExpiresIn=TRANSFER_TTL, HttpMethod='GET')
                parsed = urlsplit(url)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('invalid download URL')
            except Exception:
                raise CollabError(503, 'skill transfer unavailable; retry') from None
            await self.registry._principal(principal)
            if package.manifest['scope'] == 'room':
                await self.registry._room(principal, package.manifest['room_id'])
            if self.now() >= expires_at:
                raise CollabError(503, 'skill transfer expired; retry')
            await self._audit(principal, 'skill.install_requested' if install else 'skill.download_requested',
                              skill_id, version)
            return SignedPackageRef(skill_id, version, package.sha256,
                                    hashlib.sha256(data).hexdigest(), len(data),
                                    package.signature, package.key_id, expires_at, url)

    async def download_skill(self, principal, skill_id, version) -> SignedPackageRef:
        return await self._grant(principal, skill_id, version, install=False)

    async def install_skill(self, principal, skill_id, version) -> SignedPackageRef:
        return await self._grant(principal, skill_id, version, install=True)

    async def record_installation(self, principal, skill_id, version, sha256):
        """Authenticated harness receipt, called only after local activation succeeds."""
        async with self.conn.transaction():
            package = await self._package(principal, skill_id, version)
            if not principal.is_agent:
                raise CollabError(403, 'agent required')
            if sha256 != package.sha256:
                raise CollabError(409, 'package checksum mismatch')
            result = await self.conn.fetchval(
                'INSERT INTO collab_skill_installations '
                '(installation_id,skill_id,version,agent_id,user_id) VALUES ($1,$2,$3,$4,$5) '
                'ON CONFLICT (skill_id,version,agent_id) DO NOTHING RETURNING installation_id',
                str(uuid4()), skill_id, version, principal.id, principal.owner)
            if result:
                await self._audit(principal, 'skill.install_reported', skill_id, version)


class HarnessSkillInstaller:
    """Harness-only installer. Root must be private to the harness OS account.

    Cache entries are immutable directories named by the signed body SHA-256.
    A SHA-256 of the skill ID names the active symlink, replaced atomically only
    after every byte is verified. No URLs, dependency execution, or executable
    permissions are persisted. The lock serializes cooperating local installers;
    this is not a sandbox against other processes running as the same OS user.
    """
    def __init__(self, root: Path, *, trusted_keys: dict, now=time.time):
        self.root = Path(root).absolute()
        self.trusted_keys = dict(trusted_keys)
        self.now = now

    def _verify(self, ref, data):
        try:
            if (type(data) is not bytes or not 0 < len(data) <= MAX_PACKAGE_BYTES
                    or len(data) != ref.size_bytes
                    or not math.isfinite(ref.expires_at) or self.now() >= ref.expires_at
                    or not re.fullmatch('[0-9a-f]{64}', ref.sha256)
                    or hashlib.sha256(data).hexdigest() != ref.transport_sha256):
                raise ValueError('invalid transfer')
            obj = json.loads(data)
            package = SkillPackage(obj['manifest'],
                                   {p: base64.b64decode(b, validate=True) for p, b in obj['files'].items()},
                                   obj['sha256'], obj['signature'], obj['key_id'])
            if (not verify_skill_package(package, self.trusted_keys).ok or _encode(package) != data
                    or package.sha256 != ref.sha256 or package.signature != ref.signature
                    or package.key_id != ref.key_id or package.manifest['skill_id'] != ref.skill_id
                    or package.manifest['version'] != ref.version):
                raise ValueError('invalid package')
            # Reject file/directory collisions before touching the local filesystem.
            for path in package.files:
                if any(str(parent) in package.files for parent in Path(path).parents):
                    raise ValueError('conflicting package paths')
            return package
        except Exception:
            raise ValueError('skill package verification failed') from None

    @staticmethod
    def _directory(path):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('symlink in install path')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError('invalid install directory')

    @staticmethod
    def _check_cache(path, files):
        if path.is_symlink() or not path.is_dir():
            raise ValueError('invalid skill cache')
        actual = {}
        for entry in path.rglob('*'):
            if entry.is_symlink() or (not entry.is_dir() and not entry.is_file()):
                raise ValueError('invalid skill cache entry')
            if entry.is_file():
                relative = entry.relative_to(path).as_posix()
                if relative not in files or entry.stat().st_size != len(files[relative]):
                    raise ValueError('skill cache mismatch')
                actual[relative] = entry.read_bytes()
        if actual != files:
            raise ValueError('skill cache mismatch')

    def install(self, ref: SignedPackageRef, data: bytes) -> Path:
        package = self._verify(ref, data)
        self._directory(self.root)
        root_stat = self.root.stat()
        if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
            raise ValueError('skill install root must be private to the harness user')
        # O_NOFOLLOW also prevents replacing the installer lock with a symlink.
        fd = os.open(self.root / '.install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cache, active = self.root / 'cache', self.root / 'installed'
            self._directory(cache)
            self._directory(active)
            target = cache / ref.sha256
            if target.exists() or target.is_symlink():
                self._check_cache(target, package.files)
            else:
                staging = Path(tempfile.mkdtemp(prefix='package-', suffix='.tmp', dir=cache))
                try:
                    for relative, content in package.files.items():
                        destination = staging / relative
                        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with destination.open('xb') as output:
                            os.chmod(destination, 0o600)
                            output.write(content)
                    os.replace(staging, target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            # Recheck expiration after waiting on the lock / filesystem I/O.
            if self.now() >= ref.expires_at:
                raise ValueError('skill transfer expired')
            slot = hashlib.sha256(ref.skill_id.encode()).hexdigest()
            pending = active / f'{uuid4().hex}.tmp'
            try:
                pending.symlink_to(Path('..') / 'cache' / ref.sha256, target_is_directory=True)
                os.replace(pending, active / slot)
            finally:
                pending.unlink(missing_ok=True)
            return target
