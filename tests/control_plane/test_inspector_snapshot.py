"""Snapshot + delta must rebuild EXACTLY what a full replay builds, and anything doubtful must be refused."""
import json
import random
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import inspector as insp
from herdr_engineering.control_plane.inspector import InspectorStore, SnapshotStale

T0 = 1_800_000_000.0
LIMITS = {"max_events_per_session": 25, "max_bytes_per_session": 6000}


def store(**kw):
    return InspectorStore(limits=LIMITS, clock=lambda: T0 + 10_000, **kw)


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


def stream(n=900, seed=7):
    """Deterministic durable rows across several sessions, with reused call ids, tests, usage, lifecycle and prefs."""
    rnd = random.Random(seed)
    rows, rev = [], {}
    sessions = [f"s{i}" for i in range(6)]
    for i in range(n):
        sid, t = rnd.choice(sessions), T0 + i * 3
        rev[sid] = rev.get(sid, 0) + 1
        roll = rnd.random()
        if roll < 0.28:
            kind, body = "message", {"role": rnd.choice(["user", "assistant"]), "text": f"hello {i} token=abcdefgh{i}" * rnd.randint(1, 6)}
        elif roll < 0.55:
            cid = f"c{rnd.randint(0, 9)}"
            kind, body = "tool", {"call_id": cid, "tool": "bash", "phase": rnd.choice(["start", "update", "end"]),
                                  "args_preview": f"ls {i}", "output": "x" * rnd.randint(0, 300)}
        elif roll < 0.65:
            kind, body = "file", {"path": f"/src/f{i % 7}.py", "op": "edit", "diff": "+a\n-b\n" * rnd.randint(1, 5)}
        elif roll < 0.75:
            kind, body = "usage", {"input_tokens": rnd.randint(1, 900), "output_tokens": rnd.randint(1, 90)}
        elif roll < 0.82:
            rid = f"r{rnd.randint(0, 4)}"
            kind, body = "test", {"run_id": rid, "command": "pytest", "phase": rnd.choice(["start", "end"]), "passed": i % 9, "failed": i % 2}
        elif roll < 0.87:
            kind, body = "association", {"mission_id": f"m{i % 3}"}
        elif roll < 0.91:
            kind, body = "model", {"model": f"model-{i % 4}"}
        elif roll < 0.96:
            kind, body = "lifecycle", {"phase": rnd.choice(["started", "restarted", "interrupted", "ended"]), "dropped": rnd.randint(0, 3)}
        else:
            kind, body = "capabilities", {"reasoning": "summary", "usage": True}
        if rnd.random() < 0.3:
            body["event_id"] = f"e{rnd.randint(0, 120)}"          # some duplicates
        rows.append({"event_type": f"inspector.{kind}", "entity_id": sid, "sequence": i + 1,
                     "source_timestamp": iso(t), "ingest_timestamp": iso(t + 0.5),
                     "payload": {**body, "kind": kind, "_received_at": t, "_sequence": rev[sid]}})
    rows.append({"event_type": "inspector.preferences", "entity_id": "s1", "sequence": n + 1, "source_timestamp": iso(T0 + n * 3),
                 "ingest_timestamp": iso(T0 + n * 3), "payload": {"prefs": {"max_events_per_session": 12}, "scope": "session", "_received_at": T0 + n * 3}})
    return rows


def canon(state):
    """last_ingest is the wall-clock moment a replay ran, so it legitimately differs between two replays."""
    for sess in state["sessions"]:
        sess["last_ingest"] = None
    return json.dumps(state, sort_keys=True)


def roundtrip(s):
    return json.loads(json.dumps(s.export_state()))


@pytest.mark.parametrize("cut", [0, 1, 137, 450, 899, 901])
def test_snapshot_plus_delta_equals_full_replay(cut):
    rows = stream()
    full = store()
    full.hydrate(rows)
    head = store()
    head.hydrate(rows[:cut])
    resumed = store()
    resumed.load_state(roundtrip(head))
    resumed.hydrate_delta(rows[cut:])
    assert canon(resumed.export_state()) == canon(full.export_state())
    assert resumed.replay_watermark == full.replay_watermark and resumed.replay_count == full.replay_count


def test_a_snapshot_chain_equals_full_replay():
    rows = stream(600, seed=3)
    full = store()
    full.hydrate(rows)
    cur = store()
    for lo in range(0, len(rows), 97):
        nxt = store()
        nxt.load_state(roundtrip(cur))
        nxt.hydrate_delta(rows[lo:lo + 97])
        cur = nxt
    assert canon(cur.export_state()) == canon(full.export_state())


def test_the_loaded_store_serves_the_same_reads():
    rows = stream(400, seed=5)
    full, head, resumed = store(), store(), store()
    full.hydrate(rows)
    head.hydrate(rows[:200])
    resumed.load_state(roundtrip(head))
    resumed.hydrate_delta(rows[200:])
    assert full.usage_events() == resumed.usage_events()


def test_reasoning_is_never_in_a_snapshot():
    s = store()
    s.hydrate([{"event_type": "inspector.reasoning", "entity_id": "s0", "sequence": 1, "source_timestamp": iso(T0),
                "payload": {"kind": "reasoning", "text": "private chain of thought", "_received_at": T0}}])
    assert "private chain of thought" not in json.dumps(s.export_state())


def test_refuses_another_version_or_other_limits_or_other_redaction_rules(monkeypatch):
    head = store()
    head.hydrate(stream(50))
    blob = roundtrip(head)
    with pytest.raises(SnapshotStale):
        store().load_state({**blob, "version": 999})
    with pytest.raises(SnapshotStale):
        InspectorStore(limits={"max_events_per_session": 26}, clock=lambda: T0 + 10_000).load_state(blob)
    monkeypatch.setattr(insp, "rules_fingerprint", lambda: "a different rule set")
    with pytest.raises(SnapshotStale):
        store().load_state(blob)


def test_refuses_a_delta_that_orders_before_the_snapshot():
    rows = stream(80)
    head = store()
    head.hydrate(rows)
    resumed = store()
    resumed.load_state(roundtrip(head))
    late = {**rows[3], "sequence": 5000}
    with pytest.raises(SnapshotStale):
        resumed.hydrate_delta([late])


def test_a_malformed_snapshot_leaves_the_store_untouched():
    head = store()
    head.hydrate(stream(120))
    blob = roundtrip(head)
    victim = next(s for s in blob["sessions"] if s["tools"])
    victim["tools"]["ghost"] = 10_000_000
    target = store()
    target.hydrate(stream(10, seed=1))
    before = canon(target.export_state())
    with pytest.raises(SnapshotStale):
        target.load_state(blob)
    assert canon(target.export_state()) == before
    with pytest.raises(SnapshotStale):
        target.load_state({"version": insp.SNAPSHOT_VERSION})


# ---- persistence: the writer and the boot path -------------------------------------------------------------------
import asyncio  # noqa: E402

from herdr_engineering.control_plane import inspector_snapshot as snap  # noqa: E402
from herdr_engineering.control_plane.bridge import _rebuild_events  # noqa: E402


def db_row(ev):
    return {"event_id": f"id-{ev['sequence']}", "schema_version": 1, "source_timestamp": datetime.fromisoformat(ev["source_timestamp"]),
            "ingest_timestamp": datetime.fromisoformat(ev["ingest_timestamp"]), "entity_type": "inspector",
            "entity_id": ev["entity_id"], "mission_id": None, "session_id": ev["entity_id"], "sequence": ev["sequence"],
            "event_type": ev["event_type"], "payload": json.dumps(ev["payload"])}


class FakeConn:
    """Just enough of asyncpg for SnapshotWriter, backed by a list of fabric_events rows."""

    def __init__(self, rows):
        self.rows, self.saved = list(rows), None

    async def fetch(self, sql, last, settle, limit):
        return sorted((r for r in self.rows if r["sequence"] > last), key=lambda r: r["sequence"])[:limit]

    async def fetchval(self, sql, *args):
        if "inspector_snapshots" in sql:
            return 1 if self.saved else None
        return sum(1 for r in self.rows if r["sequence"] <= args[0])

    async def fetchrow(self, sql):
        return dict(self.saved) if self.saved else None

    async def execute(self, sql, version, fingerprint, watermark, count, body):
        self.saved = {"schema_version": version, "fingerprint": fingerprint, "watermark": watermark,
                      "event_count": count, "body": body}


def boot_events(rows):
    streams, _ = _rebuild_events([dict(r) for r in rows])
    return [e for s in streams.values() for e in s]


def full_state(rows):
    s = store()
    s.hydrate(boot_events(rows))
    return canon(s.export_state())


def booted(conn, rows):
    s = store()
    outcome = snap.apply_to_store(s, conn.saved, boot_events(rows))
    return outcome, canon(s.export_state())


def run(coro):
    return asyncio.run(coro)


def test_writer_snapshot_then_boot_equals_full_replay_and_advances():
    rows = [db_row(e) for e in stream(500, seed=11)]
    conn = FakeConn(rows[:400])
    writer = snap.SnapshotWriter(store, interval=0)
    assert run(writer.tick(conn)).startswith("written")
    conn.rows = rows                      # 100 more events arrive after the snapshot
    outcome, state = booted(conn, rows)
    assert outcome == "snapshot" and state == full_state(rows)
    assert run(writer.tick(conn)).startswith("written")
    outcome, state = booted(conn, rows)
    assert outcome == "snapshot" and state == full_state(rows)
    assert run(writer.tick(conn)) == "unchanged"


def test_a_new_writer_resumes_from_the_stored_snapshot():
    rows = [db_row(e) for e in stream(300, seed=2)]
    conn = FakeConn(rows[:200])
    run(snap.SnapshotWriter(store, interval=0).tick(conn))
    conn.rows = rows
    run(snap.SnapshotWriter(store, interval=0).tick(conn))
    assert booted(conn, rows) == ("snapshot", full_state(rows))


def test_boot_replays_everything_when_the_log_below_the_watermark_changed():
    rows = [db_row(e) for e in stream(300, seed=4)]
    conn = FakeConn(rows)
    run(snap.SnapshotWriter(store, interval=0).tick(conn))
    late = {**rows[10], "sequence": rows[10]["sequence"] + 0, "event_id": "late"}
    late["sequence"] = rows[-1]["sequence"] - 1                      # lands UNDER the watermark after the snapshot
    late["payload"] = json.dumps({"kind": "message", "role": "user", "text": "late", "_received_at": T0 + 5, "_sequence": 99})
    all_rows = rows + [late]
    outcome, state = booted(conn, all_rows)
    assert outcome.startswith("full:") and state == full_state(all_rows)


def test_boot_replays_everything_when_the_snapshot_is_unusable(monkeypatch):
    rows = [db_row(e) for e in stream(200, seed=6)]
    conn = FakeConn(rows)
    run(snap.SnapshotWriter(store, interval=0).tick(conn))
    good = dict(conn.saved)
    for broken in ({**good, "body": b"not gzip"}, {**good, "watermark": good["watermark"] - 1},
                   {**good, "body": snap.encode({"version": 1})}):
        conn.saved = broken
        outcome, state = booted(conn, rows)
        assert outcome.startswith("full:") and state == full_state(rows)
    conn.saved = good
    monkeypatch.setattr(insp, "rules_fingerprint", lambda: "new rules")
    outcome, state = booted(conn, rows)
    assert outcome.startswith("full:") and state == full_state(rows)     # a changed redaction rule re-sanitises everything


def test_no_snapshot_is_a_plain_full_replay():
    rows = [db_row(e) for e in stream(120, seed=8)]
    assert booted(FakeConn(rows), rows) == ("full:no snapshot", full_state(rows))


def test_writer_rebuilds_when_it_finds_it_missed_an_event():
    rows = [db_row(e) for e in stream(250, seed=9)]
    conn = FakeConn(rows[:200])
    writer = snap.SnapshotWriter(store, interval=0)
    run(writer.tick(conn))
    missed = {**rows[220], "sequence": conn.rows[-1]["sequence"] - 1, "event_id": "missed"}   # committed under the watermark
    conn.rows = rows[:200] + [missed]
    assert run(writer.tick(conn)).startswith("written")
    assert booted(conn, conn.rows) == ("snapshot", full_state(conn.rows))


def test_writer_never_raises_into_the_worker():
    class Broken(FakeConn):
        async def fetchrow(self, sql):
            raise RuntimeError("table does not exist")
    assert run(snap.SnapshotWriter(store, interval=0).tick(Broken([]))).startswith("failed")


def test_writer_waits_between_runs():
    conn = FakeConn([db_row(e) for e in stream(40, seed=1)])
    writer = snap.SnapshotWriter(store, interval=600, clock=lambda: 100.0)
    assert run(writer.tick(conn)).startswith("written")
    assert run(writer.tick(conn)) == "waiting"


def test_repo_boot_uses_the_snapshot_and_matches_a_full_boot():
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    rows = [db_row(e) for e in stream(300, seed=12)]
    conn = FakeConn(rows[:250])
    run(snap.SnapshotWriter(store, interval=0).tick(conn))

    def boot(snapshot):
        repo = ControlPlaneRepo(inspector_limits={"max_events_per_session": 25, "max_bytes_per_session": 6000})
        repo.inspector._clock = lambda: T0 + 10_000          # the synthetic timestamps lie after the real clock
        streams, order = _rebuild_events([dict(r) for r in rows])
        repo.import_state({"events": streams, "event_order": order, "inspector_snapshot": snapshot})
        return canon(repo.inspector.export_state())

    # the repo's store has the same limits as store(), so the fingerprints agree
    assert boot(conn.saved) == boot(None)


def test_snapshot_settings_are_off_unless_explicitly_enabled():
    from herdr_engineering.cli import _inspector_snapshot_settings as settings
    assert settings({}) == (False, 600.0)
    assert settings({"inspector_snapshots": {"enabled": "yes"}}) == (False, 600.0)
    assert settings({"inspector_snapshots": {"enabled": True, "interval_seconds": 120}}) == (True, 120.0)
    assert settings({"inspector_snapshots": {"enabled": True, "interval_seconds": 5}}) == (True, 600.0)
    with pytest.raises(SystemExit):
        settings({"inspector_snapshots": "on"})
