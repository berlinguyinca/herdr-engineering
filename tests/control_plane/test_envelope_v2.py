"""Event envelope v2 (docs/contracts/event-envelope-v2.md): server rules."""
import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import events
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.envelope import EnvelopeConflict
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink

PRINCIPAL = {"kind": "agent", "id": "key:abc", "authenticated_by": "api_key"}


def body(seq=0, event_id=None, **extra):
    return {"schema_version": 2, "event_id": event_id or f"ev-{seq}",
            "source": "pi", "source_sequence": seq,
            "entity_type": "task", "entity_id": "t1", "event_type": "task.note",
            "occurred_at": "2026-10-22T10:00:00Z", "payload": {"n": seq}, **extra}


def svc(repo=None):
    repo = repo or ControlPlaneRepo()
    return repo, IngestService(repo)


def stored(repo):
    return repo.stream("task", "t1")


# ------------------------------------------------------------ idempotency
def test_duplicate_is_acknowledged_not_reapplied():
    repo, s = svc()
    first = s.ingest(body(0), PRINCIPAL)
    again = s.ingest(body(0), PRINCIPAL)
    assert first["envelope"]["duplicate"] is False
    assert again["envelope"]["duplicate"] is True
    assert len(stored(repo)) == 1


def test_same_event_id_different_content_conflicts_and_keeps_original():
    repo, s = svc()
    s.ingest(body(0), PRINCIPAL)
    changed = body(0)
    changed["payload"] = {"n": 999}
    with pytest.raises(EnvelopeConflict):
        s.ingest(changed, PRINCIPAL)
    assert [e["payload"]["n"] for e in stored(repo)] == [0]


def test_source_sequence_collision_with_other_event_id():
    repo, s = svc()
    s.ingest(body(3, event_id="a"), PRINCIPAL)
    with pytest.raises(EnvelopeConflict):
        s.ingest(body(3, event_id="b"), PRINCIPAL)
    assert len(stored(repo)) == 1


# -------------------------------------------------------------- gaps
def test_gap_opens_on_jump_and_closes_on_out_of_order_arrival():
    repo, s = svc()
    s.ingest(body(0), PRINCIPAL)
    s.ingest(body(3), PRINCIPAL)
    state = repo.ledger.source_state("pi")
    assert state["last_sequence"] == 3
    assert [(g["from"], g["to"]) for g in state["gaps"]] == [(1, 2)]
    s.ingest(body(2), PRINCIPAL)  # late arrival inside the gap
    assert [(g["from"], g["to"]) for g in repo.ledger.source_state("pi")["gaps"]] == [(1, 1)]
    s.ingest(body(1), PRINCIPAL)
    state = repo.ledger.source_state("pi")
    assert state["gaps"] == [] and state["last_sequence"] == 3


def test_freshness_is_stale_without_events_and_healthy_after_one():
    clock = [1_000_000.0]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    s = IngestService(repo)
    assert repo.ledger.source_state("pi")["status"] == "stale"
    s.ingest(body(0), PRINCIPAL)
    assert repo.ledger.source_state("pi")["status"] == "healthy"
    clock[0] += repo.ledger.freshness_seconds + 1
    assert repo.ledger.source_state("pi")["status"] == "stale"


# ------------------------------------------------------------ normalisation
def test_client_supplied_actor_is_ignored_and_kept_as_claim_only():
    repo, s = svc()
    s.ingest(body(0, actor={"kind": "operator", "id": "root", "authenticated_by": "me"}), PRINCIPAL)
    ev = stored(repo)[0]
    assert ev["actor"] == PRINCIPAL
    assert ev["payload"]["_envelope"]["claimed_actor"]["id"] == "root"


def test_received_at_overwritten_and_occurred_at_stored_as_source_timestamp():
    repo, s = svc()
    s.ingest(body(0, received_at="1999-01-01T00:00:00Z"), PRINCIPAL)
    ev = stored(repo)[0]
    assert ev["source_timestamp"] == "2026-10-22T10:00:00+00:00"
    assert not ev["ingest_timestamp"].startswith("1999")


def test_visibility_defaults_and_unknown_fall_back_to_private():
    repo, s = svc()
    s.ingest(body(0), PRINCIPAL)
    s.ingest(body(1, visibility_scope="galaxy"), PRINCIPAL)
    s.ingest(body(2, visibility_scope="room"), PRINCIPAL)
    assert [e["visibility_scope"] for e in stored(repo)] == ["private", "private", "room"]


def test_schema_version_3_is_rejected():
    _, s = svc()
    with pytest.raises(ValueError, match="schema_version"):
        s.ingest({**body(0), "schema_version": 3}, PRINCIPAL)


@pytest.mark.parametrize("drop", ["event_id", "source", "source_sequence"])
def test_v2_requires_identity_fields(drop):
    _, s = svc()
    b = body(0)
    del b[drop]
    with pytest.raises(ValueError):
        s.ingest(b, PRINCIPAL)


def test_v1_body_is_unchanged():
    repo, s = svc()
    s.ingest({"entity_type": "task", "entity_id": "t1", "event_type": "task.note",
              "source_sequence": 7, "payload": {"x": 1}}, PRINCIPAL)
    ev = stored(repo)[0]
    assert ev["schema_version"] == 1 and "source" not in ev and "actor" not in ev
    assert repo.ledger.source_states() == []


def test_typed_entity_carries_envelope_on_its_event():
    repo, s = svc()
    s.ingest({"schema_version": 2, "event_id": "h1", "source": "pi", "source_sequence": 0,
              "entity_type": "host", "entity_id": "bender", "event_type": "host.registered",
              "payload": {"host_name": "bender"}}, PRINCIPAL)
    ev = repo.stream("host", "bender")[0]
    assert ev["event_id"] == "h1" and ev["actor"] == PRINCIPAL
    assert len(repo.list_hosts()) == 1


# ------------------------------------------------------------ durability
class V2Conn:
    """Fake asyncpg conn honouring the unique indexes and the v2 INSERT."""

    def __init__(self):
        self.rows, self.fail = [], None

    async def fetch(self, sql, *a):
        if "ORDER BY sequence" in sql:
            return [r for r in self.rows if (r["entity_type"], r["entity_id"]) == (a[0], a[1])]
        return []

    async def execute(self, sql, *a):
        return None

    async def fetchrow(self, sql, *a):
        if self.fail:
            raise self.fail
        cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
        row = dict(zip(cols, a))
        if any(r["event_id"] == row["event_id"] for r in self.rows):
            return None
        if row.get("source") is not None and any(
                (r.get("source"), r.get("source_sequence")) == (row["source"], row["source_sequence"])
                for r in self.rows):
            exc = Exception("duplicate key")
            exc.sqlstate = "23505"
            raise exc
        self.rows.append(row)
        return {"event_id": row["event_id"]}


def test_durable_replay_returns_v2_fields():
    conn = V2Conn()
    repo = ControlPlaneRepo(sink=DurableEventSink(conn=conn, run=asyncio.run))
    IngestService(repo).ingest(body(0, evidence_refs=[{"kind": "git", "uri": "u", "revision": "abc"}],
                                    task_id="T1", correlation_id="c1"), PRINCIPAL)
    back = asyncio.run(events.EventStore(conn).read_stream("task", "t1"))[0]
    assert back.event_id == "ev-0" and back.schema_version == 2
    assert (back.source, back.source_sequence, back.task_id, back.correlation_id) == ("pi", 0, "T1", "c1")
    assert back.actor == PRINCIPAL and back.visibility_scope == "private"
    assert back.evidence_refs[0]["revision"] == "abc"


def test_durable_failure_fails_ingest_and_leaves_no_ledger_trace():
    conn = V2Conn()
    conn.fail = ConnectionError("db down")
    repo = ControlPlaneRepo(sink=DurableEventSink(conn=conn, run=asyncio.run))
    s = IngestService(repo)
    with pytest.raises(ConnectionError):
        s.ingest(body(0), PRINCIPAL)
    assert repo.ledger.source_states() == []
    conn.fail = None
    assert s.ingest(body(0), PRINCIPAL)["envelope"]["duplicate"] is False  # retry applies


def test_restart_hydration_restores_ledger_from_replayed_events():
    conn = V2Conn()
    repo = ControlPlaneRepo(sink=DurableEventSink(conn=conn, run=asyncio.run))
    s = IngestService(repo)
    s.ingest(body(0), PRINCIPAL)
    s.ingest(body(2), PRINCIPAL)
    fresh = ControlPlaneRepo()
    fresh.import_state({"events": {"task|t1": stored(repo)}, "event_order": []})
    assert [(g["from"], g["to"]) for g in fresh.ledger.source_state("pi")["gaps"]] == [(1, 1)]
    again = IngestService(fresh).ingest(body(0), PRINCIPAL)
    assert again["envelope"]["duplicate"] is True
    with pytest.raises(EnvelopeConflict):
        IngestService(fresh).ingest(body(2, event_id="other"), PRINCIPAL)


# ------------------------------------------------------------ EventStore
class RaisingConn:
    def __init__(self, exc):
        self.exc = exc

    async def fetchrow(self, sql, *a):
        raise self.exc


def test_append_failure_is_not_reported_as_duplicate():
    ev = events.make_event("task", "t1", "x")
    with pytest.raises(ConnectionError):
        asyncio.run(events.EventStore(RaisingConn(ConnectionError("down"))).append(ev))


def test_append_unique_violation_on_sequence_raises_collision():
    exc = Exception("dup")
    exc.sqlstate = "23505"
    ev = events.Event(event_id="e", source="pi", source_sequence=1)
    with pytest.raises(events.SequenceCollision):
        asyncio.run(events.EventStore(RaisingConn(exc)).append(ev))


def test_sink_record_required_propagates_but_record_stays_best_effort():
    sink = DurableEventSink(conn=RaisingConn(ConnectionError("down")), run=asyncio.run)
    ev = {"event_id": "e1", "entity_type": "a", "entity_id": "b", "event_type": "c"}
    sink.record(ev)  # never raises
    with pytest.raises(ConnectionError):
        sink.record_required(ev)


# --------------------------------------------------------------- HTTP
@pytest.fixture()
def http():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys={"op-1": "operator", "ing-1": "ingest"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield f"http://127.0.0.1:{svr.port}", repo
    svr.shutdown()


def _post(base, payload, key="ing-1"):
    req = urllib.request.Request(
        base + "/api/v1/ingest", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_actor_comes_from_key_and_conflicts_are_409(http):
    base, repo = http
    status, out = _post(base, body(0, actor={"kind": "operator", "id": "root"}))
    assert status == 200 and out["entity"]["envelope"]["duplicate"] is False
    ev = repo.stream("task", "t1")[0]
    assert ev["actor"]["kind"] == "ingest" and ev["actor"]["id"].startswith("key:")
    assert "ing-1" not in json.dumps(ev)
    assert _post(base, body(0))[0] == 200
    assert _post(base, {**body(0), "payload": {"n": 5}})[0] == 409
    assert _post(base, body(0, event_id="other"))[0] == 409
    assert _post(base, {**body(1), "schema_version": 3})[0] == 400
    req = urllib.request.Request(base + "/api/v1/ingest/sources", headers={"X-API-Key": "ing-1"})
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read())["sources"][0]["source"] == "pi"
