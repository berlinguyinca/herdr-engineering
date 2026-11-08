"""Content-addressed blob store: ``<root>/sha256/xx/yy/<sha>``, temp file + atomic rename, hash verified on read."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO, NewType

Sha256 = NewType("Sha256", str)

_SHA = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1 << 20


class InvalidSha(ValueError):
    """Not 64 lowercase hex characters."""


class BlobCorrupt(Exception):
    """Stored bytes no longer hash to their address."""


class BlobMissing(FileNotFoundError):
    """No blob with that sha."""


def validate_sha(value: str) -> Sha256:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InvalidSha("sha256 must be 64 lowercase hex characters")
    return Sha256(value)


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        (self._root / "sha256").mkdir(parents=True, exist_ok=True)
        self._tmp = self._root / "tmp"
        self._tmp.mkdir(exist_ok=True)

    def _path(self, sha: str) -> Path:
        s = validate_sha(sha)
        return self._root / "sha256" / s[:2] / s[2:4] / s

    def exists(self, sha: str) -> bool:
        return self._path(sha).is_file()

    def put(self, stream: BinaryIO) -> Sha256:
        digest = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(dir=self._tmp, prefix="put-")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = Sha256(digest.hexdigest())
            final = self._path(sha)
            if final.is_file() and self._intact(final, sha):
                return sha  # dedupe hit on verified bytes
            # absent, or present but corrupt: install the verified bytes atomically (concurrent identical puts
            # write identical bytes, so the last rename winning is harmless)
            self._mkdirs(final.parent)
            os.replace(tmp_name, final)
            self._fsync_dir(final.parent)
            return sha
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _intact(path: Path, sha: str) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
        except FileNotFoundError:
            return False
        return digest.hexdigest() == sha

    def _mkdirs(self, leaf: Path) -> None:
        """Create the two shard levels, fsyncing each parent so the new directory entries are durable."""
        shard1 = leaf.parent
        for d in (shard1, leaf):
            if not d.exists():
                d.mkdir(exist_ok=True)
                self._fsync_dir(d.parent)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def open(self, sha: str) -> BinaryIO:
        """Verified read: the whole blob is hashed before any byte is returned."""
        path = self._path(sha)
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise BlobMissing(sha) from exc
        try:
            digest = hashlib.sha256()
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
            if digest.hexdigest() != sha:
                raise BlobCorrupt(sha)
            handle.seek(0)
        except BaseException:
            handle.close()
            raise
        return handle
