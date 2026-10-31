"""Crash-safe ingestion of an append-only JSONL file written by an untrusted, database-free container.

Extracted from ``ShareStore`` (see ``artifact_shares`` for the design narrative) so other event streams can reuse it.
The caller supplies two things: ``parse_line`` (bytes -> parsed value, or None to skip the line) and ``store`` (insert
one parsed value inside a per-line savepoint, returning True when a row was inserted).

* A marker file records ``{offset, fp, skipping}`` of the consumed prefix; the fingerprint hashes the first and last
  bytes of that prefix, so a deleted/recreated or rewritten file is detected and re-read from 0.
* Every line gets ``ingest_key = sha256(line):<offset>``; ``store`` must make that key unique so replays never
  double-insert.
* A final line without a newline waits for its newline.  A line longer than ``max_line_bytes`` is skipped but
  consumed, as is a line ``store`` cannot persist (poison line).  A run of ``max_ingest_bytes`` without a newline is
  skipped through its end.
* Only a regular file is read: a symlink or FIFO planted in the (writable-by-others) directory is ignored.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, BinaryIO

import asyncpg

MAX_INGEST_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 16 * 1024
FINGERPRINT_BYTES = 64
INGEST_ROUNDS = 64

ParseLine = Callable[[bytes], Any | None]
StoreLine = Callable[[Any, Any, str], Awaitable[bool]]
OpenRegular = Callable[[Path], BinaryIO | None]


@asynccontextmanager
async def transaction(conn: Any):
    """``conn.transaction()`` (a savepoint when nested); a connection without one is used as is."""
    begin = getattr(conn, "transaction", None)
    if begin is None:
        yield conn
        return
    async with begin():
        yield conn


def open_regular(path: Path) -> BinaryIO | None:
    """A read handle on ``path`` when it is a regular file (never through a symlink, never blocking on a FIFO)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def _fingerprint(handle: BinaryIO, offset: int) -> str:
    """sha256 over the first and last ``FINGERPRINT_BYTES`` of the consumed prefix ``[0, offset)``."""
    n = min(FINGERPRINT_BYTES, offset)
    handle.seek(0)
    first = handle.read(n)
    handle.seek(offset - n)
    last = handle.read(n)
    return hashlib.sha256(first + b"|" + last).hexdigest()


def _read_marker(marker: Path, handle: BinaryIO, size: int) -> tuple[int, bool]:
    try:
        data = json.loads(marker.read_text())
        offset, skipping = data["offset"], bool(data.get("skipping", False))
        if not isinstance(offset, int) or offset < 0 or offset > size:
            return 0, False
        if offset == 0 or _fingerprint(handle, offset) == data.get("fp"):
            return offset, skipping
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return 0, False


def _write_marker(marker: Path, handle: BinaryIO, offset: int, skipping: bool) -> None:
    fp = _fingerprint(handle, offset) if offset else ""
    fd, tmp = tempfile.mkstemp(prefix=marker.name + ".", suffix=".tmp", dir=marker.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"offset": offset, "fp": fp, "skipping": skipping}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, marker)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class JsonlIngestor:
    def __init__(self, conn: Any, *, parse_line: ParseLine, store: StoreLine,
                 max_line_bytes: int = DEFAULT_MAX_LINE_BYTES, max_ingest_bytes: int = MAX_INGEST_BYTES,
                 opener: OpenRegular = open_regular) -> None:
        self._conn = conn
        self._parse_line = parse_line
        self._store = store
        self._max_line_bytes = max_line_bytes
        self._max_ingest_bytes = max_ingest_bytes
        self._open = opener

    async def ingest_file(self, path: Path, marker: Path) -> int:
        """Ingest complete lines appended to ``path`` since the last run; returns rows inserted."""
        return (await self._ingest_once(Path(path), Path(marker)))[0]

    async def ingest_and_rotate(self, path: Path, marker: Path, *, rotate_bytes: int, now: float,
                                grace: float) -> int:
        """Ingest ``path`` (and a pending ``path.1``) and rotate ``path`` once ``rotate_bytes`` of it are consumed.

        Per call: (1) a leftover ``.1`` is read to its end with its own marker; it is deleted, then its marker, only
        when no complete line remains and it was not written for ``grace`` seconds.  (2) ``path`` is ingested.
        (3) Only when no ``.1`` exists and at least ``rotate_bytes`` of ``path`` were consumed, ``path`` is renamed to
        ``.1`` and its marker moved along (bytes, offsets and fingerprint unchanged), so the rest is drained by (1)
        on a later call, never in the call that renamed it.  Crash-safe: a missing or stale marker re-reads from 0
        and the unique ``ingest_key`` drops the replays.
        """
        path, marker = Path(path), Path(marker)
        rotated = path.with_name(path.name + ".1")
        rotated_marker = marker.with_name(marker.stem + ".rotated" + marker.suffix)
        inserted = 0
        if os.path.lexists(rotated):
            count, drained, _, mtime = await self._drain(rotated, rotated_marker)
            inserted += count
            try:
                regular = stat.S_ISREG(os.lstat(rotated).st_mode)
            except OSError:
                regular = True                                          # unknown: keep it
            # a planted non-file goes at once; a regular file only once fully read and quiet (an open failure such
            # as EMFILE leaves mtime None: it is kept and retried next sweep)
            if not regular or (mtime is not None and drained and now - mtime >= grace):
                try:
                    os.unlink(rotated)
                    rotated_marker.unlink(missing_ok=True)
                except OSError:
                    pass                                                # a planted directory: left alone
        count, _, offset, mtime = await self._drain(path, marker)
        inserted += count
        if mtime is not None and offset >= rotate_bytes and not os.path.lexists(rotated):
            os.rename(path, rotated)
            try:
                os.replace(marker, rotated_marker)
            except FileNotFoundError:
                pass
        return inserted

    async def _drain(self, path: Path, marker: Path) -> tuple[int, bool, int, float | None]:
        inserted, drained, offset, mtime = 0, False, 0, None
        for _ in range(INGEST_ROUNDS):
            count, drained, offset, mtime = await self._ingest_once(path, marker)
            inserted += count
            if drained or mtime is None:
                break
        return inserted, drained, offset, mtime

    async def _ingest_once(self, path: Path, marker: Path) -> tuple[int, bool, int, float | None]:
        """One bounded pass: ``(inserted, drained, consumed_offset, mtime)``; mtime None when ``path`` is not a
        regular file.  ``drained``: no complete line is left after the consumed offset (a torn tail may remain)."""
        handle = self._open(path)
        if handle is None:
            return 0, True, 0, None
        with handle:
            st = os.fstat(handle.fileno())
            size, mtime = st.st_size, st.st_mtime
            offset, skipping = _read_marker(marker, handle, size)
            handle.seek(offset)
            chunk = handle.read(self._max_ingest_bytes)
            full = len(chunk) >= self._max_ingest_bytes
            consumed = 0
            inserted = 0
            if skipping:  # discard the rest of an oversize line
                newline = chunk.find(b"\n")
                if newline < 0:
                    consumed, skipping = len(chunk), True
                else:
                    consumed, skipping = newline + 1, False
            start = consumed
            end = chunk.rfind(b"\n") + 1  # only complete lines; a torn tail waits for its newline
            if end > start:
                async with transaction(self._conn) as conn:
                    position = offset + start
                    for raw in chunk[start:end].splitlines(keepends=True):
                        parsed = None if len(raw) > self._max_line_bytes else self._parse_line(raw)
                        if parsed is not None:
                            key = hashlib.sha256(raw).hexdigest() + f":{position}"
                            try:
                                async with transaction(conn):
                                    if await self._store(conn, parsed, key):
                                        inserted += 1
                            except (asyncpg.PostgresError, UnicodeError, ValueError):
                                pass  # poison line: skipped, but consumed below
                        position += len(raw)
                consumed = end
            elif end == 0 and start == 0 and full:  # one huge line with no newline: skip it
                consumed, skipping = len(chunk), True
            drained = not full and b"\n" not in chunk[consumed:]
            if consumed == 0 and not skipping:
                return 0, drained, offset, mtime
            new_offset = offset + consumed
            _write_marker(marker, handle, new_offset, skipping)
        return inserted, drained, new_offset, mtime
