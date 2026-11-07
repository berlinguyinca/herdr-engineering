"""resource_run ingest + GET /api/v1/resource-profiles + migration 0052 (per-run resource capture).

No mocks: a real ControlPlaneServer on a loopback port and real HTTP; the Postgres tests run the migration and the
table-backed store on a private, Unix-socket-only PostgreSQL (skipped cleanly when no server binaries exist).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering.control_plane import resource_runs as rr
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import INGEST_ENTITY_TYPES
from herdr_engineering.control_plane.repo import ControlPlaneRepo

SQL = Path(__file__).resolve().parents[2] / "deploy/control-plane/migrations/0052_resource_runs.sql"
T0 = "2026-11-04T10:00:00+00:00"
T1 = "2026-11-04T10:30:00+00:00"


def run(session_id="run-1", **over):
    return {"project": "herdr-engineering", "agent_kind": "claude", "session_id": session_id, "host": "beast",
            "started_at": T0, "ended_at": T1, "peak_memory_bytes": 2_000_000_000, "cpu_seconds": 120.5,
            "disk_growth_bytes": 50_000_000, "exit_reason": "exited", "hit_limit": False, **over}


def envelope(payload, entity_id=None, event_type=rr.EVENT_TYPE):
    return {"source": "fabric-agent", "entity_type": "resource_run",
            "entity_id": payload.get("session_id") if entity_id is None else entity_id,
            "event_type": event_type, "payload": payload}


def _without(payload, key):
    return {k: v for k, v in payload.items() if k != key}


# --------------------------------------------------------------------------- unit: validator

def test_resource_run_is_an_ingest_entity_type():
    assert "resource_run" in INGEST_ENTITY_TYPES


def test_a_valid_run_returns_a_clean_copy():
    payload = run()
    clean = rr.validate_resource_run("run-1", rr.EVENT_TYPE, payload)
    assert clean == {**payload, "cpu_seconds": 120.5} and clean is not payload
    assert rr.validate_resource_run("run-1", rr.EVENT_TYPE, run(disk_growth_bytes=None))["disk_growth_bytes"] is None
    assert rr.validate_resource_run("run-1", rr.EVENT_TYPE, run(ended_at=T0))["ended_at"] == T0   # zero length
    assert rr.validate_resource_run("run-1", rr.EVENT_TYPE, run(project="owner/repo.x"))["project"] == "owner/repo.x"


@pytest.mark.parametrize("entity_id,event_type,payload", [
    ("run-1", "resource_run.started", run()),                                   # unknown event type
    ("run-2", rr.EVENT_TYPE, run()),                                            # entity_id must be session_id
    ("run-1", rr.EVENT_TYPE, {**run(), "argv": ["claude", "--token", "x"]}),    # unknown field
    ("run-1", rr.EVENT_TYPE, {**run(), "env": {"HOME": "/x"}}),
    ("run-1", rr.EVENT_TYPE, _without(run(), "hit_limit")),                     # every field is required
    ("run-1", rr.EVENT_TYPE, _without(run(), "disk_growth_bytes")),
    ("run-1", rr.EVENT_TYPE, run(ended_at="2026-11-04T09:59:59+00:00")),        # ended before it started
    ("run-1", rr.EVENT_TYPE, run(started_at="2026-11-04T10:00:00")),            # naive
    ("run-1", rr.EVENT_TYPE, run(started_at="2026-11-04T12:00:00+02:00")),      # not UTC
    ("run-1", rr.EVENT_TYPE, run(ended_at="yesterday")),
    ("run-1", rr.EVENT_TYPE, run(ended_at=1_700_000_000)),
    ("run-1", rr.EVENT_TYPE, run(started_at="2024-11-04T10:00:00+00:00")),      # longer than 366 days
    ("run-1", rr.EVENT_TYPE, run(ended_at="2099-01-01T00:00:00+00:00")),        # in the future (and too long)
    ("run-1", rr.EVENT_TYPE, run(started_at="2099-01-01T00:00:00+00:00",
                                 ended_at="2099-01-01T01:00:00+00:00")),        # a short run in the future
    ("run-1", rr.EVENT_TYPE, run(peak_memory_bytes=-1)),                        # negative
    ("run-1", rr.EVENT_TYPE, run(peak_memory_bytes=1.5)),                       # not an integer
    ("run-1", rr.EVENT_TYPE, run(peak_memory_bytes=True)),                      # bool is not a count
    ("run-1", rr.EVENT_TYPE, run(peak_memory_bytes="100")),
    ("run-1", rr.EVENT_TYPE, run(peak_memory_bytes=None)),                      # only disk growth may be unknown
    ("run-1", rr.EVENT_TYPE, run(peak_memory_bytes=2 ** 50 + 1)),               # out of range
    ("run-1", rr.EVENT_TYPE, run(cpu_seconds=-0.1)),
    ("run-1", rr.EVENT_TYPE, run(cpu_seconds=float("nan"))),
    ("run-1", rr.EVENT_TYPE, run(cpu_seconds=float("inf"))),
    ("run-1", rr.EVENT_TYPE, run(cpu_seconds=1e9 + 1)),
    ("run-1", rr.EVENT_TYPE, run(cpu_seconds=False)),
    ("run-1", rr.EVENT_TYPE, run(disk_growth_bytes=-5)),
    ("run-1", rr.EVENT_TYPE, run(disk_growth_bytes=2 ** 50 + 1)),
    ("run-1", rr.EVENT_TYPE, run(exit_reason="segfault")),                      # closed enum
    ("run-1", rr.EVENT_TYPE, run(exit_reason="OOM")),
    ("run-1", rr.EVENT_TYPE, run(hit_limit=0)),                                 # boolean only
    ("run-1", rr.EVENT_TYPE, run(hit_limit="false")),
    ("run-1", rr.EVENT_TYPE, run(project="")),
    ("run-1", rr.EVENT_TYPE, run(project="a" * 129)),                           # bounded
    ("run-1", rr.EVENT_TYPE, run(project="../etc")),
    ("run-1", rr.EVENT_TYPE, run(project="has space")),
    ("run-1", rr.EVENT_TYPE, run(project="[REDACTED]")),
    ("run-1", rr.EVENT_TYPE, run(project=7)),
    ("run-1", rr.EVENT_TYPE, run(agent_kind="Claude")),
    ("run-1", rr.EVENT_TYPE, run(agent_kind="c" * 33)),
    ("run-1", rr.EVENT_TYPE, run(host="beast; rm -rf /")),
    ("run-1", rr.EVENT_TYPE, run(host="b" * 65)),
    ("x" * 129, rr.EVENT_TYPE, run(session_id="x" * 129)),
    ("run 1", rr.EVENT_TYPE, run(session_id="run 1")),
    ("run-1", rr.EVENT_TYPE, ["not", "an", "object"]),
])
def test_bad_values_are_rejected(entity_id, event_type, payload):
    with pytest.raises(ValueError):
        rr.validate_resource_run(entity_id, event_type, payload)


# --------------------------------------------------------------------------- unit: percentile math

def test_percentile_is_linear_between_closest_ranks():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert rr.percentile(values, 0.5) == 5.5
    assert rr.percentile(values, 0.95) == pytest.approx(9.55)
    assert rr.percentile([7], 0.95) == 7
    assert rr.percentile([10, 20], 0.5) == 15
    with pytest.raises(ValueError):
        rr.percentile([], 0.5)


def test_summary_skips_unknown_values_and_reports_their_count():
    assert rr.summarize([None, 4, 1, None, 3, 2]) == {"count": 4, "median": 2.5, "p95": pytest.approx(3.85),
                                                       "max": 4}
    assert rr.summarize([None, None]) == {"count": 0, "median": None, "p95": None, "max": None}


def test_memory_store_window_keeps_the_newest_runs_of_each_kind(monkeypatch):
    monkeypatch.setattr(rr, "PROFILE_WINDOW", 3)
    store = rr.MemoryResourceRuns()
    base = datetime(2026, 11, 1, tzinfo=UTC)
    for i in range(5):   # cpu 0..4; only the newest three (2, 3, 4) count
        end = (base + timedelta(hours=i)).isoformat()
        assert store.record(run(f"run-{i}", started_at=base.isoformat(), ended_at=end, cpu_seconds=float(i)))
    [profile] = store.profiles("herdr-engineering")["profiles"]
    assert profile["count"] == 3 and profile["metrics"]["cpu_seconds"]["median"] == 3.0
    assert profile["metrics"]["cpu_seconds"]["max"] == 4.0


def test_memory_store_is_bounded_and_drops_the_oldest_run():
    store = rr.MemoryResourceRuns(max_runs=2)
    store.record(run("a", ended_at="2026-11-04T10:10:00+00:00"))
    store.record(run("b", ended_at="2026-11-04T10:05:00+00:00"))   # oldest
    store.record(run("c", ended_at="2026-11-04T10:20:00+00:00"))
    assert store.profiles("herdr-engineering")["profiles"][0]["count"] == 2
    assert store.record(run("b", ended_at="2026-11-04T10:05:00+00:00")) is True   # it was evicted, so it is new


# --------------------------------------------------------------------------- integration: HTTP

@pytest.fixture()
def server():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"

    def post(body):
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(query="?project=herdr-engineering"):
        try:
            with urllib.request.urlopen(base + "/api/v1/resource-profiles" + query) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    yield post, get, svr, base
    svr.shutdown()


# A seeded table: 20 claude runs of herdr-engineering (memory 1..20 GiB, cpu 10..200 s, disk 1..20 MB except two
# unmeasured), 3 codex runs, and a run of another project that must never show up.
GIB, MB = 1024 ** 3, 1_000_000


def seed(post):
    for i in range(1, 21):
        disk = None if i in (7, 8) else i * MB
        body = run(f"claude-{i}", peak_memory_bytes=i * GIB, cpu_seconds=float(i * 10), disk_growth_bytes=disk,
                   hit_limit=i == 20, exit_reason="oom" if i == 20 else "exited")
        assert post(envelope(body))[0] == 200
    for i, mem in enumerate((300 * MB, 100 * MB, 200 * MB)):
        assert post(envelope(run(f"codex-{i}", agent_kind="codex", peak_memory_bytes=mem, cpu_seconds=1.0,
                                 disk_growth_bytes=0)))[0] == 200
    assert post(envelope(run("other-1", project="other-project", peak_memory_bytes=999 * GIB)))[0] == 200


def test_profiles_report_count_median_p95_and_max_per_metric(server):
    post, get, *_ = server
    seed(post)
    status, body = get()
    assert status == 200
    assert body["project"] == "herdr-engineering" and body["window"] == rr.PROFILE_WINDOW
    claude, codex = body["profiles"]
    assert claude["agent_kind"] == "claude" and claude["count"] == 20
    mem = claude["metrics"]["peak_memory_bytes"]
    assert mem == {"count": 20, "median": 10.5 * GIB, "p95": pytest.approx(19.05 * GIB), "max": 20 * GIB}
    cpu = claude["metrics"]["cpu_seconds"]
    assert cpu == {"count": 20, "median": 105.0, "p95": pytest.approx(190.5), "max": 200.0}
    disk = claude["metrics"]["disk_growth_bytes"]
    known = sorted(i * MB for i in range(1, 21) if i not in (7, 8))
    assert disk["count"] == 18 and disk["max"] == 20 * MB
    assert disk["median"] == rr.percentile(known, 0.5) == 11.5 * MB   # 1..6, 9..20: (11 + 12) / 2
    assert disk["p95"] == pytest.approx(19.15 * MB)
    assert codex["agent_kind"] == "codex" and codex["count"] == 3
    assert codex["metrics"]["peak_memory_bytes"] == {"count": 3, "median": 200 * MB, "p95": pytest.approx(290 * MB),
                                                     "max": 300 * MB}


def test_an_unknown_project_has_no_profiles(server):
    post, get, *_ = server
    seed(post)
    assert get("?project=nobody") == (200, {"project": "nobody", "window": rr.PROFILE_WINDOW, "profiles": []})


@pytest.mark.parametrize("query", ["", "?", "?project=", "?project=a%20b", "?project=../x",
                                   "?project=a&project=b", "?project=a&agent_kind=claude", "?limit=5",
                                   "?project=" + "a" * 129])
def test_the_project_filter_is_required_and_validated(server, query):
    _, get, *_ = server
    status, body = get(query)
    assert status == 400 and "error" in body


def test_profiles_are_read_only(server):
    *_, base = server
    req = urllib.request.Request(base + "/api/v1/resource-profiles?project=herdr-engineering", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code in (404, 405)


def test_a_replayed_event_is_stored_once(server):
    post, get, *_ = server
    first = post(envelope(run()))
    assert first[0] == 200 and first[1]["entity"]["duplicate"] is False
    again = post(envelope(run()))
    assert again[0] == 200 and again[1]["entity"]["duplicate"] is True
    assert get()[1]["profiles"][0]["count"] == 1


def test_same_session_id_on_another_host_is_another_run(server):
    post, get, *_ = server
    assert post(envelope(run()))[0] == 200
    assert post(envelope(run(host="bender")))[0] == 200
    assert get()[1]["profiles"][0]["count"] == 2


@pytest.mark.parametrize("payload", [run(peak_memory_bytes=-1), run(ended_at="2026-11-04T09:00:00+00:00"),
                                     run(exit_reason="exploded"), {**run(), "cmdline": "claude --api-key sk-x"},
                                     run(project="ghp_" + "a" * 36)])
def test_bad_events_are_a_400_and_store_nothing(server, payload):
    post, get, *_ = server
    assert post(envelope(payload))[0] == 400
    assert get()[1]["profiles"] == []


def test_a_mismatched_entity_id_is_a_400(server):
    post, get, *_ = server
    assert post(envelope(run(), entity_id="run-2"))[0] == 400
    assert get()[1]["profiles"] == []


def test_profiles_need_the_same_auth_as_sibling_reads():
    svr = ControlPlaneServer(ControlPlaneRepo(), api_keys={"viewer-key": "viewer", "ingest-key": "ingest"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    try:
        def get(path, key=None):
            req = urllib.request.Request(base + path, headers={"X-API-Key": key} if key else {})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        for path in ("/api/v1/resource-profiles?project=p", "/api/v1/fleet-rollouts"):
            assert get(path) == 401
            assert get(path, "wrong") == 401
            assert get(path, "viewer-key") == 200
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(envelope(run())).encode(),
                                     headers={"Content-Type": "application/json", "X-API-Key": "viewer-key"},
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 403
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(envelope(run())).encode(),
                                     headers={"Content-Type": "application/json", "X-API-Key": "ingest-key"},
                                     method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
    finally:
        svr.shutdown()


def test_a_store_outage_is_a_503_and_nothing_is_acknowledged():
    def refuse():
        raise OSError("connection refused for postgresql://u:secret@db/x")
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)
    svr.resource_runs = svr.ingest.resource_runs = rr.PostgresResourceRuns("postgresql://u:secret@db/x", connect=refuse)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    try:
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(envelope(run())).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req)
        assert err.value.code == 503
        assert "secret" not in err.value.read().decode()
    finally:
        svr.shutdown()


def test_the_store_error_never_carries_the_dsn():
    def refuse():
        raise OSError("could not connect to postgresql://u:secret@db/x")
    with pytest.raises(rr.ResourceRunsUnavailable) as err:
        rr.PostgresResourceRuns("postgresql://u:secret@db/x", connect=refuse).profiles("p")
    assert "secret" not in str(err.value) and err.value.__cause__ is None


# --------------------------------------------------------------------------- migration 0052

def test_down_drops_exactly_the_table_created_here():
    text = SQL.read_text()
    assert re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text) == ["resource_runs"]
    assert re.findall(r"DROP TABLE IF EXISTS ([\w, ]+);", text) == ["resource_runs"]
    assert text.count("CREATE INDEX IF NOT EXISTS") == 1


def test_migration_is_the_next_number_and_its_enums_match_the_validator():
    names = sorted(p.name for p in SQL.parent.glob("*.sql"))
    assert names[-1] == SQL.name and SQL.name.startswith("0052_")
    text = SQL.read_text()
    enum = re.search(r"exit_reason IN \(([^)]*)\)", text, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", enum)) == rr.EXIT_REASONS
    assert str(rr.MAX_BYTES) in text and "PRIMARY KEY (host, session_id)" in text


def _initdb():
    candidates = []
    config = shutil.which("pg_config")
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    which = shutil.which("initdb")
    if which:
        candidates.append(Path(which))
    return next((c for c in candidates if c.is_file()), None)


@pytest.fixture(scope="module")
def postgres():
    if os.getuid() == 0:
        pytest.skip("isolated initdb cannot run as root")
    initdb = _initdb()
    if initdb is None:
        pytest.skip("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="rr-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "server.log"),
                        "-o", f"-c listen_addresses='' -k {area} -c fsync=off -c dynamic_shared_memory_type=mmap",
                        "-w", "start"], check=True, capture_output=True)
        try:
            yield f"postgresql:///postgres?host={area}"
        finally:
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True)


def _sql(dsn, statement, args=()):
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(statement, args)
            out = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return out
    finally:
        conn.close()


def test_migration_up_rerun_constraints_store_and_down_on_postgres(postgres):
    import psycopg2
    _sql(postgres, SQL.read_text())
    _sql(postgres, SQL.read_text())   # re-running is a no-op
    assert _sql(postgres, "SELECT to_regclass('resource_runs') IS NOT NULL")[0][0] is True
    store = rr.PostgresResourceRuns(postgres)
    assert store.record(rr.validate_resource_run("run-1", rr.EVENT_TYPE, run())) is True
    assert store.record(rr.validate_resource_run("run-1", rr.EVENT_TYPE, run())) is False   # (host, session) once
    columns = ("host, session_id, project, agent_kind, started_at, ended_at, peak_memory_bytes, cpu_seconds, "
               "disk_growth_bytes, exit_reason, hit_limit")
    insert = f"INSERT INTO resource_runs ({columns}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    good = ("beast", "run-x", "p", "claude", T0, T1, 1, 1.0, None, "exited", False)
    for index, bad in ((6, -1), (7, -0.5), (8, -1), (9, "segfault"), (5, "2026-11-04T09:00:00+00:00"),
                       (3, "Claude"), (0, "bad host")):
        row = list(good)
        row[index] = bad
        with pytest.raises(psycopg2.errors.CheckViolation):
            _sql(postgres, insert, tuple(row))
    _sql(postgres, insert, good)
    for i in range(1, 21):
        _sql(postgres, insert, ("bender", f"seed-{i}", "seeded", "pi", T0, T1, i * GIB, float(i * 10),
                                None if i in (7, 8) else i * MB, "exited", False))
    [profile] = store.profiles("seeded")["profiles"]
    assert profile["count"] == 20
    assert profile["metrics"]["peak_memory_bytes"] == {"count": 20, "median": 10.5 * GIB,
                                                       "p95": pytest.approx(19.05 * GIB), "max": 20 * GIB}
    assert profile["metrics"]["cpu_seconds"]["median"] == 105.0
    assert profile["metrics"]["disk_growth_bytes"]["count"] == 18
    down = re.search(r"DROP TABLE IF EXISTS [\w, ]+;", SQL.read_text()).group(0)
    _sql(postgres, down)
    assert _sql(postgres, "SELECT to_regclass('resource_runs') IS NULL")[0][0] is True
    assert _sql(postgres, "SELECT count(*) FROM pg_indexes WHERE indexname = 'resource_runs_profile_idx'")[0][0] == 0
    _sql(postgres, SQL.read_text())   # and up again after down
    assert _sql(postgres, "SELECT count(*) FROM resource_runs")[0][0] == 0
