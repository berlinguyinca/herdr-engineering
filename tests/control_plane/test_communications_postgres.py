"""Actual Postgres durability/restart coverage, never a substituted database."""

import asyncio
import os
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
