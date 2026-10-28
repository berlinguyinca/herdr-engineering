"""Events replayed from Postgres reach the repo with dict payloads, not the jsonb text asyncpg returns."""
import asyncio
import hashlib
import json
import threading
import urllib.error
import urllib.request
from contextlib import asynccontextmanager

import pytest

from herdr_engineering.control_plane import bridge
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class CursorConnection:
    def __init__(self, rows):
        self.rows = rows
        self.in_transaction = False
        self.transactions = []
        self.fetches = []
        self.prefetches = []
        self.fail_at = None

    @asynccontextmanager
    async def transaction(self, **options):
        self.transactions.append(options)
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def fetch(self, sql):
        assert self.in_transaction
        assert "fabric_events" not in sql, "event log must use the bounded cursor"
        self.fetches.append(sql)
        return []

    async def cursor(self, sql, *, prefetch):
        assert self.in_transaction
        assert sql == "SELECT * FROM fabric_events ORDER BY ingest_timestamp, sequence"
        self.prefetches.append(prefetch)
        for index, row in enumerate(self.rows):
            assert self.in_transaction
            if index == self.fail_at:
                raise RuntimeError("cursor interrupted")
            yield dict(row)


def replay_rows(count=2505):
    stamp = "2026-01-01T00:00:00+00:00"
    return [{"event_id": f"event-{index}", "entity_type": "mission",
             "entity_id": "pi:M1", "event_type": "MissionUpdated",
             "sequence": index, "source_timestamp": stamp, "ingest_timestamp": stamp,
             "payload": json.dumps({"title": f"Title {index}", "source": "pi", "stage": "active"})}
            for index in range(count)]


def test_cursor_replays_all_rows_then_releases_event_cache_without_refetching_resources():
    rows = replay_rows()
    conn = CursorConnection(rows)
    source = bridge.PostgresSource(conn, run=asyncio.run)
    repo = ControlPlaneRepo()
    counts = bridge.load_into(repo, source)
    assert counts["events"] == len(rows)
    assert [event["event_id"] for event in repo.stream("mission", "pi:M1")] == [
        row["event_id"] for row in rows]
    assert repo.get_mission("pi:M1")["title"] == "Title 2504"
    assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert conn.prefetches == [1000]
    assert not conn.in_transaction
    assert "events" not in source._cache
    fetches = list(conn.fetches)
    for _ in range(2):
        assert source.missions() == source.sessions() == source.services() == source.hosts() == []
    assert conn.fetches == fetches
    assert len(conn.transactions) == 1

    # The source can hydrate again, including events added since its first snapshot.
    conn.rows = replay_rows(2506)
    again = ControlPlaneRepo()
    assert bridge.load_into(again, source)["events"] == 2506
    assert again.get_mission("pi:M1")["title"] == "Title 2505"
    assert len(conn.transactions) == 2
    assert "events" not in source._cache


def test_cursor_failure_releases_transaction_and_retry_loads_complete_log():
    conn = CursorConnection(replay_rows(4))
    conn.fail_at = 2
    source = bridge.PostgresSource(conn, run=asyncio.run)
    repo = ControlPlaneRepo()
    with pytest.raises(RuntimeError, match="cursor interrupted"):
        bridge.load_into(repo, source)
    assert not conn.in_transaction
    assert source._cache is None
    assert repo.list_missions() == []
    conn.fail_at = None
    assert bridge.load_into(repo, source)["events"] == 4


def test_fetch_only_connection_remains_reusable_after_event_cache_release():
    class FetchConnection:
        def __init__(self):
            self.calls = []

        async def fetch(self, sql):
            self.calls.append(sql)
            return replay_rows(3) if "fabric_events" in sql else []

    conn = FetchConnection()
    source = bridge.PostgresSource(conn, run=asyncio.run)
    assert len(asyncio.run(source._fetch_all())["events"]) == 3
    repo = ControlPlaneRepo()
    assert bridge.load_into(repo, source)["events"] == 3
    assert "events" not in source._cache
    calls = len(conn.calls)
    assert source.hosts() == []
    assert len(conn.calls) == calls
    # Explicit event reads refetch the complete snapshot rather than returning an empty log.
    assert len(source.events()) == 3
    assert len(conn.calls) == calls + 5


def test_cursor_decodes_once_and_keeps_envelope_fields_and_payload(monkeypatch):
    row = replay_rows(1)[0]
    row.update(schema_version=2, source="pi", source_sequence=1,
               actor=json.dumps({"id": "agent", "kind": "agent"}),
               evidence_refs=json.dumps([{"type": "url", "id": "https://example.test/evidence"}]))
    conn = CursorConnection([row])
    source = bridge.PostgresSource(conn, run=asyncio.run)
    original_loads = json.loads
    decoded = []

    def loads(value):
        decoded.append(value)
        return original_loads(value)

    monkeypatch.setattr(bridge.json, "loads", loads)
    cached = source.events()[0]
    assert isinstance(cached["payload"], dict)
    streams, order = bridge._rebuild_events(source.take_events(), reuse=True)
    event = streams["mission|pi:M1"][0]
    assert event is cached
    assert event["payload"] == original_loads(row["payload"])
    assert event["actor"] == original_loads(row["actor"])
    assert event["evidence_refs"] == original_loads(row["evidence_refs"])
    assert event["source_sequence"] == 1
    assert order == []  # envelope-only rows remain in the streams for repo replay/ledger hydration
    assert decoded == [row["payload"], row["actor"], row["evidence_refs"]]


def test_cursor_preserves_full_projection_replay_and_idempotency():
    class Sink:
        def __init__(self):
            self.rows = []

        def record(self, event):
            self.rows.append(json.loads(json.dumps(event)))

        record_required = record

    sink = Sink()
    live = ControlPlaneRepo(sink=sink, now=lambda: 1_800_000_000)
    ingest = IngestService(live)
    live.upsert_mission("M1", source="pi", title="Old mission", stage="planning")
    live.set_mission_stage("pi:M1", "implementation")
    live.set_mission_stage("pi:M1", "review")
    live.add_token_usage({"request_id": "request1", "mission_id": "pi:M1", "model": "gpt",
                          "input_tokens": 5, "output_tokens": 3})
    live.add_resource_usage({"mission_id": "pi:M1", "kind": "cpu", "value": 7})
    ingest.ingest({"source": "pi", "entity_type": "test_run", "entity_id": "run1",
                   "event_type": "test_run.completed", "payload": {"mission_id": "M1", "passed": 2}})
    ingest.ingest({"source": "fabric-agent", "entity_type": "host", "entity_id": "host1",
                   "event_type": "host.heartbeat", "payload": {"telemetry": {"cpu": 12}, "maintenance": True}})
    envelope = {"schema_version": 2, "event_id": "graph-1", "source": "pi",
                "source_sequence": 1, "entity_type": "lineage", "entity_id": "task:t1",
                "event_type": "node.upsert", "visibility_scope": "org",
                "payload": {"node_type": "task", "node_id": "task:t1", "label": "Keep this node"}}
    principal = {"kind": "agent", "id": "key:writer", "authenticated_by": "api_key"}
    ingest.ingest(envelope, principal)
    live.save_scheduler_reservation({"reservation_id": "r1", "state": "committed"})
    live.save_scheduler_lease({"lease_id": "l1", "state": "running"})
    rows = [{**row, "payload": json.dumps(row["payload"]),
             **{key: json.dumps(row[key]) for key in ("actor", "evidence_refs") if key in row}}
            for row in sink.rows]
    source = bridge.PostgresSource(CursorConnection(rows), run=asyncio.run)
    for _ in range(2):
        restored = ControlPlaneRepo(now=lambda: 1_800_000_000)
        bridge.load_into(restored, source)
        assert restored.get_mission("pi:M1")["title"] == "Old mission"
        assert restored.stage_analytics() == live.stage_analytics()
        assert restored.usage_analytics(mission_id="pi:M1") == live.usage_analytics(mission_id="pi:M1")
        assert restored.get_test_run("run1") == live.get_test_run("run1")
        assert restored.graph.snapshot() == live.graph.snapshot()
        assert restored.get_host("host1")["maintenance"] is True
        assert restored.get_host("host1")["latest"]["cpu"] == 12
        assert restored.list_scheduler_reservations() == live.list_scheduler_reservations()
        assert restored.list_scheduler_leases() == live.list_scheduler_leases()
        assert IngestService(restored).ingest(envelope, principal)["envelope"]["duplicate"] is True


@pytest.mark.parametrize("use_cursor", [False, True])
def test_artifact_owner_and_canonical_id_preserve_access_after_replay(use_cursor):
    key = "artifact-owner-test-key"
    owner = f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:16]}:operator"
    original = ControlPlaneRepo()
    artifact_id = original.register_artifact(
        artifact_id="artifact_canonical_registry_id", created_by=owner,
        original_filename="owned.txt", mime_type="text/plain", storage_key="object-key",
        sha256="digest", size_bytes=5,
    )
    original.materialize_artifact(artifact_id, "host1", "session1", "/tmp/owned.txt")
    rows = [{**event, "payload": json.dumps(event["payload"])}
            for stream in original.export_state()["events"].values() for event in stream]

    class FetchConnection:
        async def fetch(self, sql):
            return [dict(row) for row in rows] if "fabric_events" in sql else []

    conn = CursorConnection(rows) if use_cursor else FetchConnection()
    source = bridge.PostgresSource(conn, run=asyncio.run)
    restored = ControlPlaneRepo()
    for _ in range(2):
        bridge.load_into(restored, source)
        artifact = restored.get_artifact(artifact_id)
        assert artifact["artifact_id"] == "artifact_canonical_registry_id"
        assert artifact["created_by"] == owner
        assert artifact["storage_key"] == "object-key"
        assert [row["artifact_id"] for row in restored.list_artifacts(created_by=owner)] == [artifact_id]
        assert "events" not in source._cache

    # No in-memory grants survive this restart: ownership must come from replay.
    server = ControlPlaneServer(restored, artifact_acl=ArtifactAcl(),
                               api_keys={key: "operator", "other-key": "operator"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        def get(path, api_key):
            request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                             headers={"X-API-Key": api_key})
            with urllib.request.urlopen(request) as response:
                return json.load(response)

        assert get(f"/api/v1/artifacts/{artifact_id}", key)["created_by"] == owner
        with pytest.raises(urllib.error.HTTPError) as error:
            get(f"/api/v1/artifacts/{artifact_id}", "other-key")
        assert error.value.code == 404
        error.value.close()
        assert get("/api/v1/artifacts", "other-key")["artifacts"] == []
        assert [row["artifact_id"] for row in get("/api/v1/artifacts", key)["artifacts"]] == [artifact_id]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_postgres_empty_tables_replay_scheduler_policy_and_artifact_registry():
    source = ControlPlaneRepo()
    source.upsert_host(
        "beast", "beast", "100.0.0.2", host_uid="uid-beast", aliases=["BEAST"],
        platform="linux", scheduler_owned=True, scheduler_trusted=True,
        scheduler_policy={"mode": "automatic", "revision": 0},
        scheduler_capacity={"total_cpu": 4, "total_memory_gib": 8},
    )
    source.upsert_host("beast", "beast", None, scheduler_capacity={"total_cpu": 8})
    source.set_host_policy("beast", mode="draining", expected_revision=0)
    source.save_scheduler_reservation({"reservation_id": "r1", "state": "reserved"})
    source.save_scheduler_reservation({"reservation_id": "r1", "state": "committed"})
    source.save_scheduler_lease({"lease_id": "l1", "state": "running", "generation": 1})
    artifact = source.register_artifact(original_filename="report.txt", mime_type="text/plain", data=b"report")
    rows = [event for stream in source.export_state()["events"].values() for event in stream]

    class Connection:
        async def fetch(self, sql):
            if "fabric_events" in sql:
                assert "ORDER BY ingest_timestamp, sequence" in sql
                return [{**row, "payload": json.dumps(row["payload"])} for row in rows]
            return []

    restored = ControlPlaneRepo()
    durable = bridge.PostgresSource(conn=Connection(), run=asyncio.run)
    for _ in range(2):
        bridge.load_into(restored, durable)
        snapshot = restored.host_snapshot("beast")
        assert snapshot.host_uid == "uid-beast"
        assert snapshot.owned and snapshot.trusted
        assert snapshot.platform == "linux"
        assert snapshot.aliases == ("beast",)
        assert snapshot.policy.mode.value == "draining"
        assert snapshot.policy.revision == 1
        assert restored.get_host("beast")["scheduler_capacity"] == {"total_cpu": 8}
        assert restored.list_scheduler_reservations() == [{"reservation_id": "r1", "state": "committed"}]
        assert restored.list_scheduler_leases() == [{"lease_id": "l1", "state": "running", "generation": 1}]
        assert [a["artifact_id"] for a in restored.list_artifacts(artifact_type="other")] == [artifact]


def test_scheduler_policy_replay_does_not_refresh_host_liveness():
    rows = [
        {"entity_type": "host", "entity_id": "h1", "event_type": "HostRegistered",
         "sequence": 0, "source_timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"host_name": "h1", "status": "unavailable"}},
        {"entity_type": "host", "entity_id": "h1", "event_type": "HostPolicyChanged",
         "sequence": 1, "source_timestamp": "2026-01-02T00:00:00+00:00",
         "payload": {"mode": "automatic", "revision": 1}},
    ]
    streams, _ = bridge._rebuild_events(rows)
    _, _, hosts, _, _ = bridge._reconstruct_from_events(streams)
    assert hosts["h1"]["status"] == "unavailable"
    assert hosts["h1"]["updated_at"] == "2026-01-01T00:00:00+00:00"
    assert hosts["h1"]["scheduler_policy"] == {"mode": "automatic", "revision": 1}


def test_rebuild_events_decodes_jsonb_text_payloads():
    payload = {"repository": "o/r", "number": 3, "nested": {"a": [1, 2]}}
    rows = [
        {"event_id": "e1", "sequence": 0, "event_type": "CIFixRequested", "entity_type": "ci_fix",
         "entity_id": "cifix_1", "payload": json.dumps(payload), "source_timestamp": "2026-10-24T00:00:00+00:00"},
        {"event_id": "e2", "sequence": 1, "event_type": "CIFixCancelRequested", "entity_type": "ci_fix",
         "entity_id": "cifix_1", "payload": None, "source_timestamp": "2026-10-24T00:01:00+00:00"},
        {"event_id": "e3", "sequence": 2, "event_type": "Odd", "entity_type": "ci_fix",
         "entity_id": "cifix_1", "payload": "not json", "source_timestamp": "2026-10-24T00:02:00+00:00"},
    ]
    streams, order = bridge._rebuild_events(rows)
    events = streams["ci_fix|cifix_1"]
    assert events[0]["payload"] == payload
    assert events[1]["payload"] == {}
    assert events[2]["payload"] == {}
    repo = ControlPlaneRepo()
    repo.import_state({"events": streams, "event_order": order})
    assert repo.stream("ci_fix", "cifix_1")[0]["payload"]["repository"] == "o/r"
