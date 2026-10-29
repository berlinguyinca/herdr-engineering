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
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
from contextlib import ExitStack
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
    """Harness-only installer with trusted ancestry and descriptor-anchored writes.

    Ancestors must be root/harness-owned and not group/other-writable unless
    sticky (e.g. root-owned /tmp). The root and internal directories are private.

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
    def _trusted_directory(info, *, private=False):
        if private:
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('skill install directory must be private to the harness user')
        elif (info.st_uid not in {0, os.getuid()}
              or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            # Root-owned sticky /tmp is safe only because every descendant is
            # also root/harness-owned. Foreign owners can chmod/replace children.
            raise ValueError('untrusted skill install ancestor')

    @classmethod
    def _open_directory(cls, stack, name, parent=None, *, create=False, private=False):
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError('symlink or invalid install directory') from None
            raise
        stack.callback(os.close, fd)
        cls._trusted_directory(os.fstat(fd), private=private)
        return fd

    @classmethod
    def _check_directories(cls, anchors):
        for parent, name, fd, private in anchors:
            try:
                info = os.fstat(fd)
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino):
                    raise ValueError('skill install directory changed')
                cls._trusted_directory(info, private=private)
            except OSError:
                raise ValueError('skill install directory changed') from None

    @classmethod
    def _check_cache(cls, fd, files, prefix=''):
        actual = {}
        for name in os.listdir(fd):
            relative = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with ExitStack() as stack:
                    child = cls._open_directory(stack, name, fd)
                    actual.update(cls._check_cache(child, files, relative + '/'))
            elif stat.S_ISREG(info.st_mode) and relative in files:
                # Nonblocking + fstat prevents a raced FIFO/device from being read.
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(source, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_size != len(files[relative]):
                        raise ValueError('skill cache mismatch')
                    actual[relative] = stream.read(len(files[relative]) + 1)
            else:
                raise ValueError('invalid skill cache entry')
        if not prefix and actual != files:
            raise ValueError('skill cache mismatch')
        return actual

    @classmethod
    def _write_cache(cls, fd, files):
        for relative, content in files.items():
            with ExitStack() as stack:
                parent = fd
                parts = relative.split('/')
                for part in parts[:-1]:
                    parent = cls._open_directory(stack, part, parent, create=True, private=True)
                output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
                with os.fdopen(output, 'wb') as stream:
                    stream.write(content)

    def install(self, ref: SignedPackageRef, data: bytes) -> Path:
        package = self._verify(ref, data)
        if '..' in self.root.parts:
            raise ValueError('invalid install ancestry')
        with ExitStack() as stack:
            # Walk from the filesystem root without resolving any symlink. Keep
            # every ancestor open, so all writes and checks use the same objects.
            root = self._open_directory(stack, '/')
            anchors = [(None, '/', root, False)]
            for index, name in enumerate(self.root.parts[1:], start=1):
                private = index == len(self.root.parts) - 1
                child = self._open_directory(stack, name, root, create=True, private=private)
                anchors.append((root, name, child, private))
                root = child
            self._trusted_directory(os.fstat(root), private=True)
            lock_fd = os.open('.install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=root)
            stack.callback(os.close, lock_fd)
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode):
                raise ValueError('invalid install lock')
            self._trusted_directory(lock_info, private=True)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._check_directories(anchors)
            cache = self._open_directory(stack, 'cache', root, create=True, private=True)
            active = self._open_directory(stack, 'installed', root, create=True, private=True)
            anchors.extend([(root, 'cache', cache, True), (root, 'installed', active, True)])
            try:
                os.stat(ref.sha256, dir_fd=cache, follow_symlinks=False)
            except FileNotFoundError:
                staging = f'package-{uuid4().hex}.tmp'
                os.mkdir(staging, mode=0o700, dir_fd=cache)
                try:
                    with ExitStack() as staged:
                        staging_fd = self._open_directory(staged, staging, cache, private=True)
                        self._write_cache(staging_fd, package.files)
                        self._check_directories(anchors)
                        os.replace(staging, ref.sha256, src_dir_fd=cache, dst_dir_fd=cache)
                finally:
                    # rmtree uses fd-based traversal; never re-resolve self.root.
                    try:
                        shutil.rmtree(staging, dir_fd=cache)
                    except FileNotFoundError:
                        pass
            target = self._open_directory(stack, ref.sha256, cache, private=True)
            self._check_cache(target, package.files)
            anchors.append((cache, ref.sha256, target, True))
            if self.now() >= ref.expires_at:
                raise ValueError('skill transfer expired')
            self._check_directories(anchors)
            slot = hashlib.sha256(ref.skill_id.encode()).hexdigest()
            pending = f'{uuid4().hex}.tmp'
            try:
                previous = os.readlink(slot, dir_fd=active)
            except FileNotFoundError:
                previous = None
            try:
                os.symlink(f'../cache/{ref.sha256}', pending, dir_fd=active)
                os.replace(pending, slot, src_dir_fd=active, dst_dir_fd=active)
                try:
                    self._check_directories(anchors)
                except ValueError:
                    # If a pathname moved during rename, undo activation only in
                    # the original directory. Never touch an attacker replacement.
                    if previous is None:
                        os.unlink(slot, dir_fd=active)
                    else:
                        os.symlink(previous, pending, dir_fd=active)
                        os.replace(pending, slot, src_dir_fd=active, dst_dir_fd=active)
                    raise
            finally:
                try:
                    os.unlink(pending, dir_fd=active)
                except FileNotFoundError:
                    pass
            return self.root / 'cache' / ref.sha256
