"""Judge API key registration: write the key file safely, prove it works, never echo it.

The key arrives in memory only (stdin in the CLI).  It is written to ``PATH.new`` (``O_EXCL``, mode 0600), health-checked
by a real ``GET /v1/models`` with that very file, and only then moved to ``PATH``; a failure removes ``PATH.new`` and
leaves any existing ``PATH`` untouched.  Errors carry a short code and never a response body or the key.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .jev import JevClient, JudgeUnavailable

__all__ = ["MODELS_SHOWN", "RegisterError", "Registered", "fingerprint", "register_key"]

MODELS_SHOWN = 10
MAX_KEY_CHARS = 4096
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class RegisterError(Exception):
    """A refusal with a short code; ``seen`` holds model ids (not secret) for ``model_not_listed``."""

    def __init__(self, code: str, seen: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.seen = seen


@dataclass(frozen=True)
class Registered:
    fingerprint: str
    model: str
    models_seen: int


def fingerprint(key: str) -> str:
    """First 8 hex of sha256 of the key: all that may ever be shown.  Computed on demand, never stored."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _check_key(key: str) -> str:
    key = key.strip()
    if not key or len(key) > MAX_KEY_CHARS or not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        raise RegisterError("bad_key")
    return key


def _check_endpoint(endpoint: str) -> None:
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
    except ValueError:
        raise RegisterError("config") from None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password or parts.query or parts.fragment:
        raise RegisterError("config")
    if parts.scheme == "http" and host not in _LOOPBACK:
        raise RegisterError("config")          # a bearer key must not cross the network in clear text


def _check_target(path: Path) -> bool:
    """Validate the destination; returns True when ``path`` already exists (a rotation)."""
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise RegisterError("bad_path")
    try:
        parent = os.stat(path.parent)
    except OSError:
        raise RegisterError("bad_path") from None
    if not stat.S_ISDIR(parent.st_mode):
        raise RegisterError("bad_path")
    if parent.st_mode & 0o022:                  # writable by group or others (no sticky-bit exception)
        raise RegisterError("unsafe_dir")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        raise RegisterError("bad_path") from None
    if stat.S_ISLNK(st.st_mode):
        raise RegisterError("symlink")
    if not stat.S_ISREG(st.st_mode):
        raise RegisterError("bad_path")
    return True


def _write_new(tmp: Path, key: str) -> None:
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise RegisterError("tmp_exists") from None
    except OSError:
        raise RegisterError("io") from None
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:      # takes ownership of fd, closing it on exit
            handle.write(key + "\n")
    except BaseException as exc:                                   # incl. KeyboardInterrupt: never leave PATH.new behind
        _unlink(tmp)
        if isinstance(exc, OSError):
            raise RegisterError("io") from None
        raise


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def register_key(endpoint: str, key_file: str, key: str, model: str, timeout: float = 10.0) -> Registered:
    key = _check_key(key)
    _check_endpoint(endpoint)
    if not model:
        raise RegisterError("config")
    path = Path(key_file)
    rotating = _check_target(path)
    tmp = path.with_name(path.name + ".new")
    _write_new(tmp, key)
    try:
        try:
            ids = JevClient(endpoint, str(tmp), model, timeout).health()
        except JudgeUnavailable as exc:
            raise RegisterError(exc.code) from None
        if model not in ids:
            raise RegisterError("model_not_listed", tuple(ids[:MODELS_SHOWN]))
        try:
            if rotating:
                os.replace(tmp, path)
            else:
                os.link(tmp, path)             # fails if a file appeared meanwhile: never clobbers a concurrent create
                os.unlink(tmp)
        except OSError:
            raise RegisterError("io") from None
    except BaseException:
        _unlink(tmp)
        raise
    return Registered(fingerprint(key), model, len(ids))
