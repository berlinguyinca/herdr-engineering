"""Durable write-through sink (Phase 1a): repo events persist to Postgres."""
import asyncio

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink


class FakeConn:
    """Fake asyncpg conn recording EventStore appends (INSERT ... RETURNING)."""

    def __init__(self):
        self.inserts = []       # (event_id, sequence, event_type, entity_type, ...)
        self.seqs = {}          # stream_key -> max sequence

    async def fetch(self, sql, *args):
        if "COALESCE(max" in sql:
            etype, eid = args
            return [{"max": self.seqs.get(f"{etype}:{eid}", -1)}]
        return []

    async def fetchrow(self, sql, *args):
        self.inserts.append(args)
        key = f"{args[3]}:{args[4]}"
        self.seqs[key] = self.seqs.get(key, -1) + 1
        return [args[0]]


def fake_run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_sink_persists_repo_events():
    conn = FakeConn()
    sink = DurableEventSink(conn=conn, run=fake_run)
    repo = ControlPlaneRepo(sink=sink)
    repo.create_mission("hello")
    assert len(conn.inserts) == 1
    ev = conn.inserts[0]
    # INSERT column order: event_id, sequence, event_type, entity_type, entity_id, ...
    assert ev[2] == "MissionCreated"
    assert ev[3] == "mission"
    assert ev[4] == repo.list_missions()[0]["mission_id"]
    assert ev[5] is None or ev[5] == repo.list_missions()[0]["mission_id"]  # mission_id


def test_sink_is_idempotent_across_restart_like_sequences():
    # Two events on the same stream get 0-indexed sequences 0, 1 (matching the
    # durable max+1 rule), so a later re-hydration replays them in order.
    conn = FakeConn()
    sink = DurableEventSink(conn=conn, run=fake_run)
    repo = ControlPlaneRepo(sink=sink)
    repo.create_mission("one")
    repo.create_mission("two")
    seqs = [r[1] for r in conn.inserts]
    assert seqs == [0, 0]  # separate streams (different mission ids)
    # one stream, two events -> sequences 0,1
    conn2 = FakeConn()
    repo2 = ControlPlaneRepo(sink=DurableEventSink(conn=conn2, run=fake_run))
    sid = repo2.create_session(agent_role="pi")
    repo2.send_message(sid, "hi")
    repo2.record_tool_call(sid, "bash")
    assert [r[1] for r in conn2.inserts] == [0, 1, 2]


def test_sink_failure_never_breaks_repo():
    class BoomSink:
        def record(self, ev):
            raise RuntimeError("durable store is down")

    repo = ControlPlaneRepo(sink=BoomSink())
    mid = repo.create_mission("survives")
    assert repo.get_mission(mid) is not None
    assert repo.recent_events()[0]["event_type"] == "MissionCreated"


def test_repo_without_sink_still_records():
    repo = ControlPlaneRepo()
    repo.create_mission("x")
    assert repo.recent_events()[0]["event_type"] == "MissionCreated"


def test_sink_record_never_raises_on_store_error():
    class BrokenStore:
        def __init__(self):
            self.record_called = False

    sink = DurableEventSink(conn=None, run=None, events_table="fabric_events")
    # no dsn, no conn -> _ensure_store raises -> record() must swallow it
    sink.record({"event_type": "X", "entity_type": "h", "entity_id": "1"})
