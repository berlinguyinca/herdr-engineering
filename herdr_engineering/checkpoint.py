"""Immutable private checkpoint containers, atomically published by directory FD."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import weakref
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .recovery import ExecutionLease

_MAGIC = b"HERDRCP1"


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointManifest:
    checkpoint_id: str
    owner_id: str
    agent_id: str
    host_uid: str
    generation: int
    created_at: str
    sha256: str
    size_bytes: int
    private: bool = True


class ProtectedCheckpointStore:
    """One immutable file commits metadata and payload together; no public URLs."""

    def __init__(self, root: Path, *, max_bytes=256 * 1024 ** 2):
        self.root = Path(root).absolute()
        self.max_bytes = int(max_bytes)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = None
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            info = os.fstat(fd)
            if info.st_uid != os.getuid():
                os.close(fd)
                raise CheckpointError("checkpoint directory owner mismatch")
            os.fchmod(fd, 0o700)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise CheckpointError("private checkpoint directory unavailable") from exc
        self._directory_fd = fd
        self._cleanup = weakref.finalize(self, os.close, fd)

    def close(self):
        self._cleanup()

    def _directory(self):
        if not self._cleanup.alive:
            raise CheckpointError("checkpoint store is closed")
        return self._directory_fd

    def put(self, *, checkpoint_id: str, owner_id: str, agent_id: str,
            lease: ExecutionLease, payload: bytes) -> CheckpointManifest:
        self._validate_id(checkpoint_id)
        if not lease.checkpoint_capable:
            raise CheckpointError("lease is not checkpoint-capable")
        if not owner_id or not agent_id or not isinstance(payload, bytes):
            raise CheckpointError("checkpoint identity and bytes are required")
        if agent_id != lease.agent_id:
            raise CheckpointError("checkpoint agent differs from execution lease")
        if len(payload) > self.max_bytes:
            raise CheckpointError("checkpoint exceeds storage limit")
        manifest = CheckpointManifest(checkpoint_id, owner_id, agent_id, lease.host_uid,
            lease.generation, datetime.now(UTC).isoformat(), hashlib.sha256(payload).hexdigest(), len(payload))
        metadata = json.dumps(asdict(manifest), sort_keys=True).encode()
        if len(metadata) > 65536:
            raise CheckpointError("checkpoint metadata exceeds storage limit")
        directory = self._directory()
        temporary = f".{checkpoint_id}.{uuid4().hex}.tmp"
        name = checkpoint_id + ".checkpoint"
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(_MAGIC + len(metadata).to_bytes(4, "big"))
                stream.write(metadata)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                existing, data = self._read(checkpoint_id, owner_id=owner_id, expected_lease=lease)
                if data != payload:
                    raise CheckpointError("immutable checkpoint conflict") from None
                os.fsync(directory)
                return existing
            os.fsync(directory)
            return manifest
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def _read(self, checkpoint_id, *, owner_id, expected_lease=None):
        self._validate_id(checkpoint_id)
        try:
            fd = os.open(checkpoint_id + ".checkpoint", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=self._directory())
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > self.max_bytes + 65548):
                    raise CheckpointError("checkpoint file is not private or exceeds storage limit")
                header = stream.read(12)
                if len(header) != 12 or header[:8] != _MAGIC:
                    raise CheckpointError("checkpoint format is invalid")
                length = int.from_bytes(header[8:], "big")
                if not 0 < length <= 65536:
                    raise CheckpointError("checkpoint format is invalid")
                manifest = CheckpointManifest(**json.loads(stream.read(length)))
                payload = stream.read(self.max_bytes + 1)
        except (OSError, ValueError, TypeError) as exc:
            raise CheckpointError("checkpoint unavailable or invalid") from exc
        if manifest.owner_id != owner_id:
            raise CheckpointError("checkpoint owner mismatch")
        if manifest.checkpoint_id != checkpoint_id or manifest.private is not True:
            raise CheckpointError("checkpoint identity mismatch")
        if expected_lease is not None and (manifest.agent_id != expected_lease.agent_id
                or manifest.host_uid != expected_lease.host_uid or manifest.generation != expected_lease.generation):
            raise CheckpointError("checkpoint lease identity mismatch")
        if (type(manifest.size_bytes) is not int or manifest.size_bytes != len(payload)
                or len(payload) > self.max_bytes or hashlib.sha256(payload).hexdigest() != manifest.sha256):
            raise CheckpointError("checkpoint digest mismatch")
        return manifest, payload

    def get(self, checkpoint_id: str, *, owner_id: str, expected_lease=None) -> bytes:
        return self._read(checkpoint_id, owner_id=owner_id, expected_lease=expected_lease)[1]

    @staticmethod
    def _validate_id(checkpoint_id: str) -> None:
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", checkpoint_id):
            raise CheckpointError("invalid checkpoint identifier")

