import asyncio
import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.bridge import PostgresSource, _LoopRunner, load_into
from herdr_engineering.control_plane.repo import ControlPlaneRepo

DSN = os.environ.get('HERDR_PERF_PG_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')


@pytest.fixture
def durable():
    import asyncpg
    from herdr_engineering.control_plane.db import Migrator
    runner = _LoopRunner()
    schema = 'projection_' + uuid4().hex

    async def prepare():
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA {schema}')
        conn = await asyncpg.connect(DSN, server_settings={'search_path':schema})
        await Migrator(conn).apply(str(Path(__file__).parents[2] / 'deploy/control-plane/migrations'))
        return admin,conn

    admin,conn=runner.run(prepare())
    sources=[]

    def recover(*, publish=False):
        source=PostgresSource(conn,dsn=DSN,run=runner.run,checkpoint_publish=publish)
        sources.append(source)
        repo=ControlPlaneRepo(now=lambda: 1000)
        counts=load_into(repo,source)
        return repo,source,counts

    try:
        yield conn,runner.run,recover
    finally:
        for source in sources:
            if source._history_provider is not None:
                source._history_provider.close()
        runner.run(conn.close())
        runner.run(admin.execute(f'DROP SCHEMA {schema} CASCADE'))
        runner.run(admin.close())
        runner._loop.call_soon_threadsafe(runner._loop.stop)
        runner._thread.join(3)


async def insert(conn,event_id,sequence,payload,*,kind='service',identity='service-one',actor=None):
    await conn.execute('INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,actor,source_timestamp,ingest_timestamp) '
                       "VALUES($1,$2,'ServiceRegistered',$3,$4,$5,$6,'1970-01-01T00:16:40Z','1970-01-01T00:16:40Z')",
                       event_id,sequence,kind,identity,json.dumps(payload),json.dumps(actor) if actor else None)


def test_checkpoint_replays_only_dirty_streams_but_legacy_history_stays_complete(durable):
    conn,run,recover=durable
    async def seed():
        for n in range(70):
            await insert(conn,f'event-{n}',n,{'service_name':'App','host_id':'h','port':8000,'status':'healthy','description':f'Version {n}'})
        for n in range(20):
            await insert(conn,f'quiet-{n}',n,{'service_name':'Quiet','host_id':'h','port':8001,'status':'healthy'},identity='quiet')
    run(seed())
    first,source,counts=recover(publish=True)
    assert source.recovery_info['mode']=='full_replay' and counts['events']==90
    assert len(first._events[('service','service-one')])==1
    assert len(first.stream('service','service-one'))==70
    run(insert(conn,'new',70,{'status':'unavailable'}))
    fast,second,counts=recover()
    assert second.recovery_info['mode']=='checkpoint'
    assert second.recovery_info['event_rows_scanned']<90
    assert fast.get_service('service-one')['description']=='Version 69'
    assert fast.get_service('service-one')['status']=='unavailable'
    assert len(fast.stream('service','service-one'))==71
    assert len(fast.export_state()['events']['service|service-one'])==71
    page=fast.stream_page('service','service-one',limit=17)
    ids=[]
    while True:
        ids[0:0]=[row['event_id'] for row in page['events']]
        if not page['has_more']:break
        page=fast.stream_page('service','service-one',limit=17,before=page['next_cursor'])
    assert len(ids)==len(set(ids))==71
    assert fast.health()['events']['cached']<fast.health()['events']['durable_total']


def test_database_history_cursor_survives_recovery_and_rejects_changed_anchor(durable):
    conn,run,recover=durable
    async def seed():
        for n in range(40):await insert(conn,f'e{n}',n,{'service_name':'App','host_id':'h','status':'healthy'})
    run(seed())
    first,_,_=recover(publish=True)
    page=first.stream_page('service','service-one',limit=10)
    cursor=page['next_cursor']
    expected=first.stream_page('service','service-one',limit=10,before=cursor)
    again,_,_=recover()
    assert again.stream_page('service','service-one',limit=10,before=cursor)==expected
    run(conn.execute("UPDATE fabric_events SET payload='{}' WHERE event_id='e30'"))
    with pytest.raises(ValueError,match='staleHistoryCursor'):
        again.stream_page('service','service-one',limit=10,before=cursor)


def test_equal_sequences_and_actor_shared_event_ids_are_not_lost(durable):
    conn,run,recover=durable
    run(insert(conn,'shared',0,{'status':'healthy'},actor={'id':'first'}))
    run(insert(conn,'shared',0,{'status':'unavailable'},actor={'id':'second'}))
    repo,_,_=recover(publish=True)
    latest=repo.stream_page('service','service-one',limit=1)
    older=repo.stream_page('service','service-one',limit=1,before=latest['next_cursor'])
    assert latest['events'][0]['event_id']==older['events'][0]['event_id']=='shared'
    assert latest['events'][0]['actor']!=older['events'][0]['actor']


def test_corrupt_latest_checkpoint_falls_back_without_losing_raw_rows(durable):
    conn,run,recover=durable
    run(insert(conn,'e1',0,{'service_name':'App','host_id':'h'}))
    _,_,_=recover(publish=True)
    run(conn.execute("UPDATE projection_checkpoints SET checksum='corrupt'"))
    repo,source,counts=recover()
    assert source.recovery_info['mode']=='full_replay'
    assert counts['events']==1 and len(repo.stream('service','service-one'))==1
    assert run(conn.fetchval('SELECT count(*) FROM fabric_events'))==1


def test_v1_memory_cursor_remains_valid_after_database_recovery(durable):
    conn, run, recover = durable
    for n in range(12):
        run(insert(conn, f'compat-{n}', n, {'description': str(n)}))
    first, _, _ = recover(publish=True)
    memory = ControlPlaneRepo(now=lambda: 1000)
    memory._events[('service', 'service-one')] = first.stream('service', 'service-one')
    page = memory.stream_page('service', 'service-one', limit=4)
    cursor = page['next_cursor']
    expected = memory.stream_page('service', 'service-one', limit=4, before=cursor)['events']
    fast, source, _ = recover()
    assert source.recovery_info['mode'] == 'checkpoint'
    assert fast.stream_page('service', 'service-one', limit=4, before=cursor)['events'] == expected
    run(conn.execute("UPDATE fabric_events SET payload='{}' WHERE event_id='compat-8'"))
    with pytest.raises(ValueError, match='staleHistoryCursor'):
        fast.stream_page('service', 'service-one', limit=4, before=cursor)


def test_recovered_database_history_api_denies_private_streams(durable):
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from herdr_engineering.control_plane.api import ControlPlaneServer

    conn, run, recover = durable
    kinds = ('inspector', 'host_metrics', 'agent_identity', 'agent_summary',
             'lineage', 'review_request', 'collab', 'collab_room')
    for n, kind in enumerate(kinds):
        run(insert(conn, f'private-{n}', n, {'text': 'private-marker'}, kind=kind, identity='private'))
    recover(publish=True)
    repo, source, _ = recover()
    assert source.recovery_info['mode'] == 'checkpoint'
    server = ControlPlaneServer(repo, api_keys={'reader': 'viewer'})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        for kind in kinds:
            for extra in ({}, {'limit': 1}, {'before': 'invalid'}):
                query = urllib.parse.urlencode({'entity_type': kind, 'entity_id': 'private', **extra})
                request = urllib.request.Request(f'http://127.0.0.1:{server.port}/api/v1/events?{query}',
                                                 headers={'X-API-Key': 'reader'})
                with pytest.raises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                with caught.value as response:
                    assert response.code == 404
                    assert json.load(response) == {'error': 'not found'}
        assert 'private-marker' not in json.dumps(repo.recent_events(100))
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_v2_backdated_tail_matches_forced_full_ledger_replay(durable):
    from datetime import UTC, datetime
    conn, run, recover = durable

    async def envelope(seq, received, actor='writer'):
        await conn.execute(
            'INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,'
            'actor,source,source_sequence,schema_version,source_timestamp,ingest_timestamp) '
            "VALUES($1,$2,'node.upsert','lineage','task:t1',$3,$4,'pi',$2,2,$5,$5)",
            f'{actor}-{seq}', seq, json.dumps({'_envelope': {'content_sha256': str(seq)}}),
            json.dumps({'id': actor}), datetime.fromtimestamp(received, UTC))

    run(envelope(1, 1000))
    run(envelope(4, 995))
    recover(publish=True)
    run(envelope(2, 990))
    run(envelope(1, 985, 'other'))
    fast, source, _ = recover()
    assert source.recovery_info['mode'] == 'checkpoint'
    run(conn.execute('DELETE FROM projection_checkpoints'))
    full, source, _ = recover()
    assert source.recovery_info['mode'] == 'full_replay'
    assert fast.ledger.export_checkpoint() == full.ledger.export_checkpoint()
    assert fast.ledger.source_state('pi', 'writer')['last_seen_at'] == '1970-01-01T00:16:30+00:00'
    assert fast.ledger.source_state('pi', 'writer')['gaps'][0]['from'] == 3
    assert fast.ledger.source_state('pi', 'other')['last_sequence'] == 1
