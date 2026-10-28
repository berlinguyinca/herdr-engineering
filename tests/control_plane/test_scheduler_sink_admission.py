"""Scheduler transaction contracts through the real sink and EventStore."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.control_plane.events import V2_COLUMNS
from herdr_engineering.control_plane.sink import DurableEventSink, EventAlreadyStored


class RecordedConnection:
    def __init__(self):
        self.calls = []
        self.inserts = []
        self.rows = []
        self.fail_on = None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if sql == self.fail_on:
            raise OSError("injected database failure")

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "COALESCE(max(sequence)" in sql:
            maximum = max((row[1] for row in self.inserts if row[3:5] == args), default=-1)
            # Give concurrent writers an opportunity to race if the sink lock is removed.
            await asyncio.sleep(0.001)
            return [{"max": maximum}]
        if sql.startswith("SELECT source, source_sequence"):
            for row in self.inserts:
                if row[0] == args[0] and json.loads(row[15])["id"] == args[1]:
                    return [{"source": row[11], "source_sequence": row[12]}]
            return []
        if "SELECT DISTINCT ON (entity_id)" in sql:
            if self.fail_on == "read":
                raise OSError("injected database failure")
            return self.rows
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        assert sql.startswith("INSERT INTO scheduler_test_events ")
        if any(row[0] == args[0] for row in self.inserts):
            return None
        self.inserts.append(args)
        return {"event_id": args[0]}


@pytest.fixture
def sink_and_connection():
    connection = RecordedConnection()
    return DurableEventSink(
        conn=connection, run=asyncio.run, events_table="scheduler_test_events",
    ), connection


def test_admission_locks_before_read_and_commits_reentrant_write(sink_and_connection):
    sink, connection = sink_and_connection
    connection.rows = [{"payload": '{"request_id":"one"}'}, {"payload": {"request_id": "two"}}]
    with sink.scheduler_admission() as rows:
        assert rows == [{"request_id": "one"}, {"request_id": "two"}]
        assert [sql for sql, _ in connection.calls[:2]] == [
            "BEGIN", "SELECT pg_advisory_xact_lock(724965310221)",
        ]
        query = connection.calls[2][0]
        assert "FROM scheduler_test_events " in query
        assert "WHERE entity_type='scheduler_reservation' " in query
        assert query.endswith("ORDER BY entity_id, sequence DESC")
        sink.record_required({"event_id": "new", "entity_type": "scheduler_reservation",
                              "entity_id": "reservation", "payload": {"state": "reserved"}})
        assert connection.inserts[0][1] == 0
        assert all(sql != "COMMIT" for sql, _ in connection.calls)
    assert connection.calls[-1] == ("COMMIT", ())


@pytest.mark.parametrize("failure", ["body", "interrupt", "read", "decode", "COMMIT"])
def test_admission_rolls_back_and_preserves_failure(sink_and_connection, failure):
    sink, connection = sink_and_connection
    connection.fail_on = failure
    if failure == "decode":
        connection.rows = [{"payload": "not JSON"}]
    error = {"body": RuntimeError, "interrupt": KeyboardInterrupt,
             "decode": json.JSONDecodeError}.get(failure, OSError)
    with pytest.raises(error):
        with sink.scheduler_admission():
            if failure in {"body", "interrupt"}:
                raise error("injected caller failure")
    assert connection.calls[-1] == ("ROLLBACK", ())
    # A failed transaction must release the local lock and allow a fresh admission.
    connection.fail_on = None
    connection.rows = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def admit():
            with sink.scheduler_admission() as rows:
                return rows
        assert pool.submit(admit).result(timeout=5) == []
    assert connection.calls[-1] == ("COMMIT", ())


@pytest.mark.parametrize("method,where", [
    ("scheduler_policies", "WHERE entity_type='host' AND event_type='HostPolicyChanged'"),
    ("scheduler_observations", "WHERE entity_type='scheduler_observation'"),
])
def test_scheduler_readers_decode_latest_per_host_rows(sink_and_connection, method, where):
    sink, connection = sink_and_connection
    with sink.scheduler_admission():
        connection.rows = [
            {"entity_id": "host-a", "payload": '{"revision":2}'},
            {"entity_id": "host-b", "payload": {"revision": 3}},
        ]
        assert getattr(sink, method)() == {"host-a": {"revision": 2}, "host-b": {"revision": 3}}
        query = connection.calls[-1][0]
        assert "SELECT DISTINCT ON (entity_id) entity_id, payload FROM scheduler_test_events " in query
        assert where in query
        assert query.endswith("ORDER BY entity_id, sequence DESC")


def test_lease_reader_decodes_payloads_by_id_in_existing_transaction(sink_and_connection):
    sink, connection = sink_and_connection
    with sink.scheduler_admission():
        connection.rows = [
            {"entity_id": "lease-a", "payload": '{"lease_id":"lease-a","state":"released"}'},
            {"entity_id": "lease-b", "payload": {"lease_id": "lease-b", "state": "running"}},
        ]
        assert sink.scheduler_leases() == {
            "lease-a": {"lease_id": "lease-a", "state": "released"},
            "lease-b": {"lease_id": "lease-b", "state": "running"},
        }
        assert connection.calls[-1][0] == (
            "SELECT DISTINCT ON (entity_id) entity_id, payload FROM scheduler_test_events "
            "WHERE entity_type='scheduler_lease' ORDER BY entity_id, sequence DESC"
        )
        assert [sql for sql, _ in connection.calls].count("BEGIN") == 1
        assert all(sql != "COMMIT" for sql, _ in connection.calls)


@pytest.mark.parametrize("entity_type,event_type", [
    ("scheduler_reservation", "SchedulerReservationChanged"),
    ("scheduler_lease", "SchedulerLeaseChanged"),
    ("scheduler_observation", "SchedulerHostObserved"),
    ("host", "HostPolicyChanged"),
])
def test_scheduler_sequences_are_allocated_from_durable_stream(sink_and_connection, entity_type, event_type):
    sink, connection = sink_and_connection

    def write(index):
        sink.record_required({"event_id": f"event-{index}", "sequence": 99,
                              "entity_type": entity_type, "entity_id": "shared",
                              "event_type": event_type})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))
    assert [row[1] for row in connection.inserts] == list(range(8))
    # A new sink has no sequence cache; it must continue the durable stream.
    restarted = DurableEventSink(conn=connection, run=asyncio.run, events_table="scheduler_test_events")
    restarted.record_required({"event_id": "after-restart", "sequence": 0,
                               "entity_type": entity_type, "entity_id": "shared", "event_type": event_type})
    assert connection.inserts[-1][1] == 8


def test_scheduler_resequencing_preserves_v2_envelope_and_duplicate_identity(sink_and_connection):
    sink, connection = sink_and_connection
    envelope = {
        "source": "host-source", "source_sequence": 47, "causation_id": "cause",
        "correlation_id": "correlation", "actor": {"id": "operator"},
        "visibility_scope": "private", "evidence_refs": [{"event_id": "evidence"}],
        "org_id": "org", "project_id": "project", "room_id": "room",
        "workspace_id": "workspace", "task_id": "task", "agent_id": "agent",
    }
    event = {"event_id": "v2-event", "entity_type": "scheduler_observation", "entity_id": "host",
             "event_type": "SchedulerHostObserved", "sequence": 99, "schema_version": 2,
             "payload": {"sample": {"sequence": 47}}, **envelope}
    sink.record_required(event)
    stored = connection.inserts[0]
    assert stored[1] == 0
    assert stored[10] == 2
    assert json.loads(stored[7]) == event["payload"]
    saved_envelope = dict(zip(V2_COLUMNS, stored[11:], strict=True))
    for key in ("actor", "evidence_refs"):
        saved_envelope[key] = json.loads(saved_envelope[key])
    assert saved_envelope == envelope
    assert event["sequence"] == 99
    assert sink.stored_identity("v2-event", "operator") == ("host-source", 47)
    with pytest.raises(EventAlreadyStored):
        sink.record_required(event)
    assert len(connection.inserts) == 1


def test_non_scheduler_sequences_and_source_state_are_unchanged(sink_and_connection):
    sink, connection = sink_and_connection
    sink.record_required({"event_id": "ordinary", "entity_type": "session", "entity_id": "session",
                          "event_type": "SessionUpdated", "sequence": 23})
    assert connection.inserts[0][1] == 23
    assert len(connection.calls) == 1
    sink.record_source_state({"actor": "operator", "source": "source", "last_sequence": 47,
                              "last_seen_at": "2026-10-28T12:00:00+00:00", "gaps": [46]})
    sql, args = connection.calls[-1]
    assert "INSERT INTO fabric_event_sources " in sql
    assert args[:3] == ("operator", "source", 47)
    assert args[3].isoformat() == "2026-10-28T12:00:00+00:00"
    assert json.loads(args[4]) == [46]
