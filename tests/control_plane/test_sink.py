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
    return asyncio.run(coro)


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


def test_required_command_receipt_propagates_database_failure_before_delivery():
    class FailingConn(FakeConn):
        async def fetchrow(self, sql, *args):
            raise OSError("database unavailable")
    sink = DurableEventSink(conn=FailingConn(), run=fake_run)
    repo = ControlPlaneRepo(sink=sink)
    sid = repo.create_session()
    calls = []
    def deliver(*args):
        calls.append(args)
        return {"status": "delivered"}
    receipt = repo.send_message(sid, "hello", dispatcher=deliver)
    assert receipt["error"] == "receipt_persistence_failed"
    assert calls == []
    # Inspect the receipt's counter without attempting a separate durable name
    # allocation against the deliberately unavailable database.
    assert repo.export_state()["sessions"][sid]["messages"] == 0


def test_sink_reconnects_a_closed_database_connection(monkeypatch):
    import sys
    from types import SimpleNamespace

    class ClosedConn(FakeConn):
        def is_closed(self):
            return True

        async def fetchrow(self, sql, *args):
            raise OSError("connection is closed")

    old = ClosedConn()
    replacement = FakeConn()
    connects = []

    async def connect(dsn):
        connects.append(dsn)
        return replacement

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    sink = DurableEventSink(dsn="postgresql://fixture", conn=old, run=fake_run)
    event = {"event_id": "recovered-event", "entity_type": "mission",
             "entity_id": "m1", "event_type": "MissionUpdated", "payload": {}}
    sink.record_required(event)
    sink.record_required({**event, "event_id": "next-event", "sequence": 1})
    assert connects == ["postgresql://fixture"]
    assert old.inserts == []
    assert [row[0] for row in replacement.inserts] == ["recovered-event", "next-event"]


def test_sink_serializes_connection_recovery_and_concurrent_writes(monkeypatch):
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    replacement = FakeConn()
    connects = []

    async def connect(dsn):
        connects.append(dsn)
        await asyncio.sleep(0.02)
        return replacement

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    sink = DurableEventSink(dsn="postgresql://fixture", run=fake_run)

    def write(index):
        sink.record_required({"event_id": f"event-{index}", "entity_type": "mission",
                              "entity_id": "m1", "event_type": "Updated", "sequence": index})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))
    assert connects == ["postgresql://fixture"]
    assert len(replacement.inserts) == 8
