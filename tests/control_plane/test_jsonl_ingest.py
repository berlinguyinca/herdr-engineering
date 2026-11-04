"""JsonlIngestor with an in-memory store callback (no database)."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from herdr_engineering.control_plane.jsonl_ingest import JsonlIngestor


class Sink:
    def __init__(self, poison: str | None = None) -> None:
        self.rows: list[tuple[str, str]] = []
        self.poison = poison

    @staticmethod
    def parse(raw: bytes) -> str | None:
        text = raw.decode().strip()
        return text or None

    async def store(self, conn: object, parsed: str, key: str) -> bool:
        if parsed == self.poison:
            raise ValueError("cannot store")
        if any(k == key for _, k in self.rows):
            return False
        self.rows.append((parsed, key))
        return True

    def ingestor(self, **kw: int) -> JsonlIngestor:
        return JsonlIngestor(None, parse_line=self.parse, store=self.store, **kw)


def run(coro):
    return asyncio.run(coro)


def test_torn_last_line_waits_for_its_newline(tmp_path: Path) -> None:
    sink, f, m = Sink(), tmp_path / "e.jsonl", tmp_path / "m.json"
    f.write_bytes(b"one\ntw")
    ing = sink.ingestor()
    assert run(ing.ingest_file(f, m)) == 1
    with f.open("ab") as fh:
        fh.write(b"o\n")
    assert run(ing.ingest_file(f, m)) == 1
    assert [p for p, _ in sink.rows] == ["one", "two"]


def test_replay_after_marker_deleted_reuses_ingest_keys(tmp_path: Path) -> None:
    f, m = tmp_path / "e.jsonl", tmp_path / "m.json"
    f.write_bytes(b"a\nb\n")
    keys = []

    async def store(conn: object, parsed: str, key: str) -> bool:
        keys.append(key)
        return True
    ing = JsonlIngestor(None, parse_line=Sink.parse, store=store)
    run(ing.ingest_file(f, m))
    m.unlink()
    run(ing.ingest_file(f, m))
    assert len(keys) == 4 and keys[:2] == keys[2:] and len(set(keys)) == 2


def test_symlink_at_path_is_ignored(tmp_path: Path) -> None:
    sink, real, f = Sink(), tmp_path / "real", tmp_path / "e.jsonl"
    real.write_bytes(b"a\n")
    f.symlink_to(real)
    assert run(sink.ingestor().ingest_file(f, tmp_path / "m.json")) == 0
    assert sink.rows == []


def test_store_valueerror_skips_and_consumes_the_line(tmp_path: Path) -> None:
    sink, f, m = Sink(poison="bad"), tmp_path / "e.jsonl", tmp_path / "m.json"
    f.write_bytes(b"a\nbad\nb\n")
    ing = sink.ingestor()
    assert run(ing.ingest_file(f, m)) == 2
    assert run(ing.ingest_file(f, m)) == 0
    assert [p for p, _ in sink.rows] == ["a", "b"]


def test_line_limit_defaults_to_16kib_and_is_configurable(tmp_path: Path) -> None:
    big = b"x" * (40 * 1024) + b"\n"
    f = tmp_path / "e.jsonl"
    f.write_bytes(big + b"ok\n")
    default = Sink()
    assert run(default.ingestor().ingest_file(f, tmp_path / "m1.json")) == 1
    assert [p for p, _ in default.rows] == ["ok"]
    wide = Sink()
    assert run(wide.ingestor(max_line_bytes=65536).ingest_file(f, tmp_path / "m2.json")) == 2
    assert len(wide.rows[0][0]) == 40 * 1024


def test_rotation_waits_for_rotate_bytes_then_drains_and_deletes_after_grace(tmp_path: Path) -> None:
    sink, f, m = Sink(), tmp_path / "e.jsonl", tmp_path / "m.json"
    rotated = tmp_path / "e.jsonl.1"
    ing = sink.ingestor()
    t0 = time.time()
    f.write_bytes(b"a\nb\n")
    assert run(ing.ingest_and_rotate(f, m, rotate_bytes=100, now=t0, grace=30)) == 2
    assert f.exists() and not rotated.exists()                    # below the threshold
    assert run(ing.ingest_and_rotate(f, m, rotate_bytes=4, now=t0, grace=30)) == 0
    assert rotated.exists() and not f.exists()                    # rotated, not drained in the same call
    with rotated.open("ab") as fh:
        fh.write(b"late\n")
    os.utime(rotated, (t0 - 3600, t0 - 3600))
    assert run(ing.ingest_and_rotate(f, m, rotate_bytes=4, now=t0, grace=30)) == 1
    assert not rotated.exists()                                   # drained and quiet for > grace
    assert [p for p, _ in sink.rows] == ["a", "b", "late"]


# --- a deterministic non-poison error must be visible, not silent (final opus review m2) --------------------------

class Flaky(Sink):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True

    async def store(self, conn: object, parsed: str, key: str) -> bool:
        if parsed == "b" and self.broken:
            raise TypeError("malformed record")
        return await super().store(conn, parsed, key)


def test_a_stuck_line_logs_a_warning_with_a_consecutive_counter_and_clears_on_progress(tmp_path: Path, caplog) -> None:
    import logging

    import pytest
    sink, f, m = Flaky(), tmp_path / "e.jsonl", tmp_path / "m.json"
    f.write_bytes(b"a\nb\nc\n")
    ing = sink.ingestor()
    with caplog.at_level(logging.WARNING, logger="herdr_engineering.control_plane.jsonl_ingest"):
        for attempt in (1, 2, 3):
            with pytest.raises(TypeError):
                run(ing.ingest_file(f, m))
            assert ing.consecutive_aborts(f) == attempt
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 3 and "offset 2" in messages[0] and "3 consecutive" in messages[2], messages
    assert "malformed record" not in " ".join(messages)           # the line's content never reaches a log
    sink.broken = False
    assert run(ing.ingest_file(f, m)) == 2                       # a is a replay (same key); b and c are new
    assert ing.consecutive_aborts(f) == 0
