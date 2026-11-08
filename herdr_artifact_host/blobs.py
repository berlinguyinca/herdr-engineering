"""Content-addressed blob store: ``<root>/sha256/xx/yy/<sha>``, temp file + atomic rename, hash verified on read.

Verification cache and its residual.  Hashing a large blob on every request is too expensive, so a file identity
(device, inode, size, mtime, ctime, all read from the open handle) that was hashed and matched its address is
remembered for ``VERIFIED_TTL_SECONDS``.  Only range, HEAD and conditional reads may trust an entry; a full 200 body
is always hashed.  The residual: a writer that changes bytes in place without moving size, inode, mtime or ctime
(a shared mmap before the kernel stamps the file, or a same-tick write on a coarse-grained clock) or silent disk
corruption can be served by a range read for at most the TTL.  Exploiting it needs local write access to the blob
directory or failing hardware, both outside this process's trust boundary; the TTL bounds the exposure and the next
full read or expiry re-verifies.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, NewType

Sha256 = NewType("Sha256", str)

_SHA = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1 << 20
VERIFIED_TTL_SECONDS = 30.0


class InvalidSha(ValueError):
    """Not 64 lowercase hex characters."""


class BlobCorrupt(Exception):
    """Stored bytes no longer hash to their address."""


class BlobAborted(Exception):
    """The caller stopped a hash pass (its client went away); the blob was neither confirmed nor found corrupt."""


class BlobMissing(FileNotFoundError):
    """No blob with that sha."""


def validate_sha(value: str) -> Sha256:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InvalidSha("sha256 must be 64 lowercase hex characters")
    return Sha256(value)


_CACHE_MAX = 4096
_Key = tuple[str, int, int, int, int, int]


def _key(sha: str, st: os.stat_result) -> _Key:
    """Identity of the verified bytes: the file (device, inode), its size and both change times."""
    return (sha, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class VerifiedCache:
    """A bounded, thread-safe set of file identities whose bytes were hashed and found to match their address.

    A hit only ever means "this exact inode, size, mtime and ctime was verified"; a write to the file changes the
    ctime, and a replacement changes the inode, so anything the cache cannot prove unchanged is hashed again."""

    def __init__(
        self,
        limit: int = _CACHE_MAX,
        ttl: float = VERIFIED_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._ttl = ttl
        self._clock = clock
        self._keys: OrderedDict[_Key, float] = (
            OrderedDict()
        )  # key -> monotonic time it was verified
        self._lock = threading.Lock()

    def hit(self, key: _Key) -> bool:
        with self._lock:
            stamp = self._keys.get(key)
            if stamp is None:
                return False
            if self._clock() - stamp > self._ttl:
                del self._keys[key]
                return False
            self._keys.move_to_end(key)
            return True

    def add(self, key: _Key) -> None:
        with self._lock:
            self._keys[key] = self._clock()
            self._keys.move_to_end(key)
            while len(self._keys) > self._limit:
                self._keys.popitem(last=False)

    def forget(self, sha: str) -> None:
        with self._lock:
            for key in [k for k in self._keys if k[0] == sha]:
                del self._keys[key]

    def __len__(self) -> int:
        return len(self._keys)


class BlobStore:
    def __init__(self, root: Path, *, verified_ttl: float = VERIFIED_TTL_SECONDS) -> None:
        self._root = Path(root)
        self._verified = VerifiedCache(ttl=verified_ttl)
        self.aborted = 0  # hash passes stopped by ``should_stop`` (tests and metrics)
        self.verifications = (
            0  # whole-blob hash passes made by open/open_checked/verify_open (tests and metrics)
        )
        self._count_lock = threading.Lock()
        (self._root / "sha256").mkdir(parents=True, exist_ok=True)
        self._tmp = self._root / "tmp"
        self._tmp.mkdir(exist_ok=True)

    @property
    def tmp_dir(self) -> Path:
        """Scratch space on the same volume as the blobs (in-flight uploads are staged beneath it)."""
        return self._tmp

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
        handle, _ = self.open_checked(sha, force=True)
        return handle

    def open_checked(self, sha: str, *, force: bool = False) -> tuple[BinaryIO, bool]:
        """Open a blob; hash it unless this exact file identity was already verified (and ``force`` is false).

        Returns the handle (positioned at 0) and whether a hash pass was made now.  Raises ``BlobMissing`` or
        ``BlobCorrupt``."""
        path = self._path(sha)
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise BlobMissing(sha) from exc
        try:
            hashed = self.verify_open(handle, sha, force=force)
            return handle, hashed
        except BaseException:
            handle.close()
            raise

    def open_handle(self, sha: str) -> BinaryIO:
        """Open a blob WITHOUT hashing it (cheap).  Raises ``BlobMissing``."""
        try:
            return self._path(sha).open("rb")
        except FileNotFoundError as exc:
            raise BlobMissing(sha) from exc

    def is_verified(self, handle: BinaryIO, sha: str) -> bool:
        """True when the exact file identity of ``handle`` is in the verified cache (no hashing, no I/O but fstat)."""
        return self._verified.hit(_key(sha, os.fstat(handle.fileno())))

    def verify_open(
        self,
        handle: BinaryIO,
        sha: str,
        *,
        force: bool = True,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        """Hash the open ``handle`` (unless cached and not ``force``); True when a hash pass was made.  The handle
        is left at offset 0.  The identity is taken from the handle (fstat), never from the path.

        ``should_stop`` is asked before the first chunk and between chunks; when it returns true the pass ends with
        ``BlobAborted`` (a vanished client must not keep a worker hashing a huge blob)."""
        before = _key(sha, os.fstat(handle.fileno()))
        if not force and self._verified.hit(before):
            handle.seek(0)
            return False
        handle.seek(0)
        digest = hashlib.sha256()
        while True:
            if should_stop is not None and should_stop():
                with self._count_lock:
                    self.aborted += 1
                raise BlobAborted(sha)
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        with self._count_lock:
            self.verifications += 1
        if digest.hexdigest() != sha:
            self._verified.forget(sha)
            raise BlobCorrupt(sha)
        if (
            _key(sha, os.fstat(handle.fileno())) == before
        ):  # unchanged while hashing: safe to remember
            self._verified.add(before)
        handle.seek(0)
        return True
