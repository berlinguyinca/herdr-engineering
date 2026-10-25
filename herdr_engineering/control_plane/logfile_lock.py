"""Who may touch the collaboration JSON-lines log: one advisory lock shared by the server (``FileEventLog``) and
``collab-repair``.

A server holds the log SHARED for its life; the repair needs it EXCLUSIVE, so a repair can never swap the file under
an open append descriptor. Both open the file read-write (NFS lock emulation needs a write-open for an exclusive lock
and a read-open for a shared one), and re-check after locking that the path still names the file they locked.
Where ``flock`` is unsupported (some NFS/FUSE mounts: ENOLCK, ENOSYS, EOPNOTSUPP) a cooperative ``<log>.lock`` file
naming ``<host> <pid>`` takes over, with a loud log line. A lock written on another host is always treated as live (its
pid cannot be checked from here); one naming this host and this very pid, but not held by this process, is stale (a containerised server restarts as
PID 1 every time and must start); a legacy pid-only file is read as this host. ``acquire`` never raises for "unsupported": it returns a ``Held`` whose
``mode`` says what protection exists, so a server still starts and the repair can decide to refuse.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import socket

log = logging.getLogger(__name__)
_flock = fcntl.flock                      # test seam
_HELD: set[str] = set()                  # lock files THIS process holds (our own pid in one we do not hold is stale)
UNSUPPORTED = {errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL, errno.EBADF}


class Busy(Exception):
    """Another process holds the log in a conflicting way."""


class Held:
    def __init__(self, fd: int | None, mode: str, lockfile: str | None = None):
        self.fd, self.mode, self.lockfile = fd, mode, lockfile      # mode: flock | lockfile | none

    def release(self) -> None:
        if self.lockfile:
            _HELD.discard(self.lockfile)
            try:
                os.unlink(self.lockfile)
            except OSError:
                pass
            self.lockfile = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_owner(lock: str) -> tuple[str | None, int]:
    """(host, pid) from a lock file; host None = a legacy pid-only file (written on this host)."""
    try:
        parts = open(lock).read().split()
    except OSError:
        return None, 0
    try:
        if len(parts) == 1:
            return None, int(parts[0])
        if len(parts) == 2:
            return parts[0], int(parts[1])
    except ValueError:
        pass
    return None, 0


def _take_lockfile(path: str) -> str | None:
    lock = path + ".lock"
    me = socket.gethostname()
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            host, pid = _read_owner(lock)
            if host is not None and host != me:
                raise Busy(f"{lock} is held by process {pid} on host {host}; if that host is gone, remove the "
                           "file by hand") from None
            if pid == os.getpid() and lock in _HELD:
                raise Busy(f"{lock} is held by this process") from None
            if pid and pid != os.getpid() and _alive(pid):
                raise Busy(f"{lock} is held by running process {pid}") from None
            try:
                os.unlink(lock)             # stale
            except OSError:
                return None
            continue
        except OSError:
            return None
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{me} {os.getpid()}")
        _HELD.add(lock)
        return lock
    return None


def acquire(path: str, *, exclusive: bool, create: bool = False) -> Held:
    """Lock ``path``. Raises ``Busy`` on a real conflict. The returned fd is the locked descriptor (the writer may
    append through it)."""
    flags = os.O_RDWR | (os.O_APPEND if not exclusive else 0) | (os.O_CREAT if create else 0)
    for _ in range(5):
        fd = os.open(path, flags, 0o600)
        try:
            _flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Busy(f"{path} is locked by another process") from None
        except OSError as exc:
            os.close(fd)
            if exc.errno not in UNSUPPORTED:
                raise
            log.error("flock is not supported for %s (%s): falling back to the cooperative %s.lock file", path,
                      os.strerror(exc.errno), path)
            lockfile = _take_lockfile(path)
            if lockfile is None:
                log.error("could not create %s.lock either: the collaboration log is NOT protected against a "
                          "concurrent collab-repair", path)
                return Held(os.open(path, flags, 0o600), "none") if not exclusive else Held(None, "none")
            return Held(os.open(path, flags, 0o600), "lockfile", lockfile)
        try:
            same = os.fstat(fd).st_ino == os.stat(path).st_ino and os.fstat(fd).st_dev == os.stat(path).st_dev
        except OSError:
            same = False
        if same:
            return Held(fd, "flock")
        os.close(fd)                        # the path was replaced (a repair finished) between open and lock: retry
    raise Busy(f"{path} keeps being replaced")
