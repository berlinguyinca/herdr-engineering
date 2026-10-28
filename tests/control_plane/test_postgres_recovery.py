"""Recovery through real PostgresSource; never connect to an operator database."""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.bridge import PostgresSource, _recovery_events_sql, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.control_plane.test_bridge import FakeSource
from tests.e2e.pg import ThrowawayPostgres, find_bins

asyncpg = pytest.importorskip("asyncpg")


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    bins = find_bins({})
    if bins is None:
        pytest.skip("PostgreSQL server binaries are required")
    db = ThrowawayPostgres(bins, tmp_path_factory.mktemp("recovery-pg")).start()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def db(pg):
    pg.query("TRUNCATE fabric_events")
    if pg.query("SELECT to_regclass('recovery_checkpoint_manifests') AS name")[0]["name"]:
        pg.query("TRUNCATE recovery_checkpoint_manifests CASCADE")
    return pg


def _source(db):
    # Explicit loop ownership closes test connections and leaves no daemon threads.
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    source = PostgresSource(conn, run=loop.run_until_complete)
    try:
        rows = source.events()
        repo = ControlPlaneRepo(now=lambda: 1793059200)
        repo.inspector._clock = lambda: 1793059200
        load_into(repo, source)
        return repo, rows
    finally:
        loop.run_until_complete(conn.close())
        loop.close()


def _row(kind, eid, event, payload, n, **extra):
    stamp = datetime(2026, 10, 26, tzinfo=UTC) + timedelta(seconds=n)
    return dict(event_id=f"{kind}-{eid}-{n}", entity_type=kind, entity_id=eid,
                event_type=event, payload=payload, sequence=n,
                source_timestamp=stamp, ingest_timestamp=stamp, **extra)


def _insert(db, rows):
    columns = ("event_id", "entity_type", "entity_id", "event_type", "payload", "sequence",
               "source_timestamp", "ingest_timestamp", "source", "source_sequence", "actor")

    async def write():
        conn = await asyncpg.connect(db.dsn)
        try:
            await conn.copy_records_to_table("fabric_events", columns=columns, records=[
                tuple(json.dumps(row.get(k)) if k in {"payload", "actor"} and row.get(k) is not None
                      else row.get(k) for k in columns) for row in rows])
        finally:
            await conn.close()
    asyncio.run(write())


def _reference(db):
    rows = [dict(r) for r in db.query(
        "SELECT * FROM fabric_events ORDER BY ingest_timestamp, sequence, event_id")]
    repo = ControlPlaneRepo(now=lambda: 1793059200)
    repo.inspector._clock = lambda: 1793059200
    load_into(repo, FakeSource(events=rows))
    return repo, rows


def _projections(repo):
    return {key: repo.export_state()[key] for key in ("missions", "sessions", "services", "hosts", "delivery")}


def _inspector_view(repo, sid):
    view = repo.inspector.query(sid, "operator")
    # The SSE epoch is intentionally new on every process, not durable state.
    view.pop("epoch")
    return view


def _snapshot():
    return dict(service_name="api", host_id="h", port=8000, status="healthy",
                route="http://api.test", description="API", owner="operator", session_id="s",
                ownership=None, unavailable_since=None, metadata_updated_at="2026-10-26T00:00:00+00:00")


def test_sink_only_entities_and_partial_history_survive_real_source(db):
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    try:
        live = ControlPlaneRepo(sink=DurableEventSink(conn=conn, run=loop.run_until_complete))
        live.upsert_mission("m", title="Persistent mission")
        live.upsert_session("s", host_id="h", mission_id="m", model="model", cwd="/repo")
        live.upsert_session("s", status="stopped", summary="finished")
        live.upsert_service("h", "api", 8000, session_id="s")
        live.upsert_host("h", "host", "100.64.0.1")
        live.record_event("ci_fix", "fix", "CIFixRequested", {"repository": "o/r"})
        live.record_event("ci_fix", "fix", "CIFixCancelRequested", {"reason": "stop"})
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    for table in ("missions", "sessions", "services", "hosts"):
        assert db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0
    expected, original = _reference(db)
    recovered, selected = _source(db)
    assert _projections(recovered) == _projections(expected)
    assert recovered.get_session("s")["status"] == "stopped"
    assert recovered.get_mission("m")["sessions"] == ["s"]
    assert recovered.stream("ci_fix", "fix") == expected.stream("ci_fix", "fix")
    assert len(selected) == len(original)


@pytest.mark.parametrize("seed", range(8))
def test_random_partial_updates_match_full_replay_and_keep_ledger(db, seed):
    rng = random.Random(seed)
    rows = [_row("mission", "m", "MissionStarted", {"title": "mission", "stage": "active"}, 0)]
    for n in range(1, 401):
        eid = f"svc-{rng.randrange(4)}"
        p = _snapshot()
        choice = rng.randrange(10)
        if choice < 5:
            # Long unchanged snapshot runs, with a changing observation timestamp.
            p["metadata_updated_at"] = f"2026-10-26T00:{n // 60:02}:{n % 60:02}+00:00"
            event = "ServiceUpdated"
        elif choice == 5:
            p, event = {"owner": f"owner-{n}", "route": f"http://{n}.test"}, "ServiceUpdated"
        elif choice == 6:
            p, event = {"status": "unavailable"}, "ServiceUnavailable"
        elif choice == 7:
            p, event = {"session_id": None, "commit_sha": None, "metadata_updated_at": p["metadata_updated_at"]}, "ServiceMetadataObserved"
        elif choice == 8:
            p, event = {"retired_at": p["metadata_updated_at"]}, "ServiceRetired"
        else:
            p.update(status="unavailable", unavailable_since="2026-10-25T00:00:00+00:00", route=None, owner="")
            event = "ServiceUpdated"
        rows.append(_row("service", eid, event, p, n))
        if n % 11 == 0:
            rows.append(_row("session", "s", "SessionUpdated", {
                "host_id": "h", "mission_id": "m", "model": "model", "agent_role": "coder",
                "status": "stopped" if n > 300 else "active", "commit_sha": None,
                "interactive_ready": False}, n))
        if n % 17 == 0:
            rows.append(_row("session", "s", "SessionCommandSettled", {"status": "delivered"}, n + 1000))
        if n % 13 == 0:
            rows.append(_row("host", "h", "HostHeartbeat", {"host_name": "host", "cpu": n}, n))
        if n % 19 == 0:
            rows.append(_row("service", eid, "service.observed", {"_envelope": {"content_sha256": str(n)}},
                             n + 2000, source="reporter", source_sequence=n, actor={"id": "a"}))
    # Ingest order need not be sequence order; equal timestamps are also valid.
    for row in rng.sample(rows, 20):
        row["ingest_timestamp"] -= timedelta(seconds=rng.randrange(100))
    _insert(db, rows)
    expected, original = _reference(db)
    recovered, selected = _source(db)
    assert _projections(recovered) == _projections(expected)
    assert recovered.ledger.source_state("reporter", "a") == expected.ledger.source_state("reporter", "a")
    assert recovered.ledger._events == expected.ledger._events
    assert {r["event_id"] for r in selected if r["source_sequence"] is not None} == {
        r["event_id"] for r in original if r["source_sequence"] is not None}
    # Partial/event histories and sequence maxima survive, not only final values.
    assert [e for e in recovered.stream("session", "s") if e["event_type"] == "SessionCommandSettled"] == [
        e for e in expected.stream("session", "s") if e["event_type"] == "SessionCommandSettled"]
    for eid in {r["entity_id"] for r in rows if r["entity_type"] == "service"}:
        assert max(e["sequence"] for e in recovered.stream("service", eid)) == max(
            e["sequence"] for e in expected.stream("service", eid))
    assert len(selected) < len(original)
    again, _ = _source(db)
    assert _projections(again) == _projections(recovered)
    assert db.query("SELECT count(*) AS n FROM fabric_events")[0]["n"] == len(rows)


def test_snapshot_runs_compact_but_do_not_cross_partial_updates(db):
    rows = [_row("service", "svc", "ServiceUpdated", _snapshot(), n) for n in range(100)]
    rows.insert(50, _row("service", "svc", "ServiceMetadataObserved", {"session_id": None}, 500))
    rows[50]["ingest_timestamp"] = rows[49]["ingest_timestamp"] + timedelta(microseconds=1)
    _insert(db, rows)
    expected, _ = _reference(db)
    recovered, selected = _source(db)
    assert _projections(recovered) == _projections(expected)
    assert len(selected) == 5
    assert [r["sequence"] for r in selected] == [0, 49, 500, 50, 99]
    assert recovered.record_event("service", "svc", "ServiceRetired", {})["sequence"] == 501


def test_production_cardinality_snapshot_recovery_and_database_restart(db):
    """Audited row/type/cardinality mix, synthetic payloads; not a latency bound."""
    observation = {**_snapshot(), "service_id": "svc", "source_port": 8000,
                   "discovery_source": "native_listener", "endpoint_scope": "local_only", "route": None,
                   "created_at": "2026-10-26T00:00:00+00:00", "updated_at": "2026-10-26T00:00:00+00:00",
                   "service_created_at": "2026-10-26T00:00:00+00:00",
                   "service_updated_at": "2026-10-26T00:00:00+00:00",
                   "repository_id": None, "commit_sha": None,
                   "listener_identity": {"pid": 123, "start": "birth"}, "verified_origin": None,
                   "metadata_observation": {"state": "observed", "observed_at": "2026-10-26T00:00:00+00:00"}}
    specs = [
        ("service", "legacy", 565, 565, "ServiceRegistered", _snapshot()),
        ("service", "legacy", 565, 300463, "ServiceUpdated", _snapshot()),
        ("service", "legacy", 565, 174469, "ServiceMetadataObserved", observation),
        ("service", "listener", 3427, 1039182, "ServiceListenerObserved", observation),
        ("service", "legacy", 11, 11, "ServiceRetired", {"retired_at": "2026-10-26T01:00:00+00:00"}),
        ("session", "session", 391, 399, "SessionStarted", {"host_id": "h", "mission_id": "m", "status": "active"}),
        ("session", "session", 391, 86845, "SessionUpdated", {
            "host_id": "h", "mission_id": "m", "agent_role": "coder", "model": "model", "status": "stopped"}),
        ("host", "host", 4, 12153, "HostHeartbeat", {"host_name": "host", "tailnet_ip": "100.64.0.1", "cpu": 0, "mem": 0}),
    ]
    inspector_counts = dict(tool=86285, usage=38583, message=19582, lifecycle=3228,
                            file=2944, association=1622, capabilities=854, test=498, model=123)
    for kind, count in inspector_counts.items():
        payload = {"kind": kind, "role": "assistant", "text": "captured", "tool": "bash", "call_id": "call",
                   "phase": "end", "output": "complete", "exit_code": 0, "input_tokens": 1,
                   "output_tokens": 1, "path": "a.py", "op": "edit", "diff": "+line", "model": "model",
                   "mission_id": "m", "reasoning": "none", "run_id": "run", "passed": 1}
        specs.append(("inspector", "session", 391, count, f"inspector.{kind}", payload))
    for phase, (kind, prefix, ids, total, event, payload) in enumerate(specs):
        db.query("""
            INSERT INTO fabric_events (event_id, entity_type, entity_id, event_type,
                sequence, source_timestamp, ingest_timestamp, payload)
            SELECT $3 || '-' || n, $1, $6 || '-' || (n % $2), $3, $7 + n / $2,
                '2026-10-26T00:00:00+00:00'::timestamptz + ($7 + n / $2) * interval '1 second',
                '2026-10-26T00:00:00+00:00'::timestamptz + ($7 + n / $2) * interval '1 second',
                $4::jsonb || CASE WHEN $1 = 'service' AND $3 <> 'ServiceRetired' THEN jsonb_build_object(
                    'metadata_updated_at', ('2026-10-26T00:00:00+00:00'::timestamptz + ($7 + n / $2) * interval '1 second')::text,
                    'updated_at', ('2026-10-26T00:00:00+00:00'::timestamptz + ($7 + n / $2) * interval '1 second')::text,
                    'metadata_observation', jsonb_build_object('state',
                        CASE WHEN $3 = 'ServiceListenerObserved' AND n / $2 >= 200 THEN 'missing' ELSE 'observed' END, 'observed_at',
                        ('2026-10-26T00:00:00+00:00'::timestamptz + ($7 + n / $2) * interval '1 second')::text),
                    'status', CASE WHEN $3 = 'ServiceListenerObserved' AND n / $2 >= 200 THEN 'unknown'
                                   WHEN n / $2 >= 200 THEN 'unavailable' ELSE 'healthy' END,
                    'unavailable_since', CASE WHEN $3 <> 'ServiceListenerObserved' AND n / $2 >= 200 THEN '2026-10-26T00:03:20+00:00' END)
                    WHEN $1 = 'inspector' THEN jsonb_build_object('event_id', $3 || '-' || n, '_sequence', $7 + n / $2)
                    ELSE '{}'::jsonb END
            FROM generate_series(0, $5::integer - 1) n
        """, kind, ids, event, json.dumps(payload), total, prefix, phase * 1000)
    # Explicitly exercise the pre-migration fallback, only in this disposable DB.
    db.query("DROP INDEX IF EXISTS idx_fabric_events_recovery_arrival")
    db.query("ANALYZE fabric_events")
    before = db.query("SELECT entity_type, count(*) AS n, count(DISTINCT entity_id) AS ids "
                      "FROM fabric_events GROUP BY entity_type ORDER BY entity_type")
    assert [(r["entity_type"], r["n"], r["ids"]) for r in before] == [
        ("host", 12153, 4), ("inspector", 153719, 391), ("service", 1514690, 3992), ("session", 87244, 391)]
    start = time.monotonic()
    recovered, rows = _source(db)
    elapsed = time.monotonic() - start
    assert len(recovered._services) == 3992 and len(recovered._sessions) == 391 and len(recovered._hosts) == 4
    assert sum(r["entity_type"] == "inspector" for r in rows) == 153719
    assert sum(r["entity_type"] == "service" for r in rows) < 22000
    assert all(r["status"] == "stopped" for r in recovered._sessions.values())
    for eid, service in recovered._services.items():
        if eid.startswith("listener"):
            assert service["status"] == "unknown"
            assert service["unavailable_since"] is None
            assert service["url"] is None
            assert service["endpoint_scope"] == "local_only"
            assert datetime.fromisoformat(service["metadata_updated_at"]) == datetime.fromisoformat(service["updated_at"])
        else:
            assert service["status"] == "unavailable"
            assert service["unavailable_since"] == "2026-10-26T00:03:20+00:00"
    print(f"\nRecovery mixed workload: 1,767,806 durable rows -> {len(rows):,} rows; existing indexes: {elapsed:.2f}s", flush=True)
    # Index DDL is exercised ONLY inside this disposable cluster.
    db.query("CREATE INDEX IF NOT EXISTS idx_fabric_events_recovery_arrival ON fabric_events "
             "(entity_type, entity_id, ingest_timestamp, sequence, event_id)")
    db.query("ANALYZE fabric_events")
    plan = db.query("EXPLAIN (FORMAT JSON) " + _recovery_events_sql("fabric_events"))[0][0]
    print("Arrival index selected:", "idx_fabric_events_recovery_arrival" in plan, "; TID scan selected:", "Tid Scan" in plan, flush=True)
    from tests.control_plane.test_recovery_checkpoint import _build
    checkpoint = _build(db)
    assert checkpoint["covered_events"] == 1767806
    assert checkpoint["selected_events"] == len(rows)
    print(f"Offline checkpoint build: {checkpoint['elapsed_seconds']:.2f}s", flush=True)
    db.stop()
    db.start_existing()
    start = time.monotonic()
    restarted, replay = _source(db)
    restart_elapsed = time.monotonic() - start
    assert _projections(restarted) == _projections(recovered)
    assert [r["event_id"] for r in replay] == [r["event_id"] for r in rows]
    assert db.query("SELECT count(*) AS n FROM fabric_events")[0]["n"] == 1767806
    assert _inspector_view(restarted, "session-0") == _inspector_view(recovered, "session-0")
    print(f"Mixed checkpoint recovery after Postgres restart: {restart_elapsed:.2f}s", flush=True)
    # Timings are evidence, not a hard bound: changing/private histories remain
    # linear and this disposable cluster shares the host's CPU and storage.


def test_compaction_keeps_freshness_boundaries_and_actor_scoped_replay(db):
    rows = []
    for n in range(10):
        payload = _snapshot()
        payload["metadata_updated_at"] = f"2026-10-26T00:00:{n:02}+00:00"
        rows.append(_row("service", "svc", "ServiceUpdated", payload, n))
    # Two principals can legitimately share event_id/source/sequence. Physical
    # row selection must not conflate their replay identities.
    for actor in ("a", "b"):
        row = _row("session", "s", "session.updated", {"_envelope": {"content_sha256": actor}},
                   20, source="reporter", source_sequence=7, actor={"id": actor})
        rows.append(row)
    _insert(db, rows)
    recovered, selected = _source(db)
    expected, _ = _reference(db)
    assert _projections(recovered) == _projections(expected)
    assert [r["sequence"] for r in selected if r["entity_type"] == "service"] == [0, 9]
    service = recovered.get_service("svc")
    assert service["created_at"] == "2026-10-26T00:00:00+00:00"
    assert service["updated_at"] == service["metadata_updated_at"] == "2026-10-26T00:00:09+00:00"
    assert recovered.ledger._events == expected.ledger._events
    for actor in ("a", "b"):
        assert recovered.ledger.check(dict(event_id="session-s-20", source="reporter", source_sequence=7,
                                           actor={"id": actor}, content_hash=actor)) == "duplicate"


def test_metadata_before_first_health_report_has_a_complete_service_shape(db):
    _insert(db, [
        _row("service", "svc", "ServiceMetadataObserved", {"session_id": "s", "ownership": {"kind": "session"}}, 0),
        _row("service", "svc", "ServiceUpdated", {"port": 8080, "host_id": "h"}, 1),
    ])
    repo, rows = _source(db)
    service = repo.get_service("svc")
    assert service["status"] == "healthy" and service["url"] == "http://h:8080"
    assert service["session_id"] == "s" and service["ownership"] == {"kind": "session"}
    assert len(rows) == 2


@pytest.mark.parametrize("event", ["ServiceMetadataObserved", "ServiceListenerObserved"])
def test_observation_clocks_compact_without_losing_resets_or_identity_changes(db, event):
    rows = []
    for n in range(100):
        stamp = f"2026-10-26T00:{n // 60:02}:{n % 60:02}+00:00"
        payload = {**_snapshot(), "service_id": "svc", "source_port": 8000,
                   "discovery_source": "native_listener", "endpoint_scope": "local_only", "route": None,
                   "created_at": "2026-10-26T00:00:00+00:00", "updated_at": stamp,
                   "service_created_at": "2026-10-26T00:00:00+00:00",
                   "service_updated_at": "2026-10-26T00:00:00+00:00",
                   "repository_id": "github.com/o/r", "commit_sha": "a" * 40,
                   "listener_identity": {"pid": 123 if n < 50 else 456, "start": "birth"},
                   "metadata_updated_at": stamp,
                   "metadata_observation": {"state": "observed", "observed_at": stamp, "stale_after_seconds": 90},
                   "ownership": {"kind": "herdr_process_ancestry", "observed_at": stamp},
                   "verified_origin": {"verified_at": stamp, "ownership": {"observed_at": stamp}}}
        if n >= 75:
            payload.update(ownership=None, verified_origin=None, session_id=None, commit_sha=None)
            payload["metadata_observation"]["state"] = "missing"
        rows.append(_row("service", "svc", event, payload, n))
    _insert(db, rows)
    expected, _ = _reference(db)
    recovered, compact = _source(db)
    assert _projections(recovered) == _projections(expected)
    assert [row["sequence"] for row in compact] == [0, 49, 50, 74, 75, 99]
    service = recovered._services["svc"]
    assert service["session_id"] is None and service["ownership"] is None and service["commit_sha"] is None
    assert service["listener_identity"]["pid"] == 456
    assert service["metadata_observation"]["observed_at"] == "2026-10-26T00:01:39+00:00"
    if event == "ServiceMetadataObserved":
        assert service["updated_at"] == "2026-10-26T00:00:00+00:00"


@pytest.mark.parametrize("checkpoint", [False, True])
def test_private_projections_and_mutations_match_full_postgres_replay(db, checkpoint):
    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    from herdr_engineering.control_plane.ingest import IngestService
    from tests.control_plane.test_collab import _digest
    from tests.control_plane.test_collab_tasks import USERS, W
    from tests.control_plane.test_graph import Feed
    from tests.control_plane.test_inspector import tool

    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    auth = StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()})
    events = [tool("call", "start", event_id="start", tool="bash"),
              {"kind": "message", "event_id": "message", "text": "durable conversation"},
              tool("call", "update", event_id="update", output="working"),
              tool("call", "end", event_id="end", exit_code=1, is_error=True),
              {"kind": "usage", "event_id": "usage", "input_tokens": 17, "output_tokens": 9},
              {"kind": "association", "event_id": "association", "mission_id": "m", "branch": "feature"}]
    try:
        sink = DurableEventSink(conn=conn, run=loop.run_until_complete)
        live = ControlPlaneRepo(sink=sink, now=lambda: 1793059200)
        live.inspector._clock = lambda: 1793059200
        live.upsert_session("s", host_id="h", name="Ada")
        profile = live.get_session("s")
        live.rename_agent("s", "Grace Hopper", profile["name_version"])
        live.persist_agent_summary({"kind": "agent_summary", "session_id": "s", "summary": {"overview": "Testing persistence"}})
        assert live.inspector.ingest("s", {"events": events})["accepted"] == len(events)
        live.inspector.set_prefs({"layout": "stacked"})
        live.inspector.set_prefs({"density": "compact"}, "s")
        live.upsert_host("h", "host", "100.64.0.1")
        live.host_metrics.record("h", {"cpu": 10, "mem": 20})
        live._now = lambda: 1793059500
        live.host_metrics.record("h", {"cpu": 30, "mem": 40})
        live.host_metrics.observe_state("h", {"versions": {"herdr": "1.2"}}, {"cpu": 30})
        feed = Feed(IngestService(live))
        feed.node("agent:a", label="Agent")
        feed.edge("delegation", "agent:a", "task:t")
        feed.send("node.state", {"node_id": "task:t", "state": "done"})
        collab = CollabService(auth, sink=sink)
        W(collab)
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    expected, original = _reference(db)
    if checkpoint:
        from tests.control_plane.test_recovery_checkpoint import _build
        _build(db)
    recovered, rows = _source(db)
    assert len(rows) == len(original)
    assert recovered.graph.snapshot() == expected.graph.snapshot() == live.graph.snapshot()
    assert recovered.get_session("s")["display_name"] == "Grace Hopper"
    assert recovered.export_state()["agent_projections"] == expected.export_state()["agent_projections"]
    assert recovered.agent_summary_events() == expected.agent_summary_events()
    assert recovered.agent_summary_events()[0]["summary"]["overview"] == "Testing persistence"
    assert recovered.inspector.get_prefs() == expected.inspector.get_prefs()
    assert recovered.inspector.get_prefs("s") == expected.inspector.get_prefs("s")
    assert _inspector_view(recovered, "s") == _inspector_view(expected, "s")
    detail = recovered.inspector.query("s", "operator")
    assert detail["summary"]["usage"]["input_tokens"] == 17
    assert recovered.inspector.query("s", "operator", view="tools")["events"][0]["data"]["status"] == "error"
    assert recovered.inspector.ingest("s", {"events": events})["duplicates"] == len(events)
    assert recovered.host_history("h", "24h") == expected.host_history("h", "24h")
    assert recovered.host_history("h", "24h")
    assert recovered._hosts["h"] == expected._hosts["h"]
    assert recovered.ledger._events == expected.ledger._events
    left, right = CollabService(auth), CollabService(auth)
    assert left.hydrate(recovered.take_collab_replay()) == right.hydrate(expected.take_collab_replay()) > 0
    assert left._rooms == right._rooms == collab._rooms
    assert left._log == right._log
    private = {"collab", "inspector", "agent_identity", "agent_summary", "host_metrics", "lineage"}
    assert not any(row[0] in private for row in recovered._event_order)


def test_changing_snapshots_and_skew_are_not_dropped(db):
    rows = []
    for n in range(2000):
        payload = _snapshot()
        payload.update(owner=f"owner-{n}", session_id=None if n % 2 else "s",
                       unavailable_since=None if n % 2 else "2026-10-26T00:00:00+00:00",
                       status="healthy" if n % 2 else "unavailable")
        row = _row("service", "svc", "ServiceUpdated", payload, n)
        row["source_timestamp"] -= timedelta(days=n % 7)
        row["ingest_timestamp"] -= timedelta(seconds=n % 19)
        rows.append(row)
    _insert(db, rows)
    expected, original = _reference(db)
    recovered, selected = _source(db)
    assert len(selected) == len(original) == 2000
    assert recovered.stream("service", "svc") == expected.stream("service", "svc")
    assert _projections(recovered) == _projections(expected)


def test_failed_postgres_read_does_not_publish_partial_recovery(db):
    from herdr_engineering.control_plane.api import ControlPlaneServer

    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    repo = ControlPlaneRepo()
    repo.upsert_mission("existing", title="not partially replaced")
    before = repo.export_state()
    try:
        source = PostgresSource(conn, run=loop.run_until_complete, events_table="missing_recovery_table")
        with pytest.raises(asyncpg.UndefinedTableError):
            ControlPlaneServer(repo, host="127.0.0.1", port=0, source=source, allow_open=True)
        assert repo.export_state() == before
        assert source._cache is None
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
