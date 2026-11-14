"""Actual Postgres durability/restart coverage, never a substituted database."""

import asyncio
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

from herdr_engineering.communications import Outbox
from herdr_engineering.control_plane.bridge import _LoopRunner
from herdr_engineering.control_plane.communications import communication_page, record_observation
from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.projection_history import PostgresHistory
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink

pytestmark = pytest.mark.integration


def test_postgres_recovery_and_paginated_deduplication(tmp_path):
    dsn = os.environ.get("HERDR_COMMUNICATION_TEST_DSN")
    if not dsn:
        pytest.skip("Set HERDR_COMMUNICATION_TEST_DSN to a disposable herdr_comm_test database")
    assert urlsplit(dsn).path.rsplit("/", 1)[-1].startswith("herdr_comm_test")

    async def migrate():
        conn = await asyncpg.connect(dsn)
        try:
            await Migrator(conn).apply(
                str(Path(__file__).parents[2] / "deploy/control-plane/migrations")
            )
        finally:
            await conn.close()

    asyncio.run(migrate())
    sink = DurableEventSink(dsn)
    writer = ControlPlaneRepo(sink=sink)
    box = Outbox(tmp_path / "outbox.sqlite")
    prefix = "audit_" + uuid.uuid4().hex
    for n in range(5):
        operation = f"{prefix}_{n}"
        data = {
            "text": "clean safely",
            "host_id": "mac",
            "sender": {},
            "recipient": {},
            "route": [],
            "sender_origin": "unknown",
            "channel": "herdr.agent.prompt",
            "evidence_kind": "host_observation",
        }
        for status in ("queued", "delivered"):
            box.put(operation, status, data)
    for row in box.pending():
        record_observation(writer, row)
    loop = _LoopRunner()
    history = PostgresHistory(dsn, loop.run)
    recovered = ControlPlaneRepo()
    recovered._durable_history = history
    try:
        # A new repository has no in-memory audit rows: every result comes from Postgres.
        page = communication_page(recovered, limit=2)
        assert len(page["communications"]) == 2
        assert all(row["status"] == "delivered" for row in page["communications"])
        older = communication_page(recovered, limit=2, before=page["next_cursor"])
        assert {r["operation_id"] for r in page["communications"]}.isdisjoint(
            r["operation_id"] for r in older["communications"]
        )
        last = box.pending()[-1]
        assert record_observation(recovered, last) is False
    finally:
        history.close()
        if sink._conn:
            sink._run(sink._conn.close())


def test_real_mirror_database_failure_does_not_break_primary_command_receipts(tmp_path, caplog):
    from herdr_engineering.control_plane.api import ControlPlaneServer

    dsn = os.environ.get("HERDR_COMMUNICATION_TEST_DSN")
    if not dsn:
        pytest.skip("Set HERDR_COMMUNICATION_TEST_DSN to a disposable herdr_comm_test database")
    assert urlsplit(dsn).path.rsplit("/", 1)[-1].startswith("herdr_comm_test")

    async def setup(block):
        conn = await asyncpg.connect(dsn)
        try:
            await Migrator(conn).apply(
                str(Path(__file__).parents[2] / "deploy/control-plane/migrations")
            )
            await conn.execute(
                "ALTER TABLE fabric_events DROP CONSTRAINT IF EXISTS test_communication_mirror_refusal"
            )
            if block:
                await conn.execute(
                    "ALTER TABLE fabric_events ADD CONSTRAINT test_communication_mirror_refusal CHECK (entity_type <> 'communication') NOT VALID"
                )
        finally:
            await conn.close()

    asyncio.run(setup(True))
    sink = DurableEventSink(dsn)
    repo = ControlPlaneRepo(sink=sink)
    sid = repo.create_session()
    server = ControlPlaneServer(repo, api_key="test-ingestion-key")
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/v1/sessions/{sid}/messages",
            data=json.dumps(
                {
                    "message": "No live target: refusal must stay a transport refusal",
                    "operation_id": "mirror_" + uuid.uuid4().hex,
                }
            ).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": "test-ingestion-key"},
        )
        with pytest.raises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request)
        assert response.value.code == 502
        result = json.loads(response.value.read())
        assert result["error"] == "transport_error"
        assert result["status"] == "failed"
        events = repo.stream("session", sid)
        assert any(e["event_type"] == "SessionCommandQueued" for e in events)
        assert events[-1]["event_type"] == "SessionCommandSettled"
        assert events[-1]["payload"]["status"] == "failed"
        # The exact same real database fault also cannot rewrite a durably saved successful receipt.
        delivered = {
            "operation_id": "delivered_" + uuid.uuid4().hex,
            "status": "delivered",
            "text": "receipt fixture",
        }
        repo.record_event(
            "session", sid, "SessionCommandSettled", delivered, session_id=sid, require_durable=True
        )
        from herdr_engineering.control_plane.communications import record_command_safely

        record_command_safely(repo, repo.get_session(sid), delivered)
        assert delivered["status"] == "delivered"
        assert repo.stream("session", sid)[-1]["payload"]["status"] == "delivered"
        assert "communication mirror unavailable" in caplog.text
    finally:
        server.shutdown()
        if sink._conn:
            sink._run(sink._conn.close())
        asyncio.run(setup(False))
