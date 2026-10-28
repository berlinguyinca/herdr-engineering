"""Independent naming writers share one durable reservation transaction."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.control_plane.agent_identity import AgentIdentityVersionConflict
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from tests.control_plane.test_recovery_checkpoint import pg as checkpoint_pg

asyncpg = pytest.importorskip('asyncpg')
pg = checkpoint_pg


@pytest.fixture
def writers(pg):
    pg.query('TRUNCATE fabric_events')
    pg.query('TRUNCATE recovery_checkpoint_manifests CASCADE')
    loops = [asyncio.new_event_loop() for _ in range(2)]
    connections = [loop.run_until_complete(asyncpg.connect(pg.dsn)) for loop in loops]
    repos = [ControlPlaneRepo(sink=DurableEventSink(conn=conn, run=loop.run_until_complete))
             for loop, conn in zip(loops, connections, strict=True)]
    try:
        yield pg, repos
    finally:
        for loop, conn in zip(loops, connections, strict=True):
            loop.run_until_complete(conn.close())
            loop.close()


def test_separate_registries_cannot_claim_the_same_global_name(writers):
    db, (first, second) = writers
    first.upsert_session('a', name='Ada')
    second.upsert_session('b', name='Ada')
    one, two = first.get_session('a'), second.get_session('b')
    assert one['agent_handle'] != two['agent_handle']
    assert one['display_name'] != two['display_name']
    assert db.query("SELECT count(*) AS n FROM fabric_events WHERE entity_type='agent_identity'")[0]['n'] == 2


def test_existing_identity_returns_actual_durable_event_without_duplicate_allocation(writers):
    db, (first, second) = writers
    first.upsert_session('a', name='Ada')
    second.upsert_session('a', name='Different provider title')
    original = first.get_session('a')
    observed = second.get_session('a')
    assert observed['display_name'] == original['display_name']
    assert observed['name_version'] == original['name_version']
    rows = db.query("SELECT event_id FROM fabric_events WHERE entity_type='agent_identity'")
    assert len(rows) == 1
    assert second.export_state()['agent_identity_history'][0]['event_id'] == rows[0]['event_id']


def test_stale_independent_rename_cannot_overwrite_a_durable_version(writers):
    db, (first, second) = writers
    for repo in (first, second):
        repo.upsert_session('a', name='Ada')
        repo.get_session('a')
    first.rename_agent('a', 'Grace', 1)
    with pytest.raises(AgentIdentityVersionConflict):
        second.rename_agent('a', 'Elm', 1)
    assert second.agent_identities.get('a')['display_name'] != 'Elm'
    assert db.query("SELECT count(*) AS n FROM fabric_events WHERE entity_type='agent_identity'")[0]['n'] == 2


def test_concurrent_process_registries_reserve_names_atomically(writers):
    db, repos = writers
    for index, repo in enumerate(repos):
        repo.upsert_session(str(index), name='Ada')
    ready = threading.Barrier(2)

    def allocate(index):
        ready.wait(timeout=5)
        return repos[index].get_session(str(index))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(allocate, range(2)))
    assert len({profile['agent_handle'] for profile in results}) == 2
    assert db.query("SELECT count(*) AS n FROM fabric_events WHERE entity_type='agent_identity'")[0]['n'] == 2


def test_failed_identity_insert_does_not_publish_tentative_reservations(writers):
    db, (repo, _) = writers
    repo.upsert_session('a', name='Ada')
    store = repo._sink._store
    conn = store._conn

    class FailingConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        async def fetchrow(self, sql, *args):
            if sql.startswith('INSERT INTO fabric_events'):
                raise RuntimeError('injected write failure')
            return await conn.fetchrow(sql, *args)

    store._conn = FailingConnection()
    with pytest.raises(RuntimeError, match='injected write failure'):
        repo.get_session('a')
    assert repo.agent_identities.get('a') is None
    assert repo.export_state()['agent_identity_history'] == []
    assert db.query("SELECT count(*) AS n FROM fabric_events WHERE entity_type='agent_identity'")[0]['n'] == 0
