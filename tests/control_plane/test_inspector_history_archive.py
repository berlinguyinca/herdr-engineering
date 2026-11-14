"""Inspector history continues into fabric_events_archive past the archive boundary, on a real PostgreSQL."""
import asyncio
import json

import pytest

from herdr_engineering.control_plane.bridge import PostgresSource, _LoopRunner
from herdr_engineering.control_plane.projection_history import PostgresHistory
from tests.control_plane import test_inspector_history as base
from tests.control_plane.test_inspector_history import (
    SID,
    T0,
    _iso,
    _postgres,
    _repo_with_ring,
    _rows,
)

ARCHIVE_DDL = """CREATE TABLE fabric_events_archive (LIKE fabric_events INCLUDING DEFAULTS,
  archived_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_fabric_events_archive_inspector
  ON fabric_events_archive (entity_id, ingest_timestamp, sequence)
  WHERE entity_type = 'inspector' AND entity_id IS NOT NULL AND entity_id <> '';"""  # the real migration 0052 index
ARCHIVED_BEFORE = 60  # rows older than T0+60 s are moved into the archive


async def _seed(dsn, *, archive=True, move=True, duplicate=False):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        if archive:
            await conn.execute(ARCHIVE_DDL)
        for kind, n, t, body in _rows():
            p = {**body, "kind": kind, "agent_id": "main", "parent_agent_id": None, "event_id": f"x{n}",
                 "sensitive": False, "redacted": False, "truncated": False, "_received_at": t, "_sequence": n}
            await conn.execute(
                "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id,"
                " payload, source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)",
                f"x{n}", n, f"inspector.{kind}", SID, json.dumps(p), _iso(t))
        if archive and move:
            cols = "event_id, sequence, event_type, entity_type, entity_id, mission_id, session_id, payload," \
                   " source_timestamp, ingest_timestamp, schema_version, source, source_sequence"
            verb = f"INSERT INTO fabric_events_archive ({cols}, archived_at) SELECT {cols}, now() FROM fabric_events "
            await conn.execute(verb + "WHERE ingest_timestamp < $1", _iso(T0 + ARCHIVED_BEFORE))
            if not duplicate:
                await conn.execute("DELETE FROM fabric_events WHERE ingest_timestamp < $1", _iso(T0 + ARCHIVED_BEFORE))
    finally:
        await conn.close()


def _history(tmp_path, **kw):
    pytest.importorskip("asyncpg")
    cm = _postgres(tmp_path)
    dsn = cm.__enter__()
    if dsn is None:
        cm.__exit__(None, None, None)
        pytest.skip("no postgres available")
    asyncio.run(_seed(dsn, **kw))
    runner = _LoopRunner()
    history = PostgresHistory(dsn, runner.run)
    return cm, runner, history


@pytest.fixture()
def pg_archived(tmp_path):
    cm, runner, history = _history(tmp_path)
    yield history
    history.close()
    runner._loop.call_soon_threadsafe(runner._loop.stop)
    cm.__exit__(None, None, None)


def _all_ids(repo, limit=20):
    ids, before = [], None
    while True:
        page = repo.inspector_history_page(SID, limit=limit, before=before)
        ids += [e["history_id"] for e in page["events"]]
        if not page["has_more"]:
            return ids
        before = page["next_cursor"]


@pytest.mark.integration
def test_pages_cross_the_archive_boundary_without_gaps_or_duplicates(pg_archived):
    repo = _repo_with_ring(pg_archived)
    pages, before = [], None
    while True:
        page = repo.inspector_history_page(SID, limit=20, before=before)
        pages.append(page)
        if not page["has_more"]:
            break
        before = page["next_cursor"]
    events = [e for p in pages for e in p["events"]]
    ids = [e["history_id"] for e in events]
    assert len(ids) == len(set(ids))
    stamps = [e["ts"] for e in events]
    assert stamps == sorted(stamps, reverse=True)
    # the oldest message ever written is reachable (it lives only in the archive)
    assert any(e["data"].get("text") == "msg 0" for e in events if e["kind"] == "message")
    # every message below the ring is returned exactly once
    texts = sorted(e["data"]["text"] for e in events if e["kind"] == "message")
    ring_texts = {e["data"]["text"] for e in repo.inspector.query(SID, limit=1000)["events"] if e["kind"] == "message"}
    expected = {f"msg {i}" for i in range(base.TOTAL) if i % 5 != 4} - ring_texts
    assert set(texts) == expected and len(texts) == len(expected)
    # a page that straddles the boundary exists: some page holds rows from both tables
    boundary = _iso(T0 + ARCHIVED_BEFORE).isoformat()
    assert any(min(e["ts"] for e in p["events"]) < boundary <= max(e["ts"] for e in p["events"]) for p in pages)


@pytest.mark.integration
def test_a_row_present_in_both_tables_is_returned_once(tmp_path):
    cm, runner, history = _history(tmp_path, duplicate=True)
    try:
        repo = _repo_with_ring(history)
        ids = _all_ids(repo)
        assert len(ids) == len(set(ids))
        assert "x1" in ids  # the oldest row sits in both tables
    finally:
        history.close()
        runner._loop.call_soon_threadsafe(runner._loop.stop)
        cm.__exit__(None, None, None)


@pytest.mark.integration
def test_without_an_archive_table_history_is_unchanged(tmp_path):
    cm, runner, history = _history(tmp_path, archive=False)
    try:
        repo = _repo_with_ring(history)
        ids = _all_ids(repo)
        assert len(ids) == len(set(ids)) and len(ids) > 0
    finally:
        history.close()
        runner._loop.call_soon_threadsafe(runner._loop.stop)
        cm.__exit__(None, None, None)


@pytest.mark.integration
def test_archived_view_filter_still_applies(pg_archived):
    repo = _repo_with_ring(pg_archived)
    before, kinds, texts = None, set(), set()
    while True:
        page = repo.inspector_history_page(SID, limit=20, before=before, view="conversation")
        kinds |= {e["kind"] for e in page["events"]}
        texts |= {e["data"]["text"] for e in page["events"]}
        if not page["has_more"]:
            break
        before = page["next_cursor"]
    assert kinds == {"message"}
    assert "msg 0" in texts


async def _insert(dsn, table, event_id, seq, ts, text):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        p = {"kind": "message", "role": "assistant", "text": text, "agent_id": "main", "parent_agent_id": None,
             "event_id": event_id, "_received_at": ts, "_sequence": seq}
        await conn.execute(
            f"INSERT INTO {table} (event_id, sequence, event_type, entity_type, entity_id, session_id, payload,"
            " source_timestamp, ingest_timestamp) VALUES ($1,$2,'inspector.message','inspector',$3,$3,$4::jsonb,$5,$5)",
            event_id, seq, SID, json.dumps(p), _iso(ts))
    finally:
        await conn.close()


def _texts(repo, limit=3):
    out, before = [], None
    while True:
        page = repo.inspector_history_page(SID, limit=limit, before=before)
        out += [e["data"]["text"] for e in page["events"] if e["kind"] == "message"]
        if not page["has_more"]:
            return out
        before = page["next_cursor"]


def _with_extra_rows(tmp_path, rows):
    """rows: (table, event_id, sequence, ts, text); seeded on top of the standard journal + archive."""
    pytest.importorskip("asyncpg")
    cm = _postgres(tmp_path)
    dsn = cm.__enter__()
    if dsn is None:
        cm.__exit__(None, None, None)
        pytest.skip("no postgres available")
    asyncio.run(_seed(dsn))
    for row in rows:
        asyncio.run(_insert(dsn, *row))
    return cm, dsn


@pytest.mark.integration
def test_same_event_id_with_a_different_sequence_in_both_tables_keeps_both(tmp_path):
    cm, dsn = _with_extra_rows(tmp_path, [
        ("fabric_events_archive", "shared", 9001, T0 + 5.25, "dup archived"),
        ("fabric_events", "shared", 9002, T0 + 5.5, "dup journal")])
    runner = _LoopRunner()
    history = PostgresHistory(dsn, runner.run)
    try:
        texts = _texts(_repo_with_ring(history))
        assert "dup archived" in texts and "dup journal" in texts
        assert texts.index("dup journal") < texts.index("dup archived")
    finally:
        history.close()
        runner._loop.call_soon_threadsafe(runner._loop.stop)
        cm.__exit__(None, None, None)


@pytest.mark.integration
def test_equal_timestamps_split_across_tables_order_and_page_consistently(tmp_path):
    ts = T0 + 7.25
    cm, dsn = _with_extra_rows(tmp_path, [
        ("fabric_events", "e1", 8001, ts, "t1"), ("fabric_events_archive", "e2", 8002, ts, "t2"),
        ("fabric_events", "e3", 8003, ts, "t3"), ("fabric_events_archive", "e4", 8004, ts, "t4"),
        ("fabric_events", "same", 8005, ts, "t5a"), ("fabric_events_archive", "samf", 8005, ts, "t5b")])
    runner = _LoopRunner()
    history = PostgresHistory(dsn, runner.run)
    try:
        for limit in (1, 2, 3):
            tied = [t for t in _texts(_repo_with_ring(history), limit=limit) if t.startswith("t") and t[1:2].isdigit()]
            assert tied == ["t5b", "t5a", "t4", "t3", "t2", "t1"], limit
    finally:
        history.close()
        runner._loop.call_soon_threadsafe(runner._loop.stop)
        cm.__exit__(None, None, None)


@pytest.mark.integration
def test_bridge_path_reads_the_archive(tmp_path):
    pytest.importorskip("asyncpg")
    cm = _postgres(tmp_path)
    dsn = cm.__enter__()
    if dsn is None:
        cm.__exit__(None, None, None)
        pytest.skip("no postgres available")
    asyncio.run(_seed(dsn))
    source = PostgresSource(dsn=dsn)
    try:
        texts, before = [], None
        while True:
            page = source.inspector_page(SID, limit=20, before=before)
            texts += [r["payload"]["text"] for r in page["rows"] if r["payload"].get("kind") == "message"]
            if not page["has_more"]:
                break
            before = page["next_cursor"]
        assert "msg 0" in texts and len(texts) == len(set(texts))
    finally:
        source.close()
        cm.__exit__(None, None, None)
