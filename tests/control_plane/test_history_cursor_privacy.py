"""Opaque cursors must not publish offline guesses of redacted journal secrets."""
import asyncio
import base64
import hashlib
import json

import pytest

from herdr_engineering.control_plane.durable_history import DurableHistoryReader
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_postgres_recovery import _insert, _row
from tests.control_plane.test_recovery_checkpoint import pg as checkpoint_pg

asyncpg = pytest.importorskip('asyncpg')
pg = checkpoint_pg


def plain_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def decode(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))


def test_memory_cursor_is_not_a_plaintext_hash_of_a_redacted_value():
    repo = ControlPlaneRepo()
    for _ in range(2):
        repo.record_event('test_run', 'r', 'TestRunUpdated', {'passed': '123456'})
    page = repo.stream_page('test_run', 'r', limit=1)
    assert decode(page['next_cursor'])[3] != plain_digest(page['events'][0])
    assert repo.stream_page('test_run', 'r', limit=1, before=page['next_cursor'])['has_more'] is False


def test_legacy_null_nonce_cursor_cannot_validate_offline_secret_guesses(pg):
    pg.query('TRUNCATE fabric_events')
    pg.query('ALTER TABLE fabric_events DISABLE TRIGGER fabric_event_recovery_instance')
    try:
        _insert(pg, [_row('host', 'h', 'HostHeartbeat', {'password': '123456'}, n) for n in range(2)])
    finally:
        pg.query('ALTER TABLE fabric_events ENABLE TRIGGER fabric_event_recovery_instance')

    async def exercise():
        conn = await asyncpg.connect(pg.dsn)
        try:
            reader = DurableHistoryReader(conn)
            page = await reader.page('host', 'h', limit=1)
            cursor = decode(page['next_cursor'])
            assert cursor[5] != plain_digest([page['events'][0], None])
            assert cursor[4] != plain_digest([page['events'][0]['event_id'], ''])
            older = await reader.page('host', 'h', limit=1, before=page['next_cursor'])
            assert len(older['events']) == 1 and older['has_more'] is False
        finally:
            await conn.close()
    asyncio.run(exercise())
