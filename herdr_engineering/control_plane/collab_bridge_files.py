"""The file contract between the control-plane worker and ``collab-public`` for the collab artifact bridge.

Both sides import this module, so it is stdlib-only (plus the stdlib-only ``share_bundle`` and ``ids``) and never pulls in
asyncpg. It defines:

* the protocol constants plan 5 shares (chunk size, chunk body cap, id patterns, ages);
* ``BridgeLimits``, the configurable sanity / anti-abuse limits the worker publishes in ``grants.json``;
* the staging layout (``staging/<owner_key>/<upload_id>/{meta.json, 0000000.., verified, audited}``) and chunk checks;
* intent envelope / commit-change validation and the intent-kind registry;
* safe I/O.

Safe I/O is the security-critical part. ``collab-public`` controls the outbox tree and the worker runs as root, so every
read, write and delete below walks the path one component at a time: each component is opened with
``O_NOFOLLOW`` (``O_DIRECTORY`` for directories) relative to the previous directory fd, and the final operation
(``openat``/``unlinkat``/``renameat``/``linkat``/``mkdirat``) is again fd-relative. No path string is joined or resolved
after the trusted ``root`` is opened, so a component swapped for a symlink between any two steps makes the call fail
(``ELOOP``/``ENOTDIR``) instead of reaching outside the tree. A directory renamed away while its fd is held stays inside
the same mount (``rename`` cannot cross mounts), so the worst a hostile writer achieves is moving the operation to another
place inside its own volume. Every component is checked first (non-empty, no ``/``, no NUL, not ``.``/``..``), because
``O_NOFOLLOW`` guards only the last component of a single open and ``..`` would climb out.

``root`` itself is opened following symlinks: it is trusted deployment configuration, not attacker input. Callers must
pass the trusted mount point (the outbox or mirror volume as mounted) as ``root``, never a subdirectory path under
``collab-public``'s control.

The containment guarantee for a directory renamed away while its fd is held rests on the kernel refusing ``renameat`` /
``linkat`` across mounts (``EXDEV``). It therefore requires the outbox to be a bind mount rooted at the outbox itself,
with no overlapping read-write mount of ``collab-public`` in either direction (no mount of a parent of the outbox and no
mount of a directory inside it). Under that deployment a hostile writer can only move things within its own volume.
"""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import io
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import BinaryIO, Literal

from .share_bundle import ShareBundleError, check_names, normalise_name

# ----------------------------------------------------------------------------- protocol constants (not configuration)

CHUNK_BYTES = 524_288
MCP_UPLOAD_MAX_BODY = 786_432
MAX_MESSAGE = 1000

INTENT_MAX_AGE = 60.0
FEED_MAX_AGE = 120.0

TICKET_RE = r"ict_[0-9a-f]{24}\Z"
UPLOAD_RE = r"upl_[0-9a-f]{24}\Z"
SHA_RE = r"[0-9a-f]{64}\Z"

MAX_CHUNK_INDEX = 9_999_999

META_NAME = "meta.json"
VERIFIED_MARKER = "verified"
AUDITED_MARKER = "audited"
META_MAX_BYTES = 65_536
MAX_TREE_DEPTH = 4  # remove_tree_beneath: an upload dir is flat; anything deeper is hostile (and cannot be swept)
ENTRY_SLACK = 64  # non-part entries an upload dir may hold besides its parts (meta, markers, a few stale tmps)

_TICKET = re.compile(TICKET_RE)
_UPLOAD = re.compile(UPLOAD_RE)
_SHA = re.compile(SHA_RE)
_PART = re.compile(r"[0-9]{7}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")  # owner keys, actor / artifact / version ids
_KIND = re.compile(r"[a-z][a-z0-9_.]{0,47}\Z")
_IDEMPOTENCY = re.compile(r"[\x21-\x7e]{1,128}\Z")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MAX_B64 = 4 * math.ceil(CHUNK_BYTES / 3)
_ACTOR_KINDS = frozenset({"agent", "user"})
_MEMBER_PREFIX = "m_"


class BridgeError(ValueError):
    """A refused bridge input. ``reason`` is a fixed string: it never echoes a name, text or credential."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class IntentError(BridgeError):
    """A refused intent envelope, payload or change."""


# ----------------------------------------------------------------------------- limits

@dataclass(frozen=True)
class BridgeLimits:
    daily_bytes_per_agent: int = 100_000_000_000
    commits_per_minute: int = 30
    chunks_per_minute: int = 1200
    max_changes_per_commit: int = 100
    max_pending_intents_per_agent: int = 4
    max_file_bytes: int = 100_000_000_000
    staging_ttl_seconds: int = 86400
    staging_bytes_per_agent: int = 200_000_000_000
    staging_bytes_total: int = 2_000_000_000_000
    max_text_merge_bytes: int = 2_097_152
    max_conflict_text_bytes: int = 8_388_608
    mirror_history_versions: int = 5
    mirror_max_manifests: int = 100000
    mirror_max_file_bytes: int = 1_073_741_824
    mirror_max_artifact_bytes: int = 17_179_869_184
    apply_concurrency: int = 4

    @classmethod
    def from_mapping(cls, d: dict) -> BridgeLimits:
        """Defaults overridden by ``d``. Unknown keys, non-ints, bools, zero and negatives are refused."""
        if not isinstance(d, dict):
            raise BridgeError("limits must be a mapping")
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise BridgeError(f"unknown limit key(s): {', '.join(sorted(map(str, unknown)))}")
        for key, value in d.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BridgeError(f"limit {key} must be a positive integer")
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- safe filesystem access

def _safe_part(part) -> bool:
    return (isinstance(part, str) and part not in ("", ".", "..") and "/" not in part and "\0" not in part)


def _safe_parts(parts) -> bool:
    return isinstance(parts, list) and all(_safe_part(p) for p in parts)


def _open_root(root: Path) -> int:
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _open_subdir(name: str, dir_fd: int) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)


def _open_beneath(root: Path, parts: list[str], *, max_size: int | None = None) -> tuple[int, os.stat_result] | None:
    """Open ``root/parts...`` without following a symlink at ANY component; a regular file, or None."""
    if not _safe_parts(parts) or not parts:
        return None
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (max_size is not None and st.st_size > max_size):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd, st


def _open_single(root: Path, parts: list[str], max_size: int | None) -> int | None:
    """``_open_beneath`` that also refuses a hard-linked file (``st_nlink != 1``): a file the other side linked in
    from elsewhere is never read as bridge content."""
    opened = _open_beneath(Path(root), parts, max_size=max_size)
    if opened is None:
        return None
    fd, st = opened
    if st.st_nlink != 1:
        os.close(fd)
        return None
    return fd


def _read_capped(fd: int, max_bytes: int) -> bytes | None:
    """At most ``max_bytes`` from ``fd`` (closing it), or None if the file holds more (it may grow after fstat)."""
    try:
        chunks, total = [], 0
        while True:
            block = os.read(fd, min(1 << 20, max_bytes + 1 - total))
            if not block:
                return b"".join(chunks)
            chunks.append(block)
            total += len(block)
            if total > max_bytes:
                return None
    except OSError:
        return None
    finally:
        os.close(fd)


def _no_constants(value: str):
    raise ValueError("NaN/Infinity are not JSON")


def read_bytes_beneath(root: Path, parts: list[str], *, max_bytes: int) -> bytes | None:
    """The bytes of the regular, single-linked file ``root/parts...`` (no symlink anywhere), at most ``max_bytes``."""
    fd = _open_single(root, parts, max_bytes)
    return None if fd is None else _read_capped(fd, max_bytes)


def read_json_beneath(root: Path, parts: list[str], *, max_bytes: int) -> dict | None:
    """``read_bytes_beneath`` parsed as a JSON object; None on any failure (missing, unsafe, oversize, invalid)."""
    data = read_bytes_beneath(root, parts, max_bytes=max_bytes)
    if data is None:
        return None
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_no_constants)
        # a lone surrogate parses but can never be re-serialised as UTF-8: refuse it here, once
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError, UnicodeError):
        return None
    return obj if isinstance(obj, dict) else None


class _BoundedReader(io.RawIOBase):
    """A raw reader on ``fd`` that raises ``BridgeError`` as soon as more than ``limit`` bytes come out of the file,
    so a file that grows after the open (or whose fstat under-reports) is never consumed past its cap."""

    def __init__(self, fd: int, limit: int):
        super().__init__()
        self._fd, self._limit, self._count = fd, limit, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        if not len(view):
            return 0
        want = max(1, min(len(view), self._limit + 1 - self._count))
        data = os.read(self._fd, want)
        self._count += len(data)
        if self._count > self._limit:
            raise BridgeError("file grew past its size cap")
        view[:len(data)] = data
        return len(data)

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if not self.closed:
            try:
                os.close(self._fd)
            finally:
                super().close()


def open_beneath(root: Path, parts: list[str], *, max_bytes: int | None = None) -> BinaryIO | None:
    """A streaming reader on ``root/parts...`` with the same checks as ``read_bytes_beneath``; the caller closes it.

    With ``max_bytes`` the file must be at most that size at open, and reading raises ``BridgeError`` once more than
    ``max_bytes`` bytes have come out (the file grew after the open), so the cap holds for the whole read.
    """
    fd = _open_single(root, parts, max_bytes)
    if fd is None:
        return None
    try:
        if max_bytes is not None:
            return _BoundedReader(fd, max_bytes)
        return os.fdopen(fd, "rb")
    except (OSError, ValueError):
        os.close(fd)
        return None


# ----------------------------------------------------------------------------- writes by the owning side

def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _tmp_name() -> str:
    return ".tmp-" + secrets.token_hex(8)


def _ensure_dir(path: Path) -> int:
    """An fd on ``path`` (trusted: the writer's own tree), creating missing directories with mode 0755."""
    path = Path(path)
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for d in reversed(missing):
        try:
            d.mkdir()
        except FileExistsError:
            continue
        os.chmod(d, 0o755)
    return _open_root(path)


def _create_tmp(dfd: int, data: bytes, mode: int) -> str:
    """Write ``data`` into a fresh ``.tmp-<rand>`` in ``dfd`` (fsynced, ``mode`` regardless of umask); its name."""
    tmp = _tmp_name()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        try:
            _write_all(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _unlink_quiet(tmp, dfd)
        raise
    return tmp


def _unlink_quiet(name: str, dfd: int) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def atomic_write_bytes(dir: Path, name: str, data: bytes, *, mode: int = 0o644) -> None:
    """Write ``dir/name`` all-or-nothing (tmp, fsync, ``renameat`` within the dir fd). For the writer's own tree."""
    if not _safe_part(name) or name.startswith(".tmp-"):
        raise BridgeError("unsafe file name")
    dfd = _ensure_dir(dir)
    try:
        tmp = _create_tmp(dfd, bytes(data), mode)
        try:
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            _unlink_quiet(tmp, dfd)
            raise
        os.fsync(dfd)
    finally:
        os.close(dfd)


def atomic_write_json(dir: Path, name: str, obj: dict, *, mode: int = 0o644) -> None:
    data = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
    atomic_write_bytes(dir, name, data, mode=mode)


_TMPFILE_FALLBACK = (errno.EOPNOTSUPP, errno.EISDIR, errno.EINVAL)


def _open_tmpfile(dfd: int) -> int:
    """An unnamed ``O_TMPFILE`` in the directory ``dfd`` (raises ``OSError`` where unsupported, e.g. non-Linux)."""
    flag = getattr(os, "O_TMPFILE", None)
    if flag is None:
        raise OSError(errno.EOPNOTSUPP, "O_TMPFILE is not available")
    return os.open(".", flag | os.O_WRONLY | os.O_CLOEXEC, 0o600, dir_fd=dfd)


def _link_tmpfile(dfd: int, name: str, data: bytes, mode: int) -> bool | None:
    """The ``O_TMPFILE`` path of ``link_new_bytes``: True/False as it returns, or None when unsupported (fall back).

    The file has no name until ``linkat`` gives it its final one, so there is never a ``.tmp-*`` leftover and never an
    ``nlink == 2`` moment; a crash at any point leaves either nothing or the complete file.
    """
    try:
        fd = _open_tmpfile(dfd)
    except OSError as exc:
        if exc.errno in _TMPFILE_FALLBACK:
            return None
        raise
    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
        try:
            os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=dfd, follow_symlinks=True)
        except FileExistsError:
            return False
        except FileNotFoundError:  # no /proc: the unnamed file is discarded on close
            return None
    finally:
        os.close(fd)
    os.fsync(dfd)
    return True


def link_new_bytes(dir: Path, name: str, data: bytes, *, mode: int = 0o600) -> bool:
    """Create ``dir/name`` with ``data`` unless it exists (False then; never replaced, never partial).

    Uses ``O_TMPFILE`` + ``linkat`` where available. The fallback (no ``O_TMPFILE`` or no ``/proc``) writes a
    ``.tmp-<rand>`` and links it: a crash there can leave a stale ``.tmp-*`` (ignored by ``list_parts`` and the read
    helpers, removed by the staging sweep) and the final name briefly has ``nlink == 2``.
    """
    if not _safe_part(name) or name.startswith(".tmp-"):
        raise BridgeError("unsafe file name")
    data = bytes(data)
    dfd = _ensure_dir(dir)
    try:
        linked = _link_tmpfile(dfd, name, data, mode)
        if linked is not None:
            return linked
        tmp = _create_tmp(dfd, data, mode)
        try:
            os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
        except FileExistsError:
            return False
        finally:
            _unlink_quiet(tmp, dfd)
        os.fsync(dfd)
        return True
    finally:
        os.close(dfd)


# ----------------------------------------------------------------------------- root-safe mutation in a hostile tree

def _walk_dirs(root: Path, parts: list[str]) -> int | None:
    """An fd on ``root/parts...``, every component opened ``O_DIRECTORY|O_NOFOLLOW`` from the previous fd; or None."""
    if not _safe_parts(parts):
        return None
    try:
        dfd = _open_root(Path(root))
    except OSError:
        return None
    try:
        for part in parts:
            nfd = _open_subdir(part, dfd)
            os.close(dfd)
            dfd = nfd
    except OSError:
        os.close(dfd)
        return None
    return dfd


def _scan_names(dfd: int, cap: int) -> list[str]:
    """The entry names of the directory ``dfd``; ``BridgeError`` as soon as there are more than ``cap`` (the scan
    stops there, so a directory flooded with empty files costs at most ``cap`` names of memory)."""
    names = []
    with os.scandir(dfd) as it:
        for entry in it:
            names.append(entry.name)
            if len(names) > cap:
                raise BridgeError("too many entries")
    return names


def list_beneath(root: Path, parts: list[str], *, max_entries: int) -> list[tuple[str, os.stat_result]] | None:
    """The entries of the real directory ``root/parts...`` with their no-follow ``lstat`` (sorted by name).

    Symlinks, FIFOs and the like are reported as such (``S_ISLNK``/``S_ISFIFO``) so callers can skip them; an entry that
    vanishes while listing is left out. None if any component is unsafe, a symlink or not a directory;
    ``BridgeError("too many entries")`` past ``max_entries`` (fail closed: the scan stops there).

    Empty files cost no quota bytes, so ``max_entries`` is the only bound on what a hostile writer can make the worker
    hold; callers derive it from their own limits, for example ``intents/``: a few thousand (agents ×
    ``max_pending_intents_per_agent`` plus slack); ``staging/``: the number of staging owners plus slack;
    ``staging/<owner>/``: the uploads one owner can have within ``staging_bytes_per_agent`` (a few thousand).
    An upload dir is listed with ``list_parts``, which is bounded by the upload's own part count.
    """
    dfd = _walk_dirs(root, parts)
    if dfd is None:
        return None
    try:
        out = []
        for name in _scan_names(dfd, max_entries):
            try:
                out.append((name, os.stat(name, dir_fd=dfd, follow_symlinks=False)))
            except FileNotFoundError:
                continue
        return sorted(out, key=lambda item: item[0])
    except OSError:
        return None
    finally:
        os.close(dfd)


def write_marker_beneath(root: Path, parts: list[str], name: str, data: bytes = b"", *, mode: int = 0o644) -> bool:
    """Create ``root/parts.../name`` exclusively; False if it exists or any component is unsafe (never raises)."""
    if not _safe_part(name):
        return False
    dfd = _walk_dirs(root, parts)
    if dfd is None:
        return False
    try:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        except OSError:
            return False
        try:
            _write_all(fd, bytes(data))
            os.fchmod(fd, mode)
            os.fsync(fd)
        except OSError:
            os.close(fd)
            _unlink_quiet(name, dfd)
            return False
        os.close(fd)
        try:
            os.fsync(dfd)
        except OSError:
            pass
        return True
    finally:
        os.close(dfd)


def unlink_beneath(root: Path, parts: list[str], name: str) -> bool:
    """``unlinkat`` the regular file ``root/parts.../name``; False for anything else (a symlink is left alone).

    A swap between the type check and the unlink is harmless: ``unlinkat`` never follows a link (it would remove the
    link itself, inside the tree) and refuses a directory.
    """
    if not _safe_part(name):
        return False
    dfd = _walk_dirs(root, parts)
    if dfd is None:
        return False
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            return False
        os.unlink(name, dir_fd=dfd)
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)


def _empty_dir(dfd: int, depth: int, budget: list[int]) -> bool:
    """Remove everything inside the directory ``dfd``; recursion goes only through fds opened no-follow.

    ``budget`` (a one-item list) is the number of entries the whole walk may still scan; past it the walk stops and
    the directory counts as not emptied (False). An over-deep or over-full tree is never swept: it stays, inside the
    volume, and the sweep reports False.
    """
    if depth > MAX_TREE_DEPTH:
        return False
    ok = True
    try:
        names = _scan_names(dfd, budget[0])
    except BridgeError:
        return False
    budget[0] -= len(names)
    for child in names:
        try:
            st = os.stat(child, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            ok = False
            continue
        if stat.S_ISDIR(st.st_mode):
            try:
                cfd = _open_subdir(child, dfd)
            except OSError:  # swapped for a link or file since the stat: remove that entry itself, never its target
                try:
                    os.unlink(child, dir_fd=dfd)
                except OSError:
                    ok = False
                continue
            try:
                inner = _empty_dir(cfd, depth + 1, budget)
            finally:
                os.close(cfd)
            if not inner:
                ok = False
                continue
            try:
                os.rmdir(child, dir_fd=dfd)
            except OSError:
                ok = False
        else:
            try:
                os.unlink(child, dir_fd=dfd)
            except FileNotFoundError:
                pass
            except OSError:
                ok = False
    return ok


def max_parts_for(limits: BridgeLimits) -> int:
    """The most parts a valid upload can have under ``limits``."""
    return min(_total_for(limits.max_file_bytes), MAX_CHUNK_INDEX + 1)


def tree_entry_cap(limits: BridgeLimits) -> int:
    """The entry budget for sweeping one upload dir under ``limits``: its largest part count plus ``ENTRY_SLACK``."""
    return max_parts_for(limits) + ENTRY_SLACK


def remove_tree_beneath(root: Path, parts: list[str], *, max_entries: int | None = None) -> bool:
    """Delete the real directory ``root/parts...`` and everything in it, never following a link.

    False (with whatever could be removed safely removed) if any component is unsafe, a symlink or not a directory,
    the tree is deeper than ``MAX_TREE_DEPTH``, holds more than ``max_entries`` entries in all (default:
    ``tree_entry_cap`` of the default limits; callers pass ``tree_entry_cap(limits)`` or a bound of their own), or
    something could not be removed. Links inside are removed as links.
    """
    budget = [tree_entry_cap(BridgeLimits()) if max_entries is None else max_entries]
    if not _safe_parts(parts) or not parts:
        return False
    parent = _walk_dirs(root, parts[:-1])
    if parent is None:
        return False
    try:
        try:
            dfd = _open_subdir(parts[-1], parent)
        except OSError:
            return False
        try:
            ok = _empty_dir(dfd, 1, budget)
        except (OSError, RecursionError):
            ok = False
        finally:
            os.close(dfd)
        if not ok:
            return False
        try:
            os.rmdir(parts[-1], dir_fd=parent)
        except OSError:
            return False
        return True
    finally:
        os.close(parent)


# ----------------------------------------------------------------------------- staging layout

@dataclass(frozen=True)
class Actor:
    kind: Literal["agent", "user"]
    id: str


def _check_id(value, what: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise BridgeError("invalid " + what)
    return value


def agent_owner_key(agent_id: str) -> str:
    """The staging owner key of a collaborator agent: its id. ``m_`` is reserved for plan 5's member keys."""
    _check_id(agent_id, "agent id")
    if agent_id.startswith(_MEMBER_PREFIX):
        raise BridgeError("agent id uses the reserved member prefix")
    return agent_id


def _check_sha(value, what: str) -> str:
    if not isinstance(value, str) or not _SHA.match(value):
        raise BridgeError("invalid " + what)
    return value


def _check_size(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BridgeError("invalid size_bytes")
    return value


def upload_id_for(owner_key: str, artifact_id: str, name: str, file_sha256: str, size_bytes: int) -> str:
    """The derived (never chosen) upload id. Only ``name`` may contain ``|``, so the pre-image is unambiguous."""
    _check_id(owner_key, "owner key")
    _check_id(artifact_id, "artifact id")
    _check_sha(file_sha256, "file_sha256")
    _check_size(size_bytes)
    try:
        name = normalise_name(name)
    except ShareBundleError:
        raise BridgeError("invalid file name") from None
    pre = "|".join((owner_key, artifact_id, name, file_sha256, str(size_bytes)))
    return "upl_" + hashlib.sha256(pre.encode("utf-8")).hexdigest()[:24]


def staging_parts(owner_key: str, upload_id: str) -> list[str]:
    _check_id(owner_key, "owner key")
    if not isinstance(upload_id, str) or not _UPLOAD.match(upload_id):
        raise BridgeError("invalid upload id")
    return ["staging", owner_key, upload_id]


def staging_path(root: Path, owner_key: str, upload_id: str) -> Path:
    return Path(root).joinpath(*staging_parts(owner_key, upload_id))


def _total_for(size_bytes: int) -> int:
    return -(-size_bytes // CHUNK_BYTES)


@dataclass(frozen=True)
class StagedUpload:
    upload_id: str
    owner_key: str
    artifact_id: str
    name: str
    size_bytes: int
    file_sha256: str
    total: int
    created_at: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> StagedUpload:
        """A ``meta.json`` checked against its own identity: ``upload_id`` must be the one its fields derive."""
        if not isinstance(d, dict) or set(d) != {f.name for f in fields(cls)}:
            raise BridgeError("invalid upload metadata")
        size = _check_size(d["size_bytes"])
        upload_id = upload_id_for(d["owner_key"], d["artifact_id"], d["name"], d["file_sha256"], size)
        if d["upload_id"] != upload_id or d["name"] != normalise_name(d["name"]):
            raise BridgeError("invalid upload metadata")
        total = d["total"]
        if isinstance(total, bool) or total != _total_for(size):
            raise BridgeError("invalid upload metadata")
        created = d["created_at"]
        if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
            raise BridgeError("invalid upload metadata")
        return cls(upload_id=upload_id, owner_key=d["owner_key"], artifact_id=d["artifact_id"], name=d["name"],
                   size_bytes=size, file_sha256=d["file_sha256"], total=total, created_at=float(created))


def read_staged_meta(root: Path, owner_key: str, upload_id: str) -> StagedUpload | None:
    """The validated ``meta.json`` of an upload, bound to the ``owner_key``/``upload_id`` it was looked up by."""
    try:
        parts = staging_parts(owner_key, upload_id)
    except BridgeError:
        return None
    raw = read_json_beneath(root, [*parts, META_NAME], max_bytes=META_MAX_BYTES)
    if raw is None:
        return None
    try:
        meta = StagedUpload.from_dict(raw)
    except (BridgeError, ShareBundleError):
        return None
    if meta.owner_key != owner_key or meta.upload_id != upload_id:
        return None
    return meta


def list_parts(root: Path, owner_key: str, upload_id: str, *, max_parts: int) -> list[int]:
    """The sorted indices below ``max_parts`` of the regular, single-linked part files in an upload dir (no symlink,
    dir, FIFO or stale ``.tmp-*`` counts).

    ``max_parts`` is the upload's validated ``meta.total`` (or ``max_parts_for(limits)``). The scan stops with
    ``BridgeError("too many entries")`` once the dir holds more than ``max_parts + ENTRY_SLACK`` entries; only names
    shaped like a part are stat'ed and only their integer indices are kept.
    """
    try:
        parts = staging_parts(owner_key, upload_id)
    except BridgeError:
        return []
    if isinstance(max_parts, bool) or not isinstance(max_parts, int) or max_parts < 0:
        raise BridgeError("invalid part bound")
    dfd = _walk_dirs(root, parts)
    if dfd is None:
        return []
    cap = max_parts + ENTRY_SLACK
    try:
        seen, out = 0, []
        with os.scandir(dfd) as it:
            for entry in it:
                seen += 1
                if seen > cap:
                    raise BridgeError("too many entries")
                name = entry.name
                if not _PART.match(name) or int(name) >= max_parts:
                    continue
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                    out.append(int(name))
        out.sort()
        return out
    except OSError:
        return []
    finally:
        os.close(dfd)


def upload_complete(root: Path, owner_key: str, upload_id: str) -> bool:
    """Every index ``0..total-1`` is present, or the worker's ``verified`` marker is.

    The marker is a hint for ``collab-public`` only; the worker never trusts it and re-verifies the bytes.
    """
    try:
        parts = staging_parts(owner_key, upload_id)
    except BridgeError:
        return False
    marker = _open_beneath(Path(root), [*parts, VERIFIED_MARKER], max_size=META_MAX_BYTES)
    if marker is not None:
        os.close(marker[0])
        return True
    meta = read_staged_meta(root, owner_key, upload_id)
    if meta is None:
        return False
    try:
        present = set(list_parts(root, owner_key, upload_id, max_parts=meta.total))
    except BridgeError:
        return False
    return all(i in present for i in range(meta.total))


@dataclass(frozen=True)
class Chunk:
    artifact_id: str
    name: str
    size_bytes: int
    file_sha256: str
    total: int
    index: int
    data: bytes


_CHUNK_KEYS = frozenset({"artifact_id", "name", "size_bytes", "file_sha256", "total", "index", "data", "sha256"})


def _strict_b64(value) -> bytes:
    if not isinstance(value, str) or len(value) > _MAX_B64:
        raise BridgeError("chunk data is not valid base64")
    try:
        raw = value.encode("ascii")
        data = base64.b64decode(raw, validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        raise BridgeError("chunk data is not valid base64") from None
    if base64.b64encode(data) != raw:
        raise BridgeError("chunk data is not canonical base64")
    return data


def validate_chunk(args: dict, limits: BridgeLimits) -> Chunk:
    """A staged-upload chunk, fully checked; ``BridgeError`` otherwise."""
    if not isinstance(args, dict) or set(args) != _CHUNK_KEYS:
        raise BridgeError("chunk arguments are missing or unknown")
    artifact_id = _check_id(args["artifact_id"], "artifact id")
    size = args["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= limits.max_file_bytes:
        raise BridgeError("size_bytes is out of range")
    total = args["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total != _total_for(size) \
            or total > MAX_CHUNK_INDEX + 1:
        raise BridgeError("total does not match size_bytes")
    index = args["index"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < total:
        raise BridgeError("index is out of range")
    file_sha = _check_sha(args["file_sha256"], "file_sha256")
    chunk_sha = _check_sha(args["sha256"], "chunk sha256")
    try:
        name = check_names([args["name"]])[0]
    except ShareBundleError:
        raise BridgeError("invalid file name") from None
    data = _strict_b64(args["data"])
    expected = CHUNK_BYTES if index < total - 1 else size - CHUNK_BYTES * (total - 1)
    if len(data) != expected:
        raise BridgeError("chunk has the wrong length")
    if hashlib.sha256(data).hexdigest() != chunk_sha:
        raise BridgeError("chunk sha256 does not match its data")
    return Chunk(artifact_id=artifact_id, name=name, size_bytes=size, file_sha256=file_sha, total=total, index=index,
                 data=data)


# ----------------------------------------------------------------------------- intents

@dataclass(frozen=True)
class Change:
    name: str
    op: Literal["put", "delete"]
    text: str | None
    upload_id: str | None


@dataclass(frozen=True)
class CommitPayload:
    base_version_id: str
    message: str
    changes: tuple[Change, ...]
    inline_bytes: int


@dataclass(frozen=True)
class Intent:
    ticket: str
    kind: str
    actor: Actor
    artifact_id: str | None
    idempotency_key: str | None
    submitted_at: float
    payload: object
    request_sha256: str


@dataclass(frozen=True)
class _KindSpec:
    validate_payload: Callable[[dict, BridgeLimits], object]
    actors: frozenset[str]
    requires_artifact: bool


_INTENT_KINDS: dict[str, _KindSpec] = {}


def register_intent_kind(kind: str, validate_payload: Callable[[dict, BridgeLimits], object], *,
                         actors: frozenset[str], requires_artifact: bool = False) -> None:
    """Register an intent kind; registering one twice raises. ``actors`` are the actor kinds allowed to send it."""
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise ValueError("invalid intent kind name")
    actors = frozenset(actors)
    if not actors or not actors <= _ACTOR_KINDS:
        raise ValueError("intent kind actors must be a non-empty subset of {'agent', 'user'}")
    if not callable(validate_payload):
        raise ValueError("validate_payload must be callable")
    if kind in _INTENT_KINDS:
        raise ValueError("intent kind is already registered")
    _INTENT_KINDS[kind] = _KindSpec(validate_payload, actors, bool(requires_artifact))


def new_ticket() -> str:
    return "ict_" + secrets.token_hex(12)


_ENVELOPE_KEYS = frozenset({"ticket", "kind", "actor", "artifact_id", "idempotency_key", "submitted_at", "payload"})
_ENVELOPE_REQUIRED = frozenset({"ticket", "kind", "actor", "submitted_at", "payload"})


def request_digest(raw: dict) -> str:
    """sha256 of the canonical ``{kind, actor, artifact_id, payload}``: the same request with a new ticket matches."""
    body = {"kind": raw.get("kind"), "actor": raw.get("actor"), "artifact_id": raw.get("artifact_id"),
            "payload": raw.get("payload")}
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise IntentError("intent is not canonical JSON") from None


def _validate_actor(raw) -> Actor:
    if not isinstance(raw, dict) or set(raw) != {"kind", "id"} or raw["kind"] not in _ACTOR_KINDS:
        raise IntentError("invalid actor")
    try:
        actor_id = agent_owner_key(raw["id"]) if raw["kind"] == "agent" else _check_id(raw["id"], "actor id")
    except BridgeError:
        raise IntentError("invalid actor") from None
    return Actor(raw["kind"], actor_id)


def validate_intent(raw: dict, *, filename: str, limits: BridgeLimits) -> Intent:
    """A checked intent envelope, its payload validated by its kind's validator; ``IntentError`` otherwise.

    The age check (``INTENT_MAX_AGE``) is the caller's: it depends on whether the ticket was already received.
    """
    if not isinstance(raw, dict):
        raise IntentError("intent is not an object")
    if not set(raw) <= _ENVELOPE_KEYS or not _ENVELOPE_REQUIRED <= set(raw):
        raise IntentError("intent has missing or unknown keys")
    ticket = raw["ticket"]
    if not isinstance(ticket, str) or not _TICKET.match(ticket):
        raise IntentError("invalid ticket")
    if filename != ticket + ".json":
        raise IntentError("intent file name does not match its ticket")
    actor = _validate_actor(raw["actor"])
    spec = _INTENT_KINDS.get(raw["kind"]) if isinstance(raw["kind"], str) else None
    if spec is None or actor.kind not in spec.actors:
        raise IntentError("unknown intent kind")
    artifact_id = raw.get("artifact_id")
    if artifact_id is not None:
        try:
            _check_id(artifact_id, "artifact id")
        except BridgeError:
            raise IntentError("invalid artifact id") from None
    elif spec.requires_artifact:
        raise IntentError("intent kind requires an artifact id")
    key = raw.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not _IDEMPOTENCY.match(key)):
        raise IntentError("invalid idempotency key")
    submitted = raw["submitted_at"]
    if isinstance(submitted, bool) or not isinstance(submitted, (int, float)) or not math.isfinite(submitted) \
            or submitted <= 0:
        raise IntentError("invalid submitted_at")
    digest = request_digest(raw)
    try:
        payload = spec.validate_payload(raw["payload"], limits)
    except IntentError:
        raise
    except (BridgeError, ShareBundleError, TypeError, ValueError, KeyError):
        raise IntentError("invalid intent payload") from None
    return Intent(ticket=ticket, kind=raw["kind"], actor=actor, artifact_id=artifact_id, idempotency_key=key,
                  submitted_at=float(submitted), payload=payload, request_sha256=digest)


_CHANGE_KEYS = frozenset({"name", "op", "text", "upload_id"})


def _check_text(text) -> int:
    """The UTF-8 size of inline ``text``; C0 controls other than tab/LF/CR and lone surrogates are refused."""
    if not isinstance(text, str):
        raise IntentError("change text must be a string")
    if _TEXT_CONTROL.search(text):
        raise IntentError("change text contains a control character")
    try:
        return len(text.encode("utf-8"))
    except UnicodeError:
        raise IntentError("change text is not valid UTF-8") from None


def validate_changes(changes: list, limits: BridgeLimits) -> tuple[Change, ...]:
    """Commit changes as both sides check them. Content identity never comes from the caller: only inline text or a
    staged upload's id are accepted (no sha256, storage key, entry, size or media type)."""
    if not isinstance(changes, list) or not changes:
        raise IntentError("changes must be a non-empty list")
    if len(changes) > limits.max_changes_per_commit:
        raise IntentError("too many changes")
    out = []
    for change in changes:
        if not isinstance(change, dict):
            raise IntentError("a change must be an object")
        if not set(change) <= _CHANGE_KEYS:
            raise IntentError("a change has an unknown or forbidden key")
        if "name" not in change or not isinstance(change["name"], str):
            raise IntentError("a change has no name")
        op = change.get("op")
        has_text, has_upload = "text" in change, "upload_id" in change
        text = upload_id = None
        if op == "put":
            if has_text == has_upload:
                raise IntentError("a put needs exactly one of text and upload_id")
            if has_text:
                _check_text(change["text"])
                text = change["text"]
            else:
                upload_id = change["upload_id"]
                if not isinstance(upload_id, str) or not _UPLOAD.match(upload_id):
                    raise IntentError("invalid upload id")
        elif op == "delete":
            if has_text or has_upload:
                raise IntentError("a delete carries no content")
        else:
            raise IntentError("a change op must be put or delete")
        out.append((change["name"], op, text, upload_id))
    try:
        names = check_names([name for name, *_ in out])
    except ShareBundleError:
        raise IntentError("invalid or clashing file names") from None
    return tuple(Change(name, op, text, upload_id) for name, (_, op, text, upload_id) in zip(names, out))


_COMMIT_KEYS = frozenset({"base_version_id", "message", "changes"})


def validate_commit_payload(payload: dict, limits: BridgeLimits) -> CommitPayload:
    if not isinstance(payload, dict) or not set(payload) <= _COMMIT_KEYS \
            or not {"base_version_id", "changes"} <= set(payload):
        raise IntentError("commit payload has missing or unknown keys")
    try:
        base = _check_id(payload["base_version_id"], "base version id")
    except BridgeError:
        raise IntentError("invalid base version id") from None
    message = payload.get("message", "")
    if not isinstance(message, str) or len(message) > MAX_MESSAGE:
        raise IntentError("invalid commit message")
    _check_text(message)
    changes = validate_changes(payload["changes"], limits)
    inline = sum(len(c.text.encode("utf-8")) for c in changes if c.text is not None)
    return CommitPayload(base_version_id=base, message=message, changes=changes, inline_bytes=inline)


register_intent_kind("commit", validate_commit_payload, actors=frozenset({"agent"}), requires_artifact=True)

__all__ = [
    "Actor", "agent_owner_key", "atomic_write_bytes", "atomic_write_json", "AUDITED_MARKER", "BridgeError",
    "BridgeLimits", "Change", "Chunk", "CHUNK_BYTES", "CommitPayload", "ENTRY_SLACK", "FEED_MAX_AGE", "Intent",
    "INTENT_MAX_AGE", "IntentError", "link_new_bytes", "list_beneath", "list_parts", "MAX_CHUNK_INDEX",
    "MAX_MESSAGE", "max_parts_for", "MAX_TREE_DEPTH", "MCP_UPLOAD_MAX_BODY", "META_MAX_BYTES", "META_NAME",
    "new_ticket", "open_beneath", "read_bytes_beneath", "read_json_beneath", "read_staged_meta",
    "register_intent_kind", "remove_tree_beneath", "request_digest", "SHA_RE", "StagedUpload", "staging_parts",
    "staging_path", "TICKET_RE", "tree_entry_cap", "unlink_beneath", "upload_complete", "upload_id_for",
    "UPLOAD_RE", "validate_changes", "validate_chunk", "validate_commit_payload", "validate_intent",
    "VERIFIED_MARKER", "write_marker_beneath",
]
