"""Older inspector pages come from Postgres on demand: bounded, ordered, role-gated, never loaded into the ring."""
import asyncio
import contextlib
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import inspector_history as ih
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import _LoopRunner
from herdr_engineering.control_plane.inspector import InspectorStore
from herdr_engineering.control_plane.projection_history import PostgresHistory
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane._private_pg import private_postgres

FABRIC = """CREATE TABLE fabric_events (
  event_id text NOT NULL, sequence bigint NOT NULL, event_type text NOT NULL, entity_type text, entity_id text,
  mission_id text, session_id text, payload jsonb, source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(), schema_version integer NOT NULL DEFAULT 1,
  source text, source_sequence bigint);
CREATE INDEX idx_fabric_events_stream ON fabric_events (entity_type, entity_id, sequence);"""
T0 = float(int(time.time())) - 7200.0
SID = "sess-1"
RING = 50
TOTAL = 150


def _iso(t):
    return datetime.fromtimestamp(t, UTC)


def _rows():
    """TOTAL durable rows for SID, one per second: messages, with every 5th a finished tool call (start+end rows)."""
    rows, n = [], 0
    for i in range(TOTAL):
        t = T0 + i
        if i % 5 == 4:
            for phase, status in (("start", "running"), ("end", "ok")):
                n += 1
                rows.append(("tool", n, t + (0.1 if phase == "end" else 0),
                             {"call_id": f"c{i}", "tool": "bash", "status": status, "output": f"out {i}",
                              "args_preview": f"ls {i}"}))
        else:
            n += 1
            rows.append(("message", n, t, {"role": "assistant", "text": f"msg {i}"}))
    return rows


@contextlib.contextmanager
def _postgres(tmp_path):
    fabric = tmp_path / "fabric.sql"
    fabric.write_text(FABRIC)
    with private_postgres(fabric) as dsn:
        if dsn is not None:
            yield dsn
            return
    if not shutil.which("docker"):
        yield None
        return
    name, port = f"ih-test-{uuid.uuid4().hex[:8]}", 55000 + int(uuid.uuid4().int % 5000)
    subprocess.run(["docker", "run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=t", "-p", f"127.0.0.1:{port}:5432",
                    "postgres:16"], check=True, capture_output=True)
    try:
        dsn = f"postgresql://postgres:t@127.0.0.1:{port}/postgres"
        import asyncpg

        async def ready():
            for _ in range(60):
                try:
                    conn = await asyncpg.connect(dsn)
                    await conn.execute(FABRIC)
                    await conn.close()
                    return
                except Exception:
                    await asyncio.sleep(1)
            raise RuntimeError("postgres did not start")
        asyncio.run(ready())
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture()
def pg(tmp_path):
    asyncpg = pytest.importorskip("asyncpg")
    with _postgres(tmp_path) as dsn:
        if dsn is None:
            pytest.skip("no postgres available")

        async def seed():
            conn = await asyncpg.connect(dsn)
            try:
                for kind, n, t, body in _rows():
                    p = {**body, "kind": kind, "agent_id": "main", "parent_agent_id": None, "event_id": f"x{n}",
                         "sensitive": False, "redacted": False, "truncated": False, "_received_at": t,
                         "_sequence": n}
                    await conn.execute(
                        "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id,"
                        " payload, source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,$6,$6)",
                        f"x{n}", n, f"inspector.{kind}", SID, json.dumps(p), _iso(t))
                # another session and a preferences row that must never appear
                await conn.execute(
                    "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, payload,"
                    " source_timestamp, ingest_timestamp) VALUES ('o1',1,'inspector.message','inspector','other',"
                    " '{\"kind\":\"message\",\"role\":\"user\",\"text\":\"nope\"}', $1, $1)", _iso(T0))
                await conn.execute(
                    "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, payload,"
                    " source_timestamp, ingest_timestamp) VALUES ('p1',999,'inspector.preferences','inspector',$1,"
                    " '{\"prefs\":{}}', $2, $2)", SID, _iso(T0 - 5))
            finally:
                await conn.close()
        asyncio.run(seed())
        runner = _LoopRunner()
        history = PostgresHistory(dsn, runner.run)
        yield history
        history.close()
        runner._loop.call_soon_threadsafe(runner._loop.stop)


def _repo_with_ring(history):
    """A repo whose ring holds only the newest RING durable rows, as after a boot that loaded a window."""
    repo = ControlPlaneRepo()
    repo.inspector.set_prefs({"max_events_per_session": RING}, SID, persist=False)
    repo._durable_history = history
    events = []
    for kind, n, t, body in _rows():
        events.append({"event_type": f"inspector.{kind}", "entity_id": SID, "sequence": n,
                       "source_timestamp": _iso(t).isoformat(), "ingest_timestamp": _iso(t).isoformat(),
                       "payload": {**body, "kind": kind, "_received_at": t, "_sequence": n,
                                   "event_id": f"x{n}"}})
    repo.inspector.hydrate(events)
    return repo


def _walk(repo, **kw):
    pages, before = [], None
    while True:
        page = repo.inspector_history_page(SID, limit=20, before=before, **kw)
        pages.append(page)
        if not page["has_more"]:
            return pages
        before = page["next_cursor"]


def test_config_defaults_and_validation():
    assert ih.parse_inspector_history(None) == {"enabled": True, "page_size": 100, "max_page_size": 500}
    assert ih.parse_inspector_history({"enabled": False})["enabled"] is False
    for bad in ({"bogus": 1}, {"enabled": "yes"}, {"page_size": 0}, {"page_size": True},
                {"page_size": 600, "max_page_size": 500}, {"max_page_size": 5000}, [], "x"):
        with pytest.raises(ValueError):
            ih.parse_inspector_history(bad)


def test_fleet_config_rejects_unknown_inspector_history_key(tmp_path):
    import yaml

    from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config
    path = tmp_path / "fleet.yaml"

    def load(values):
        path.write_text(yaml.safe_dump({"version": 1, "sections": {"control_plane": {
            "roles": ["control-plane"], "values": {"inspector_history": values}}}, "hosts": {}}))
        path.chmod(0o600)
        return load_fleet_config(path)
    load({"enabled": True, "page_size": 50})
    with pytest.raises(FleetConfigError):
        load({"enabled": True, "typo": 1})


def test_cursor_roundtrip_and_rejection():
    row = {"ingest_timestamp": _iso(T0), "sequence": 5, "event_id": "e"}
    assert ih.decode_cursor(ih.encode_cursor(row)) == (_iso(T0), 5, "e")
    for bad in ("", "!!!", "e30", "x" * 600):
        with pytest.raises(ih.HistoryCursorInvalid):
            ih.decode_cursor(bad)


@pytest.mark.integration
def test_paging_below_the_ring_is_complete_ordered_and_bounded(pg):
    repo = _repo_with_ring(pg)
    ring_before = repo.inspector.query(SID, limit=1000)
    assert len(ring_before["events"]) == RING
    pages = _walk(repo)
    assert all(len(p["events"]) <= 20 for p in pages) and len(pages) > 1
    events = [e for p in pages for e in p["events"]]
    ids = [e["history_id"] for e in events]
    assert len(ids) == len(set(ids))
    # newest first, strictly older than everything in the ring, other sessions and preferences excluded
    stamps = [e["ts"] for e in events]
    assert stamps == sorted(stamps, reverse=True)
    oldest_ring_ts = ring_before["events"][-1]["ts"]
    assert all(s < oldest_ring_ts for s in stamps)
    assert not any("nope" in json.dumps(e) for e in events)
    # ring events + history cover every message and every tool call exactly once
    texts = [e["data"].get("text") for e in events if e["kind"] == "message"]
    ring_texts = [e["data"].get("text") for e in ring_before["events"] if e["kind"] == "message"]
    assert len(set(texts + ring_texts)) == len(texts + ring_texts)
    calls = [e["data"]["call_id"] for e in events if e["kind"] == "tool"]
    assert len(calls) == len(set(calls))
    assert all(e["data"]["status"] == "ok" for e in events if e["kind"] == "tool" and e["data"]["call_id"] != calls[-1])
    # the ring is unchanged: nothing loaded, nothing evicted
    assert repo.inspector.query(SID, limit=1000)["events"] == ring_before["events"]
    assert all(e["seq"] is None for e in events)


@pytest.mark.integration
def test_view_filter_and_last_page_has_no_cursor(pg):
    repo = _repo_with_ring(pg)
    pages = _walk(repo, view="tools")
    events = [e for p in pages for e in p["events"]]
    assert events and {e["kind"] for e in events} == {"tool"}
    assert pages[-1]["has_more"] is False and pages[-1]["next_cursor"] is None
    with pytest.raises(ValueError):
        repo.inspector_history_page(SID, view="nonsense")


@pytest.mark.integration
def test_sensitive_rows_are_withheld_from_a_viewer(pg):
    repo = _repo_with_ring(pg)
    store: InspectorStore = repo.inspector
    entries = ih.entries_from_rows([{"event_id": "s", "payload": {"kind": "tool", "call_id": "z", "sensitive": True,
                                                                  "output": "secret-token", "status": "ok"},
                                     "source_timestamp": _iso(T0)}])
    shown = store.render_history(entries, "viewer")[0]
    assert shown["data"]["withheld"] is True and shown["data"]["output"] is None
    assert "secret-token" in json.dumps(store.render_history(entries, "operator")[0])


@pytest.mark.integration
def test_without_durable_history_the_reader_says_so():
    repo = ControlPlaneRepo()
    with pytest.raises(LookupError):
        repo.inspector_history_page(SID)


@pytest.mark.integration
def test_http_endpoint_pages_and_reports_errors(pg):
    repo = _repo_with_ring(pg)
    svr = ControlPlaneServer(repo)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}/api/v1/sessions/{SID}/inspector"

    def get(url):
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        status, first = get(f"{base}/history?limit=5")
        assert status == 200 and first["source"] == "postgres" and len(first["events"]) <= 5 and first["has_more"]
        status, second = get(f"{base}/history?limit=5&before={first['next_cursor']}")
        assert status == 200 and second["events"][0]["ts"] < first["events"][-1]["ts"]
        assert get(f"{base}/history?limit=abc")[0] == 400
        assert get(f"{base}/history?before=garbage")[0] == 400
        assert get(f"{base}/history?view=nonsense")[0] == 400
        assert get(f"http://127.0.0.1:{svr.port}/api/v1/sessions/nope/inspector/history")[0] == 404
        # the existing ring endpoint is untouched
        status, ring = get(f"{base}?limit=100")
        assert status == 200 and len(ring["events"]) == RING
        repo._durable_history = None
        assert get(f"{base}/history")[0] == 503
    finally:
        svr.shutdown()
