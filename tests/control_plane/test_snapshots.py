import asyncio

from herdr_engineering.control_plane import events, snapshots


class _SnapConn:
    def __init__(self):
        self.snaps = []

    async def execute(self, sql, *a):
        if "INSERT INTO state_snapshots" in sql:
            self.snaps.append({
                "entity_kind": a[0], "entity_id": a[1],
                "sequence": a[2], "state": a[3],
            })
        return 1

    async def fetchrow(self, sql, *a):
        cands = [
            s for s in self.snaps
            if s["entity_kind"] == a[0] and s["entity_id"] == a[1]
        ]
        if not cands:
            return None
        best = max(cands, key=lambda s: s["sequence"])
        return {"state": best["state"], "sequence": best["sequence"]}


class _FakeEvents:
    async def read_stream(self, entity_type, entity_id, after=0):
        return [
            events.Event(event_id="a", entity_type=entity_type, entity_id=entity_id,
                         sequence=11, event_type="MissionProgressUpdated",
                         payload={"progress": 0.6}),
            events.Event(event_id="b", entity_type=entity_type, entity_id=entity_id,
                         sequence=12, event_type="MissionStageChanged",
                         payload={"stage": "review"}),
            events.Event(event_id="c", entity_type=entity_type, entity_id=entity_id,
                         sequence=13, event_type="MissionProgressUpdated",
                         payload={"progress": 0.7}),
        ]


def _run(coro):
    return asyncio.run(coro)


def test_snapshot_save_load_latest():
    conn = _SnapConn()
    ss = snapshots.SnapshotStore(conn)
    _run(ss.save("mission", "m1", {"progress": 0.5}, 10))
    _run(ss.save("mission", "m1", {"progress": 0.9}, 20))
    state, seq = _run(ss.load_latest("mission", "m1"))
    assert state == {"progress": 0.9} and seq == 20


def test_recover_folds_later_events():
    conn = _SnapConn()
    ss = snapshots.SnapshotStore(conn)
    _run(ss.save("mission", "m1",
                 {"progress": 0.5, "stage": "implementation"}, 10))
    state = _run(snapshots.recover("mission", "m1", ss, _FakeEvents()))
    assert state["progress"] == 0.7 and state["stage"] == "review"
