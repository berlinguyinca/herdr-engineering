"""The durable sink is bounded and its failures are visible (health, counters, rate-limited log).

A hung database is a real TCP listener that accepts and never answers, or a real Postgres whose table is locked.
"""
import asyncio
import contextlib
import json
import logging
import socket
import threading
import time
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import _LoopRunner
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.control_plane.test_inspector_history import _postgres

EV = {"entity_type": "mission", "entity_id": "m1", "event_type": "MissionCreated", "sequence": 0, "payload": {}}


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setenv("HERDR_CP_SINK_CONNECT_TIMEOUT_S", "0.3")
    monkeypatch.setenv("HERDR_CP_SINK_COMMAND_TIMEOUT_S", "0.5")
    monkeypatch.setenv("HERDR_CP_SINK_WAIT_TIMEOUT_S", "1.5")
    monkeypatch.setenv("HERDR_CP_SINK_RETRY_COOLDOWN_S", "0")


@pytest.fixture()
def blackhole():
    """A listener that accepts connections and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            with contextlib.suppress(OSError):
                held.append(srv.accept()[0])

    threading.Thread(target=accept, daemon=True).start()
    yield f"postgresql://user:s3cretpw@127.0.0.1:{srv.getsockname()[1]}/db"
    stop.set()
    srv.close()
    for c in held:
        c.close()


def test_runner_timeout_cancels_the_coroutine():
    runner = _LoopRunner()
    cancelled = threading.Event()

    async def hang():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        runner.run(hang(), timeout=0.2)
    assert time.monotonic() - t0 < 2
    assert cancelled.wait(2)


def test_hung_database_is_bounded_and_counted(fast, blackhole):
    sink = DurableEventSink(blackhole)
    t0 = time.monotonic()
    with pytest.raises(Exception):
        sink.record_required(dict(EV))
    sink.record(dict(EV))                               # best effort: never raises
    assert time.monotonic() - t0 < 5
    h = sink.write_health()
    assert h["failed_writes"] == 2 and h["dropped_events"] == 1 and h["degraded"] is True
    assert h["last_failure_at"] and h["last_failure_reason"]
    assert "s3cretpw" not in json.dumps(h) and "127.0.0.1" not in json.dumps(h)


def test_best_effort_writes_back_off_while_failing(fast, blackhole, monkeypatch):
    monkeypatch.setenv("HERDR_CP_SINK_RETRY_COOLDOWN_S", "30")
    sink = DurableEventSink(blackhole)
    sink.record(dict(EV))                               # pays the connect timeout once
    t0 = time.monotonic()
    for _ in range(20):
        sink.record(dict(EV))
    assert time.monotonic() - t0 < 0.5                  # skipped, not retried: the repo lock is not held
    h = sink.write_health()
    assert h["dropped_events"] == 21 and h["failed_writes"] == 1


def test_failure_log_is_rate_limited_without_traceback(fast, blackhole, caplog):
    sink = DurableEventSink(blackhole)
    with caplog.at_level(logging.WARNING, logger="herdr_engineering.control_plane.sink"):
        for _ in range(4):
            sink.record(dict(EV))
    recs = [r for r in caplog.records if r.name.endswith("sink")]
    assert len(recs) == 1 and recs[0].exc_info is None
    assert "s3cretpw" not in recs[0].getMessage()


def test_repo_health_reports_degraded_sink(fast, blackhole):
    repo = ControlPlaneRepo(sink=DurableEventSink(blackhole))
    assert repo.health()["status"] == "ok" and "durable_sink_degraded" not in repo.health()
    repo.create_mission("x")                            # the sink write fails; the repo carries on
    h = repo.health()
    assert h["status"] == "ok" and h["durable_sink_degraded"] is True and h["durable_sink"]["failed_writes"] >= 1
    assert ControlPlaneRepo().health().get("durable_sink") is None


def test_health_endpoints_flag_degraded_and_keep_status_ok(fast, blackhole):
    repo = ControlPlaneRepo(sink=DurableEventSink(blackhole))
    repo.create_mission("x")
    srv = ControlPlaneServer(repo)
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    try:
        for path in ("/api/v1/health?probe=1", "/api/v1/health"):
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}{path}", timeout=5) as r:
                body = json.loads(r.read())
            assert body["status"] == "ok" and body["durable_sink_degraded"] is True, path
            assert "s3cretpw" not in json.dumps(body)
    finally:
        srv.shutdown()


def test_blocked_insert_times_out_discards_connection_and_recovers(fast, tmp_path):
    asyncpg = pytest.importorskip("asyncpg")
    with _postgres(tmp_path) as dsn:
        if dsn is None:
            pytest.skip("no postgres available")
        runner = _LoopRunner()
        locker = runner.run(asyncpg.connect(dsn))
        sink = DurableEventSink(dsn)
        sink.record_required({**EV, "event_id": "e0"})                  # healthy write opens the connection
        first = sink._store._conn
        assert sink.write_health()["degraded"] is False
        tx = locker.transaction()
        runner.run(tx.start())
        runner.run(locker.execute("LOCK TABLE fabric_events IN ACCESS EXCLUSIVE MODE"))
        t0 = time.monotonic()
        with pytest.raises(Exception):
            sink.record_required({**EV, "event_id": "e1", "sequence": 1})
        assert time.monotonic() - t0 < 3
        assert sink._store._conn is None                                  # discarded, not reused in an unknown state
        assert sink.write_health()["degraded"] is True
        runner.run(tx.rollback())
        sink.record_required({**EV, "event_id": "e2", "sequence": 2})    # reconnects
        assert sink._store._conn is not first
        assert sink.write_health()["degraded"] is False
        rows = runner.run(locker.fetch("SELECT event_id FROM fabric_events ORDER BY sequence"))
        # The timed-out insert may still commit once the lock lifts (the server only notices the dropped socket then):
        # a timeout means "unknown", and the event_id makes a retry idempotent.
        ids = [r["event_id"] for r in rows]
        assert ids[0] == "e0" and ids[-1] == "e2" and set(ids) <= {"e0", "e1", "e2"}
        runner.run(locker.close())


def test_business_errors_are_not_write_failures(fast):
    sink = DurableEventSink()
    bad = {"entity_type": "agent_identity", "entity_id": "a1", "event_type": "wrong", "payload": {}}
    with pytest.raises(ValueError):
        sink.record_required(bad)
    h = sink.write_health()
    assert h["failed_writes"] == 0 and h["degraded"] is False and sink._retry_after == 0.0


def test_degraded_stays_until_a_write_succeeds(fast, blackhole):
    sink = DurableEventSink(blackhole)
    sink.record(dict(EV))
    sink._last_failure_mono -= 10_000                   # long past any time window
    assert sink.write_health()["degraded"] is True


def test_source_state_honours_the_retry_cooldown(fast, blackhole, monkeypatch):
    monkeypatch.setenv("HERDR_CP_SINK_RETRY_COOLDOWN_S", "30")
    sink = DurableEventSink(blackhole)
    sink.record(dict(EV))
    t0 = time.monotonic()
    sink.record_source_state({"source": "s", "last_sequence": 1, "last_seen_at": "2026-01-01T00:00:00+00:00", "gaps": []})
    assert time.monotonic() - t0 < 0.5
    h = sink.write_health()
    assert h["failed_writes"] == 1 and h["dropped_events"] == 2


def test_bridge_wait_bound_is_opt_in(monkeypatch):
    monkeypatch.setenv("HERDR_CP_BRIDGE_WAIT_TIMEOUT_S", "0.2")
    with pytest.raises(TimeoutError):
        _LoopRunner().run(asyncio.sleep(30))
