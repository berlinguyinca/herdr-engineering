"""``control_plane.journal_suppression``: no-op writes stay out of the journal and a replay shows the same state.

Each workload runs twice through a real ``DurableEventSink`` into real PostgreSQL, suppression on and off; both journals are
replayed and compared, and the suppressed one must be smaller. No mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from herdr_engineering.control_plane.journal_suppression import OFF, JournalSuppression
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.control_plane import test_recovery_checkpoint as checkpoint_tests
from tests.control_plane.test_fabric_compaction import asyncpg
from tests.control_plane.test_postgres_recovery import _projections, _reference

pg, db = checkpoint_tests.pg, checkpoint_tests.db      # the disposable-database fixtures

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg required")

T0 = 1793059200
SID = "s1"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def _run(db, policy, workload):
    """Run ``workload(repo, clock)`` against a fresh journal under ``policy``; return the replayed repo and the row counts."""
    db.query("TRUNCATE fabric_events")
    clock = Clock()
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    try:
        repo = ControlPlaneRepo(now=clock, sink=DurableEventSink(conn=conn, run=loop.run_until_complete),
                                journal_suppression=policy)
        repo.inspector._clock = clock
        workload(repo, clock)
        live = {"session": repo.get_session(SID), "stream": len(repo.stream("session", SID))}
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    replayed, _ = _reference(db)
    rows = {r["event_type"]: r["n"] for r in db.query(
        "SELECT event_type, count(*) AS n FROM fabric_events GROUP BY event_type")}
    return replayed, rows, live


def _session_workload(repo, clock):
    repo.upsert_mission("m", title="Mission")
    repo.upsert_session(SID, host_id="h", mission_id="m", model="model", cwd="/repo", status="active")
    for n in range(60):                              # one report every 10 s: runtime counters and the transcript flip
        clock.t = T0 + 10 * (n + 1)
        repo.upsert_session(SID, runtime={"cpu_s": n, "rss_mib": 100 + n},
                            transcript={"status": "recording" if n % 2 else "identity_unavailable"})
    clock.t += 10
    repo.upsert_session(SID, summary="finished", branch="topic")      # a material change is always journaled
    clock.t += 10
    repo.upsert_session(SID, runtime={"cpu_s": 99, "rss_mib": 1})


def test_session_updates_replay_the_same_and_the_journal_shrinks(db):
    on, rows_on, live_on = _run(db, JournalSuppression(), _session_workload)
    off, rows_off, live_off = _run(db, OFF, _session_workload)
    assert rows_off["SessionUpdated"] == 62
    assert rows_on["SessionUpdated"] < rows_off["SessionUpdated"] / 3, (rows_on, rows_off)
    assert live_on == live_off                       # the live in-memory state and stream are untouched
    on_s, off_s = _projections(on), _projections(off)
    for proj in (on_s, off_s):                       # the volatile keys and updated_at may lag by under the heartbeat
        for s in proj["sessions"].values() if isinstance(proj["sessions"], dict) else proj["sessions"]:
            for k in ("runtime", "transcript", "updated_at"):
                s.pop(k, None)
    assert on_s == off_s
    assert live_on["session"]["summary"] == "finished" and on.get_session(SID)["summary"] == "finished"


def test_replayed_session_timestamp_is_never_older_than_the_heartbeat(db):
    seconds = 120.0

    def workload(repo, clock):
        repo.upsert_session(SID, host_id="h", model="model", status="active")
        for n in range(1, 100):
            clock.t = T0 + 10 * n
            repo.upsert_session(SID, runtime={"cpu_s": n})
    on, _, _ = _run(db, JournalSuppression(session_heartbeat_seconds=seconds), workload)
    off, _, _ = _run(db, OFF, workload)
    lag = _stamp(off.get_session(SID)["updated_at"]) - _stamp(on.get_session(SID)["updated_at"])
    assert 0 <= lag < seconds + 10


def _stamp(value):
    from datetime import datetime
    return value if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()


def test_configuration_defaults_and_validation():
    assert JournalSuppression.from_config(None) == JournalSuppression()
    assert JournalSuppression.from_config({}) == JournalSuppression()
    d = JournalSuppression()
    assert d.session_noop and d.inspector_tool_updates and d.inspector_zero_usage and d.session_heartbeat_seconds == 300
    off = JournalSuppression.from_config({"session_noop": False, "inspector_tool_updates": False,
                                          "inspector_zero_usage": False, "session_heartbeat_seconds": 60})
    assert not (off.session_noop or off.inspector_tool_updates or off.inspector_zero_usage)
    assert off.session_heartbeat_seconds == 60.0
    for bad in ({"nope": 1}, {"session_noop": "yes"}, {"session_heartbeat_seconds": 0},
                {"session_heartbeat_seconds": 601}, {"session_heartbeat_seconds": True}, [], "on"):
        with pytest.raises(ValueError):
            JournalSuppression.from_config(bad)


def test_no_sink_means_nothing_is_suppressed():
    repo = ControlPlaneRepo(now=Clock())
    repo.upsert_session(SID, host_id="h", model="model", status="active")
    for n in range(5):
        repo.upsert_session(SID, runtime={"cpu_s": n})
    assert len(repo.stream("session", SID)) == 6
