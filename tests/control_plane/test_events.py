import asyncio
from datetime import datetime

from herdr_engineering.control_plane import events


class _FakeEvents:
    def __init__(self):
        self.rows = []

    async def fetch(self, sql, *a):
        if "max(sequence)" in sql:
            et, eid = a[0], a[1]
            seqs = [
                r["sequence"]
                for r in self.rows
                if r["entity_type"] == et and r["entity_id"] == eid
            ]
            return [{"max": max(seqs, default=-1)}]
        if "ORDER BY sequence" in sql:
            et, eid = a[0], a[1]
            after = a[2] if len(a) > 2 else -1
            rows = [
                r for r in self.rows
                if r["entity_type"] == et and r["entity_id"] == eid
                and r["sequence"] > after
            ]
            return sorted(rows, key=lambda r: r["sequence"])
        return []

    async def execute(self, sql, *a):
        return 1

    async def fetchrow(self, sql, *a):
        if sql.strip().startswith("INSERT") and "RETURNING" in sql:
            row = {
                "event_id": a[0], "sequence": a[1], "event_type": a[2],
                "entity_type": a[3], "entity_id": a[4], "mission_id": a[5],
                "session_id": a[6], "payload": a[7],
                "source_timestamp": a[8], "ingest_timestamp": a[9],
                "schema_version": a[10],
            }
            if any(r["event_id"] == row["event_id"] for r in self.rows):
                return None
            self.rows.append(row)
            return {"event_id": row["event_id"]}
        return None


def _run(coro):
    return asyncio.run(coro)


def test_make_event_envelope():
    e = events.make_event("mission", "m1", "MissionCreated",
                          payload={"title": "x"}, mission_id="m1")
    assert e.event_id and e.schema_version == 1
    assert e.entity_type == "mission" and e.entity_id == "m1"
    assert isinstance(e.source_timestamp, datetime) and e.source_timestamp.tzinfo is not None


def test_append_is_idempotent_and_sequences():
    store = events.EventStore(_FakeEvents())
    e1 = events.make_event("mission", "m1", "MissionCreated", mission_id="m1")
    seq1 = _run(store.next_sequence("mission", "m1"))
    e1 = events.Event(**{**e1.__dict__, "sequence": seq1})
    assert _run(store.append(e1)) is True
    assert _run(store.append(e1)) is False  # duplicate event_id ignored


def test_read_stream_ordered_and_resumable():
    store = events.EventStore(_FakeEvents())
    for i in range(3):
        ev = events.make_event("session", "s1", f"Event{i}", session_id="s1")
        seq = _run(store.next_sequence("session", "s1"))
        ev = events.Event(**{**ev.__dict__, "sequence": seq})
        _run(store.append(ev))
    full = _run(store.read_stream("session", "s1"))
    assert [e.sequence for e in full] == [0, 1, 2]
    tail = _run(store.read_stream("session", "s1", after=1))
    assert [e.sequence for e in tail] == [2]
