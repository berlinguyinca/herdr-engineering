"""Admission provenance through a real Postgres: durable history stays private and replay restores attribution."""
import asyncio
import json

import pytest

from herdr_engineering.control_plane.bridge import _LoopRunner
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.projection_history import PostgresHistory
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_inspector_admission import KEY, Sink, body, entry
from tests.control_plane.test_inspector_history import _postgres

SECRET_EVENT_ID = "envelope-secret-event-id"


@pytest.mark.integration
def test_history_is_private_and_replay_restores_admission_via_real_postgres(tmp_path):
    asyncpg = pytest.importorskip("asyncpg")
    sink = Sink()
    live = ControlPlaneRepo(sink=sink)
    envelope = body(payload={"kind": "message", "role": "user", "text": "hello"})
    envelope["event_id"] = SECRET_EVENT_ID
    IngestService(live).ingest(envelope, principal={**KEY, "api_key": "NEVER_COPY"})
    rows = [e for e in sink.events if e["entity_type"] == "inspector" and e["event_type"] == "inspector.message"]
    assert len(rows) == 1
    with _postgres(tmp_path) as dsn:
        if dsn is None:
            pytest.skip("no postgres available")

        async def seed():
            conn = await asyncpg.connect(dsn)
            try:
                for n, row in enumerate(rows):
                    await conn.execute(
                        "INSERT INTO fabric_events (event_id, sequence, event_type, entity_type, entity_id, session_id,"
                        " payload, source_timestamp, ingest_timestamp) VALUES ($1,$2,$3,'inspector',$4,$4,$5::jsonb,"
                        "now() - interval '1 hour',now() - interval '1 hour')",
                        row["event_id"], n, row["event_type"], "session", json.dumps(row["payload"]))
            finally:
                await conn.close()
        asyncio.run(seed())
        runner = _LoopRunner()
        history = PostgresHistory(dsn, runner.run)
        try:
            repo = ControlPlaneRepo()
            repo._durable_history = history
            for role in ("viewer", "operator"):
                page = repo.inspector_history_page("session", role)
                assert page["events"]
                text = json.dumps(page)
                for needle in ("_inspector_context", "admission", KEY["id"], SECRET_EVENT_ID):
                    assert needle not in text, (role, needle)
        finally:
            history.close()
            runner._loop.call_soon_threadsafe(runner._loop.stop)
    replayed = ControlPlaneRepo()
    replayed.inspector.hydrate([
        {**row, "payload": json.loads(json.dumps(row["payload"]))} for row in rows])
    assert entry(replayed.inspector)["admission"] == entry(live.inspector)["admission"]
    assert entry(live.inspector)["admission"]["actor"] == KEY
