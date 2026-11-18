"""Newest-first reading of a JSONL record file, shared by the adapters that scan a session record's tail."""
from collections.abc import Iterator
from pathlib import Path

_BLOCK = 1_048_576


def lines_newest_first(path: Path, block: int = _BLOCK) -> Iterator[str]:
    """Yield the file's non-empty lines, last first, reading `block` bytes at a time from the end.

    Splits on "\\n" only: str.splitlines() also splits on U+2028, U+2029, U+0085 and others that can
    appear raw inside a JSON string. A line is yielded only once it is whole (its start was read); the
    last line may be half-written, which the caller's JSON parse rejects.
    """
    with open(path, "rb") as fh:
        pos = fh.seek(0, 2)
        tail = b""
        while pos > 0:
            start = max(0, pos - block)
            fh.seek(start)
            parts = (fh.read(pos - start) + tail).split(b"\n")
            tail = parts.pop(0) if start > 0 else b""  # the first piece may be cut; keep it for the next block
            for part in reversed(parts):
                if part:
                    yield part.decode("utf-8", "replace")
            pos = start
