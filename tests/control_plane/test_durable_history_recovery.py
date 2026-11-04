"""Compacted full/checkpoint startup must not truncate either HTTP history form."""
import asyncio
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import PostgresSource, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_postgres_recovery import _insert, _row, _snapshot
from tests.control_plane.test_recovery_checkpoint import _build
from tests.control_plane.test_recovery_checkpoint import pg as checkpoint_pg

asyncpg = pytest.importorskip('asyncpg')
pg = checkpoint_pg


@pytest.fixture
def db(pg):
    pg.query('TRUNCATE fabric_events')
    pg.query('TRUNCATE recovery_checkpoint_manifests CASCADE')
    return pg


def get(server, **query):
    try:
        response = urllib.request.urlopen(
            f'http://127.0.0.1:{server.port}/api/v1/events?{urlencode(query)}', timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def start(db, kind, checkpoint):
    shapes = {
        'host': ('HostHeartbeat', {'host_name': 'host', 'tailnet_ip': '100.64.0.1', 'cpu': 1, 'mem': 2}),
        'session': ('SessionUpdated', {'host_id': 'h', 'mission_id': None, 'agent_role': 'pi',
                                       'model': 'model', 'status': 'stopped'}),
        'service': ('ServiceUpdated', _snapshot()),
    }
    event, payload = shapes[kind]
    rows = [_row(kind, 'entity', event, payload, n) for n in range(12)]
    _insert(db, rows)
    if checkpoint:
        _build(db)
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(db.dsn))
    source = PostgresSource(conn, run=loop.run_until_complete)
    repo = ControlPlaneRepo()
    load_into(repo, source)
    assert len(repo._events[(kind, 'entity')]) == 2
    server = ControlPlaneServer(repo, allow_open=True)
    worker = threading.Thread(target=server.serve, daemon=True)
    worker.start()
    server.wait_until_ready()
    return rows, source, server, worker, loop, conn


def close(server, worker, loop, conn):
    server.shutdown()
    worker.join(timeout=5)
    loop.run_until_complete(conn.close())
    loop.close()


@pytest.mark.parametrize('kind', ['host', 'session', 'service'])
@pytest.mark.parametrize('checkpoint', [False, True])
def test_raw_history_remains_complete_after_compacted_startup(db, kind, checkpoint):
    rows, source, server, worker, loop, conn = start(db, kind, checkpoint)
    try:
        status, legacy = get(server, entity_type=kind, entity_id='entity')
        assert status == 200
        assert [event['event_id'] for event in legacy['events']] == [row['event_id'] for row in rows]
        groups, cursor = [], None
        while True:
            query = {'entity_type': kind, 'entity_id': 'entity', 'limit': 3}
            if cursor is not None:
                query['before'] = cursor
            status, page = get(server, **query)
            assert status == 200
            groups.append(page['events'])
            if not page['has_more']:
                break
            cursor = page['next_cursor']
        assert [event['event_id'] for group in reversed(groups) for event in group] == [row['event_id'] for row in rows]
        assert source.recovery_metrics['mode'] == ('checkpoint' if checkpoint else 'full')
    finally:
        close(server, worker, loop, conn)


def test_database_failure_is_explicit_unavailable_not_incomplete_memory(db):
    _, source, server, worker, loop, conn = start(db, 'host', True)

    class Unavailable:
        def __getattr__(self, name):
            return getattr(conn, name)

        async def fetch(self, *args, **kwargs):
            raise RuntimeError('injected database failure with private details')

        async def fetchrow(self, *args, **kwargs):
            raise RuntimeError('injected database failure with private details')

    source._conn = Unavailable()
    try:
        for extra in ({}, {'limit': 3}):
            status, body = get(server, entity_type='host', entity_id='entity', **extra)
            assert status == 503 and body == {'error': 'Integration service is unavailable.'}
        status, body = get(server, entity_type='agent_identity', entity_id='entity', limit=3)
        assert status == 200 and body['events'] == []
    finally:
        close(server, worker, loop, conn)


def test_history_api_redacts_raw_journal_values_without_global_count_exceptions(db):
    _, _, server, worker, loop, conn = start(db, 'host', True)
    leaked = 'abc123xyz789'
    payload = {'host_name': f'runner leaked token {leaked}', 'tailnet_ip': '100.64.0.1',
               'cpu': 1, 'mem': 2, 'passed': 3}
    _insert(db, [_row('host', 'entity', 'HostHeartbeat', payload, 20)])
    try:
        for extra in ({}, {'limit': 3}):
            status, body = get(server, entity_type='host', entity_id='entity', **extra)
            assert status == 200
            assert leaked not in json.dumps(body)
            assert body['events'][-1]['payload']['passed'] == '[REDACTED]'
        raw = db.query("SELECT payload FROM fabric_events WHERE event_id='host-entity-20'")[0]['payload']
        assert leaked in raw
    finally:
        close(server, worker, loop, conn)


def test_delivery_history_preserves_counts_while_scanning_secret_values(db):
    _, _, server, worker, loop, conn = start(db, 'host', True)
    leaked = 'abc123xyz789'
    server.repo.upsert_test_run('run', passed=3, failed=1, status='failed',
                               results=[{'name': f'runner leaked token {leaked}'}])
    try:
        for extra in ({}, {'limit': 3}):
            status, body = get(server, entity_type='test_run', entity_id='run', **extra)
            assert status == 200
            assert leaked not in json.dumps(body)
            assert body['events'][-1]['payload']['passed'] == 3
    finally:
        close(server, worker, loop, conn)
