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
                      "hosts": 1, "events": 1}
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


def test_load_into_empty_source():
    repo = ControlPlaneRepo()
    counts = load_into(repo, FakeSource())
    assert counts == {"missions": 0, "sessions": 0, "services": 0,
                      "hosts": 0, "events": 0}
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
