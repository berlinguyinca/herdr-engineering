import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.projection_store import (
    CheckpointInvalid,
    ProjectionStore,
    decode_checkpoint,
    encode_checkpoint,
)

DSN = os.environ.get('HERDR_PERF_PG_DSN')


def test_checkpoint_json_integrity_and_compatibility():
    value = {'schema': 1, 'state': {'services': {'s': {'status': 'healthy'}}}}
    packed, digest = encode_checkpoint(value)
    assert decode_checkpoint(packed, digest) == value
    with pytest.raises(CheckpointInvalid):
        decode_checkpoint(packed[:-2] + b'xx', digest)
    with pytest.raises(CheckpointInvalid):
        decode_checkpoint(packed, '0' * 64)
    bad, checksum = encode_checkpoint({'schema': 999})
    with pytest.raises(CheckpointInvalid):
        decode_checkpoint(bad, checksum)
    bad, checksum = encode_checkpoint({'schema': True})
    with pytest.raises(CheckpointInvalid):
        decode_checkpoint(bad, checksum)


@asynccontextmanager
async def database():
    import asyncpg
    from herdr_engineering.control_plane.db import Migrator

    admin = await asyncpg.connect(DSN)
    schema = 'checkpoint_' + uuid4().hex
    await admin.execute(f'CREATE SCHEMA {schema}')
    conn = await asyncpg.connect(DSN, server_settings={'search_path': schema})
    try:
        await Migrator(conn).apply(str(Path(__file__).parents[2] / 'deploy/control-plane/migrations'))
        yield schema, conn
    finally:
        await conn.close()
        await admin.execute(f'DROP SCHEMA {schema} CASCADE')
        await admin.close()


async def append(conn, event_id, sequence=0):
    await conn.execute(
        "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,source_timestamp) "
        "VALUES($1,$2,'ServiceRegistered','service','s','{}',now())", event_id, sequence)


@pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')
def test_frontier_waits_for_all_preceding_commits_and_releases_writers_before_fold():
    async def scenario():
        import asyncpg
        async with database() as (schema, conn):
            slow = await asyncpg.connect(DSN, server_settings={'search_path': schema})
            later = await asyncpg.connect(DSN, server_settings={'search_path': schema})
            transaction = slow.transaction()
            await transaction.start()
            try:
                await append(slow, 'slow', 0)
                await append(later, 'committed-first', 1)
                reached = asyncio.Event()
                release = asyncio.Event()

                async def capture():
                    async with ProjectionStore(conn).snapshot() as point:
                        assert await conn.fetchval('SELECT count(*) FROM fabric_events') == 2
                        reached.set()
                        await release.wait()
                        assert await conn.fetchval('SELECT count(*) FROM fabric_events') == 2
                        return point['position']

                task = asyncio.create_task(capture())
                await asyncio.sleep(.05)
                assert not reached.is_set(), 'checkpoint must wait for the lower position to commit'
                await transaction.commit()
                await asyncio.wait_for(reached.wait(), 2)
                await append(later, 'after-snapshot', 2)
                release.set()
                frontier = await task
                changes = await ProjectionStore(conn).changes_after(frontier)
                assert [row['event_id'] for row in changes] == ['after-snapshot']
            finally:
                if slow.is_in_transaction():
                    await transaction.rollback()
                await slow.close()
                await later.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')
def test_conflict_noops_rollback_and_delete_are_tracked_without_touching_raw_history():
    async def scenario():
        async with database() as (_, conn):
            store = ProjectionStore(conn)
            await append(conn, 'one')
            await conn.execute("INSERT INTO fabric_events SELECT * FROM fabric_events WHERE event_id='one' ON CONFLICT DO NOTHING")
            assert len(await store.changes_after(0)) == 1
            tx = conn.transaction()
            await tx.start()
            await append(conn, 'rolled-back', 1)
            await tx.rollback()
            assert len(await store.changes_after(0)) == 1
            async with store.snapshot() as point:
                frontier = point['position']
            await conn.execute("DELETE FROM fabric_events WHERE event_id='one'")
            tail = await store.changes_after(frontier)
            assert len(tail) == 1 and tail[0]['operation'] == 'D'
            assert await conn.fetchval('SELECT count(*) FROM fabric_events') == 0
    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')
def test_atomic_publication_keeps_previous_valid_checkpoint_and_raw_rows():
    async def scenario():
        async with database() as (_, conn):
            store = ProjectionStore(conn)
            await append(conn, 'one')
            await store.publish('code-v1', 'policy-v1', 1, {'schema': 1, 'state': {'generation': 1}})
            await store.publish('code-v1', 'policy-v1', 2, {'schema': 1, 'state': {'generation': 2}})
            await conn.execute("UPDATE projection_checkpoints SET checksum='bad' WHERE frontier=2")
            rows = await store.candidates('code-v1', 'policy-v1')
            valid = []
            for row in rows:
                try:
                    valid.append(decode_checkpoint(bytes(row['state']), row['checksum']))
                except CheckpointInvalid:
                    pass
            assert valid == [{'schema': 1, 'state': {'generation': 1}}]
            assert await conn.fetchval('SELECT count(*) FROM fabric_events') == 1
            assert await store.candidates('other-code', 'policy-v1') == []
    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')
def test_publisher_excludes_other_materializers_and_releases_on_failure():
    async def scenario():
        import asyncpg
        async with database() as (schema, conn):
            other = await asyncpg.connect(DSN, server_settings={'search_path': schema})
            try:
                with pytest.raises(RuntimeError, match='failed build'):
                    async with ProjectionStore(conn).publisher():
                        with pytest.raises(CheckpointInvalid, match='materializer'):
                            async with ProjectionStore(other).publisher():
                                pytest.fail('overlapping checkpoint builds')
                        # Readers still obtain a fenced snapshot.
                        async with ProjectionStore(other).snapshot():
                            pass
                        raise RuntimeError('failed build')
                async with ProjectionStore(other).publisher():
                    pass
            finally:
                await other.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')
def test_fence_timeout_closes_session_without_stranding_writer_lock():
    async def scenario():
        import asyncpg
        async with database() as (schema, conn):
            writer = await asyncpg.connect(DSN, server_settings={'search_path': schema})
            tx = writer.transaction()
            await tx.start()
            try:
                await append(writer, 'blocked')
                with pytest.raises(TimeoutError):
                    async with ProjectionStore(conn, fence_timeout=.05).snapshot():
                        pytest.fail('uncommitted writer was skipped')
                assert conn.is_closed()
                await tx.commit()
                await asyncio.wait_for(append(writer, 'unblocked', 1), 2)
            finally:
                await writer.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')
def test_frontier_does_not_regress_when_change_metadata_is_pruned():
    async def scenario():
        async with database() as (_, conn):
            await append(conn, 'one')
            store = ProjectionStore(conn)
            async with store.snapshot() as point:
                frontier = point['position']
            await store.publish('code', 'policy', frontier, {'schema': 1})
            assert await conn.fetchval('SELECT count(*) FROM projection_event_changes') == 0
            async with store.snapshot() as next_point:
                assert next_point['position'] == frontier
            assert await conn.fetchval('SELECT count(*) FROM fabric_events') == 1
    asyncio.run(scenario())
