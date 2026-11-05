"""S2: the mirrored Pi activity rows are a private stream. Viewers cannot read them through /events and the shared feeds
(in memory and on a real PostgreSQL history) never contain them; a restart still hydrates the projection."""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import PostgresSource, load_into
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.mission_activity import ENTITY_TYPE
from herdr_engineering.control_plane.projection_history import PostgresHistory, hidden_history
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.e2e.pg import ThrowawayPostgres, find_bins

asyncpg = pytest.importorskip("asyncpg")


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def snapshot(key, at):
    return {'entity_type': 'mission_activity', 'entity_id': f'pi:{key}', 'source': 'pi',
            'event_type': 'mission_activity.snapshot',
            'payload': {'contract_version': 1, 'host_id': 'h', 'instance_id': 'i', 'session_id': 's',
                        'observed_at': stamp(at), 'snapshot_at': stamp(at), 'mission_revision': 1,
                        'connection': 'connected',
                        'mission': {'id': key, 'title': 'private-title-marker', 'status': 'RUNNING'},
                        'activity': {'health': 'active'}}}


@pytest.fixture(scope='module')
def pg(tmp_path_factory):
    bins = find_bins({})
    if bins is None:
        pytest.skip('PostgreSQL server binaries are required')
    db = ThrowawayPostgres(bins, tmp_path_factory.mktemp('activity-privacy-pg')).start()
    try:
        yield db
    finally:
        db.close()


def test_the_store_type_is_private():
    assert hidden_history(ENTITY_TYPE) and hidden_history(ENTITY_TYPE.upper())


def test_events_route_hides_the_mirror_stream_from_a_viewer(pg):
    pg.query('TRUNCATE fabric_events')
    t = time.time()
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(pg.dsn))
    try:
        repo = ControlPlaneRepo(now=lambda: t, sink=DurableEventSink(conn=conn, run=loop.run_until_complete))
        IngestService(repo).ingest(snapshot('MSN-1', t))
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    server = ControlPlaneServer(ControlPlaneRepo(now=lambda: t), api_keys={'reader': 'viewer'})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        request = urllib.request.Request(
            f'http://127.0.0.1:{server.port}/api/v1/events?entity_type={ENTITY_TYPE}&entity_id=pi:MSN-1',
            headers={'X-API-Key': 'reader'})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        with caught.value as response:
            assert response.code == 404 and json.load(response) == {'error': 'not found'}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_postgres_feeds_exclude_the_mirror_and_restart_still_hydrates(pg):
    pg.query('TRUNCATE fabric_events')
    t = time.time()
    clock = [t]
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(pg.dsn))
    try:
        repo = ControlPlaneRepo(now=lambda: clock[0], sink=DurableEventSink(conn=conn, run=loop.run_until_complete))
        IngestService(repo).ingest(snapshot('MSN-1', t))
        repo.upsert_mission('m-public', title='Visible', stage='implementation', source='pi')
        assert pg.query('SELECT count(*) AS n FROM fabric_events WHERE entity_type=$1', ENTITY_TYPE)[0]['n'] == 1
        history = PostgresHistory(pg.dsn, loop.run_until_complete)
        try:
            recent = history.recent_events(100)
            assert recent, 'the public mission event should still be in the feed'
            assert all(e['entity_type'] != ENTITY_TYPE for e in recent)
            assert 'private-title-marker' not in json.dumps(recent)
            streams, order = history.export_events()
            assert not any(key.split('|', 1)[0] == ENTITY_TYPE for key in streams)
            assert all(row[0] != ENTITY_TYPE for row in order)
        finally:
            history.close()
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    clock[0] = t + 5
    loop = asyncio.new_event_loop()
    conn = loop.run_until_complete(asyncpg.connect(pg.dsn))
    try:
        again = ControlPlaneRepo(now=lambda: clock[0])
        load_into(again, PostgresSource(conn, run=loop.run_until_complete))
    finally:
        loop.run_until_complete(conn.close())
        loop.close()
    assert again.mission_activity.get('pi:MSN-1')['mission']['title'] == 'private-title-marker'
