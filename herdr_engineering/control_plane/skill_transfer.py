"""Authorized skill transfers and an explicitly harness-side local install contract.

The control plane issues a 60-second, single-use, principal-bound capability whose redemption route streams the signed
package through the control plane (no presigned object-store URL, so no browser or CSP origin is involved). It never
invokes HarnessSkillInstaller. The harness fetches bytes with a bounded, no-redirect transport, keeps the URL out of
logs, and supplies them to install(). Public verification keys come from trusted harness configuration, not the package.
Installation does not execute code, install dependencies, or grant capabilities. A harness may report completion
separately; that receipt is a claim, not attestation.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .skill_registry import (
    MAX_PACKAGE_BYTES,
    SkillError,
    SkillPackage,
    _encode,
    verify_skill_package,
)

TRANSFER_TTL = 60
DOWNLOAD_PATH = '/api/v1/skills/download/'


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
    """Issues and redeems transfer capabilities. Only the SHA-256 of a token is kept, in memory, for 60 seconds."""

    def __init__(self, registry, *, now=time.time, record_event=None):
        self.registry = registry
        self.now = now
        self.record_event = record_event
        self._grants: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _audit(self, action, principal, skill_id, version):
        # No URL, token or package bytes.
        if self.record_event:
            self.record_event('skill', skill_id, action, {'version': version, 'principal_id': principal})

    def _sweep(self):
        now = self.now()
        for key in [k for k, g in self._grants.items() if g['expires_at'] <= now]:
            del self._grants[key]

    def get_skill(self, principal, skill_id, version):
        return self.registry.view(self.registry.load(principal, skill_id, version))

    def grant(self, principal, skill_id, version, *, install=False) -> SignedPackageRef:
        package = self.registry.load(principal, skill_id, version)
        data = _encode(package)
        token = secrets.token_urlsafe(32)
        expires_at = self.now() + TRANSFER_TTL
        with self._lock:
            self._sweep()
            if len(self._grants) >= 10000:
                raise SkillError(503, 'skill transfer busy; retry')
            self._grants[hashlib.sha256(token.encode()).hexdigest()] = {
                'principal': principal, 'skill_id': skill_id, 'version': version, 'expires_at': expires_at}
        self._audit('skill.install_requested' if install else 'skill.download_requested',
                    principal, skill_id, version)
        return SignedPackageRef(skill_id, version, package.sha256, hashlib.sha256(data).hexdigest(), len(data),
                                package.signature, package.key_id, expires_at, DOWNLOAD_PATH + token)

    def redeem(self, principal, token) -> bytes:
        """Single use. Wrong principal, expired, replayed and revoked-since-issue are all the same 404."""
        key = hashlib.sha256(str(token).encode()).hexdigest()
        with self._lock:
            self._sweep()
            grant = self._grants.pop(key, None)
        if grant is None or grant['principal'] != principal:
            raise SkillError(404, 'not found')
        # Authorization is live at redemption, not only at issue.
        package = self.registry.load(principal, grant['skill_id'], grant['version'])
        return _encode(package)

    def record_installation(self, principal, skill_id, version, sha256):
        """Authenticated harness receipt, called only after local activation succeeds (a claim, not attestation)."""
        package = self.registry.load(principal, skill_id, version)
        if sha256 != package.sha256:
            raise SkillError(409, 'package checksum mismatch')
        self._audit('skill.install_reported', principal, skill_id, version)


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
