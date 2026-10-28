import asyncio
import threading
import urllib.request

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import PostgresSource, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class FakeSource:
    def __init__(self, missions=(), sessions=(), services=(), hosts=(),
                 events=()):
        self._m, self._s, self._sv, self._h, self._e = (
            list(missions), list(sessions), list(services), list(hosts),
            list(events))

    def missions(self):
        return self._m

    def sessions(self):
        return self._s

    def services(self):
        return self._sv

    def hosts(self):
        return self._h

    def events(self):
        return self._e


def _row(**kw):
    return dict(kw)


def test_load_into_populates_repo():
    src = FakeSource(
        missions=[_row(mission_id="m1", title="Bridge mission", purpose="p",
                       stage="implementation", status="implementation",
                       created_at="2026-01-01T00:00:00+00:00")],
        sessions=[_row(session_id="s1", mission_id="m1", agent_role="coder",
                       model="gpt-4", status="active", messages=3,
                       tool_calls=2, created_at="2026-01-02T00:00:00+00:00")],
        services=[_row(service_id="sv1", service_name="api", host_id="h1",
                       port=8080, status="healthy",
                       created_at="2026-01-03T00:00:00+00:00")],
        hosts=[_row(host_id="h1", hostname="bender", tailnet_ip="100.64.1.1",
                    status="healthy", created_at="2026-01-04T00:00:00+00:00")],
        events=[_row(event_id="e1", sequence=0, event_type="MissionCreated",
                     entity_type="mission", entity_id="m1", mission_id="m1",
                     session_id=None, payload={"title": "x"},
                     source_timestamp="2026-01-01T00:00:00+00:00",
                     ingest_timestamp="2026-01-01T00:00:01+00:00",
                     schema_version=1)],
    )
    repo = ControlPlaneRepo()
    counts = load_into(repo, src)
    assert counts == {"missions": 1, "sessions": 1, "services": 1,
                      "hosts": 1, "events": 1, "artifacts": 0}
    m = repo.get_mission("m1")
    assert m["title"] == "Bridge mission"
    assert m["stage"] == "implementation"
    assert m["sessions"] == ["s1"]
    s = repo.get_session("s1")
    assert s["model"] == "gpt-4" and s["messages"] == 3
    sv = repo.get_service("sv1")
    assert sv["url"] == "http://h1:8080"
    h = repo.get_host("h1")
    assert h["host_name"] == "bender"
    assert repo.stream("mission", "m1")[0]["event_type"] == "MissionCreated"


def test_load_into_idempotent_and_events_ordered():
    rows = [_row(event_id=f"e{i}", sequence=i, event_type=f"E{i}",
                 entity_type="session", entity_id="s1", mission_id=None,
                 session_id="s1", payload={},
                 source_timestamp=f"2026-01-01T00:00:0{i}+00:00",
                 ingest_timestamp=f"2026-01-01T00:00:0{i}+00:00",
                 schema_version=1) for i in range(3)]
    repo = ControlPlaneRepo()
    load_into(repo, FakeSource(events=rows))
    seqs = [e["sequence"] for e in repo.stream("session", "s1")]
    assert seqs == [0, 1, 2]
    # recent_events newest-first
    assert repo.recent_events(limit=1)[0]["event_type"] == "E2"


def test_server_hydrates_from_source():
    src = FakeSource(missions=[_row(mission_id="m1", title="from pg",
                                    status="active",
                                    created_at="2026-01-01T00:00:00+00:00")])
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, source=src)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{svr.port}/api/v1/missions") as resp:
            body = resp.read().decode()
        assert '"title": "from pg"' in body
    finally:
        svr.shutdown()


def test_load_into_reconstructs_entities_from_event_log():
    """Durable events (the only thing the write-through sink persists) must
    rebuild hosts/missions/sessions on hydration even when the structured
    tables are empty."""
    ts = "2026-01-01T00:00:00+00:00"
    # asyncpg yields jsonb as a JSON string, so exercise that path too.
    events = [
        _row(event_id="e1", sequence=0, event_type="host.registered",
             entity_type="host", entity_id="bender",
             payload='{"host_name": "bender", "tailnet_ip": "100.104.39.6"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e2", sequence=0, event_type="mission.started",
             entity_type="mission", entity_id="MSN-1",
             payload='{"title": "Reconstructed", "purpose": "p", "stage": "active", "cwd": "/work/msn1"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e3", sequence=0, event_type="session.started",
             entity_type="session", entity_id="w1H:t1",
             payload='{"host_id": "bender", "mission_id": "MSN-1", "agent_role": "pi", "cwd": "/work/msn1", "branch": "main@abc123", "pid": "1234", "cmdline": "pi --x", "summary": "fixing bug", "attach": "herdr workspace focus MSN-1"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e4", sequence=0, event_type="ArtifactRegistered",
             entity_type="artifact", entity_id="artifact_1",
             payload='{"filename": "note.txt", "sha256": "abc123", "storage_key": "sha256/ab/c1/abc123", "size_bytes": 5, "mime_type": "text/plain", "session_id": "w1H:t1", "mission_id": "MSN-1"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
    ]
    src = FakeSource(events=events)  # structured tables intentionally empty
    repo = ControlPlaneRepo()
    counts = load_into(repo, src)
    assert counts["hosts"] == 1 and counts["missions"] == 1
    assert counts["sessions"] == 1
    assert counts["artifacts"] == 1
    h = repo.get_host("bender")
    assert h["host_name"] == "bender" and h["tailnet_ip"] == "100.104.39.6"
    m = repo.get_mission("MSN-1")
    assert m["title"] == "Reconstructed" and m["stage"] == "active"
    assert m["cwd"] == "/work/msn1"
    assert m["sessions"] == ["w1H:t1"]
    s = repo.get_session("w1H:t1")
    assert s["mission_id"] == "MSN-1" and s["agent_role"] == "pi"
    assert s["cwd"] == "/work/msn1" and s["branch"] == "main@abc123"
    assert s["pid"] == "1234" and s["cmdline"] == "pi --x"
    assert s["summary"] == "fixing bug"
    assert s["attach"] == "herdr workspace focus MSN-1"
    a = repo.get_artifact("artifact_1")
    assert a["original_filename"] == "note.txt"
    assert a["storage_key"] == "sha256/ab/c1/abc123"
    assert a["sha256"] == "abc123" and a["size_bytes"] == 5
    assert a["mission_id"] == "MSN-1"


def test_load_into_reconstructs_services_from_event_log():
    """Services, like hosts/missions/sessions, must be rebuilt from durable
    events on hydration even when the structured table is empty (the write-
    through sink persists only ``fabric_events``)."""
    ts = "2026-01-01T00:00:00+00:00"
    events = [
        _row(event_id="e1", sequence=0, event_type="ServiceRegistered",
             entity_type="service", entity_id="svc_1",
             payload='{"service_name": "herdr-control-plane", "host_id": "bender", "port": 8080, "route": "http://bender:8080", "status": "healthy"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e2", sequence=1, event_type="ServiceUpdated",
             entity_type="service", entity_id="svc_1",
             payload='{"service_name": "herdr-control-plane", "host_id": "bender", "port": 8081, "route": "http://bender:8081", "status": "healthy", "description": "The control-plane web UI and API", "owner": "python"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
    ]
    src = FakeSource(events=events)  # structured services table intentionally empty
    repo = ControlPlaneRepo()
    counts = load_into(repo, src)
    assert counts["services"] == 1
    svc = repo.get_service("svc_1")
    assert svc["service_name"] == "herdr-control-plane"
    assert svc["host_id"] == "bender"
    # the latest event wins
    assert svc["port"] == 8081
    assert svc["route"] == "http://bender:8081"
    assert svc["status"] == "healthy"
    # description + owner survive the replay
    assert svc["description"] == "The control-plane web UI and API"
    assert svc["owner"] == "python"


def test_load_into_derives_description_for_legacy_services():
    """Service events predating the description field still hydrate to a
    record that carries a human description (read-time derivation)."""
    ts = "2026-01-01T00:00:00+00:00"
    events = [
        _row(event_id="e1", sequence=0, event_type="ServiceRegistered",
             entity_type="service", entity_id="svc_1",
             payload='{"service_name": "node on mac.lan (port 4000)", "host_id": "mac.lan", "port": 18050, "route": "http://dev.lan:18050", "status": "healthy"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
    ]
    src = FakeSource(events=events)
    repo = ControlPlaneRepo()
    load_into(repo, src)
    svc = repo.get_service("svc_1")
    assert svc["description"]
    assert "mac.lan" in svc["description"]


def test_load_into_replays_delivery_entities_from_event_log():
    """Delivery projections (test_run/build/review/pull_request) must be rebuilt
    from durable events on hydration. The write-through sink persists only
    fabric_events, so without this replay a restart would silently lose every
    delivery record (issue #40)."""
    ts = "2026-01-01T00:00:00+00:00"
    events = [
        _row(event_id="e1", sequence=0, event_type="TestRunStarted",
             entity_type="test_run", entity_id="run1", mission_id="pi:M1",
             payload='{"mission_id": "pi:M1", "kind": "test", "status": "ok", "passed": 2, "failed": 0, "total": 2}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e2", sequence=1, event_type="BuildStarted",
             entity_type="build", entity_id="build1", mission_id="pi:M1",
             payload='{"mission_id": "pi:M1", "status": "ok"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e3", sequence=2, event_type="ReviewStarted",
             entity_type="review", entity_id="rev1", mission_id="pi:M1",
             payload='{"mission_id": "pi:M1", "status": "in_review"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e4", sequence=3, event_type="ReviewFindingAdded",
             entity_type="review", entity_id="rev1", mission_id="pi:M1",
             payload='{"mission_id": "pi:M1", "severity": "medium", "summary": "leak", "status": "open"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e5", sequence=4, event_type="PullRequestOpened",
             entity_type="pull_request", entity_id="pr1", mission_id="pi:M1",
             payload='{"mission_id": "pi:M1", "title": "Fix bug", "url": "https://github.com/o/r/pull/1"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
    ]
    src = FakeSource(events=events)
    repo = ControlPlaneRepo()
    counts = load_into(repo, src)
    # Delivery entities survive the restart even with empty structured tables.
    assert counts["events"] == 5
    tr = repo.get_test_run("run1")
    assert tr["mission_id"] == "pi:M1" and tr["passed"] == 2 and tr["status"] == "ok"
    assert repo.list_test_runs(mission_id="pi:M1")[0]["test_run_id"] == "run1"
    build = repo.get_test_run("build1")
    assert build["mission_id"] == "pi:M1" and build["kind"] == "build"
    assert repo.get_review("rev1")["findings"][0]["summary"] == "leak"
    assert repo.list_reviews(mission_id="pi:M1")[0]["review_id"] == "rev1"
    assert repo.get_pull_request("pr1")["url"] == "https://github.com/o/r/pull/1"
    assert repo.list_pull_requests(mission_id="pi:M1")[0]["pr_id"] == "pr1"


def test_load_into_replays_usage_and_stage_analytics():
    """Usage analytics (#36) and stage analytics (#37) ride the same durable
    event log, so an event-only reload must rebuild them or they vanish after a
    restart (issue #40). Replay is idempotent: loading twice yields the same
    projections."""
    ts = "2026-01-01T00:00:00+00:00"
    events = [
        _row(event_id="e1", sequence=0, event_type="token_usage.recorded",
             entity_type="token_usage", entity_id="req1", mission_id="pi:M1",
             payload='{"mission_id": "pi:M1", "model": "gpt", "request_id": "req1", "input_tokens": 5, "output_tokens": 3}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e2", sequence=1, event_type="MissionStageChanged",
             entity_type="mission", entity_id="pi:M1", mission_id="pi:M1",
             payload='{"from": "planning", "to": "implementation"}',
             source_timestamp=ts, ingest_timestamp=ts, schema_version=1),
        _row(event_id="e3", sequence=2, event_type="MissionStageChanged",
             entity_type="mission", entity_id="pi:M1", mission_id="pi:M1",
             payload='{"from": "implementation", "to": "review"}',
             source_timestamp="2026-01-01T00:00:10+00:00",
             ingest_timestamp="2026-01-01T00:00:10+00:00", schema_version=1),
    ]
    src = FakeSource(events=events)
    repo = ControlPlaneRepo()
    load_into(repo, src)
    ua = repo.usage_analytics(mission_id="pi:M1")
    assert ua["tokens"]["total"]["input_tokens"] == 5
    assert ua["tokens"]["by_model"]["gpt"]["output_tokens"] == 3
    sa = repo.stage_analytics()
    # planning -> implementation closed a run (~10s); implementation is still open.
    assert sa["in_progress"]["pi:M1"] == ["review"]
    assert sa["stages"]["implementation"]["runs"] == 1
    assert sa["stages"]["implementation"]["p50_ms"] == 10000
    # Idempotent: reloading the same event log must not double the projections.
    repo2 = ControlPlaneRepo()
    load_into(repo2, src)
    ua2 = repo2.usage_analytics(mission_id="pi:M1")
    assert ua2["tokens"]["total"]["input_tokens"] == 5
    sa2 = repo2.stage_analytics()
    assert sa2["stages"]["implementation"]["runs"] == 1


def test_load_into_empty_source():
    repo = ControlPlaneRepo()
    counts = load_into(repo, FakeSource())
    assert counts == {"missions": 0, "sessions": 0, "services": 0,
                      "hosts": 0, "events": 0, "artifacts": 0}
    assert repo.list_missions() == []


def test_postgres_source_uses_provided_run():
    # A fake asyncpg conn; PostgresSource should hand its coroutine to `run`.
    calls = []

    class FakeConn:
        async def fetch(self, sql, *args):
            if "missions" in sql:
                return [{"mission_id": "m9", "title": "pg", "status": "active",
                         "created_at": "2026-01-01T00:00:00+00:00"}]
            if "sessions" in sql:
                return []
            if "services" in sql:
                return []
            if "hosts" in sql:
                return []
            if "fabric_events" in sql:
                return []
            return []

    def fake_run(coro):
        calls.append(coro)
        return asyncio.new_event_loop().run_until_complete(coro)

    src = PostgresSource(FakeConn(), run=fake_run)
    missions = src.missions()
    assert missions[0]["mission_id"] == "m9"
    assert len(calls) == 1  # cached — fetched once, reused for all resources


def test_postgres_source_preserves_event_only_host_session_and_service_state():
    calls = []
    rows = [
        {"event_id": "h0", "entity_type": "host", "entity_id": "observed-host",
         "event_type": "HostRegistered", "sequence": 0,
         "source_timestamp": "2026-10-26T12:00:00+00:00",
         "payload": {"host_name": "Observed host", "status": "healthy"}},
        {"event_id": "s0", "entity_type": "session", "entity_id": "observed-session",
         "event_type": "SessionStarted", "sequence": 0,
         "source_timestamp": "2026-10-26T12:00:00+00:00",
         "payload": {"host_id": "observed-host", "agent_role": "pi", "status": "active"}},
        {"event_id": "v0", "entity_type": "service", "entity_id": "observed-service",
         "event_type": "ServiceRegistered", "sequence": 0,
         "source_timestamp": "2026-10-26T12:00:00+00:00",
         "payload": {"host_id": "observed-host", "service_name": "Observed web", "port": 18001}},
    ]

    class FakeConn:
        async def fetch(self, sql, *args):
            calls.append(sql)
            if "fabric_events" in sql:
                assert "WHERE entity_type" not in sql
                return rows
            return []

    def fake_run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    restored = ControlPlaneRepo()
    load_into(restored, PostgresSource(FakeConn(), run=fake_run))
    assert restored.get_host("observed-host")["host_name"] == "Observed host"
    assert restored.get_session("observed-session")["agent_role"] == "pi"
    assert restored.get_service("observed-service")["service_name"] == "Observed web"
    event_sql = next(sql for sql in calls if "fabric_events" in sql)
    assert "LIMIT" not in event_sql
