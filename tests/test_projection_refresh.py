import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane import projection_refresh as refresh
from herdr_engineering.control_plane.projection_store import ProjectionStore


def test_refresh_policy_sanitized_and_closed(monkeypatch):
    seen = {}

    class Source:
        recovery_info = {'mode': 'checkpoint', 'frontier': 4, 'dsn': 'secret',
                         'elapsed_ms': 1.2}

        def __init__(self, **kwargs):
            seen.update(kwargs)

        def close(self):
            seen['closed'] = True

    def load(repo, source):
        assert repo._host_aliases == {'alias': 'host'}
        assert repo._service_archive_days == 7
        assert repo.inspector.limits == refresh.ControlPlaneRepo().inspector.limits
        return {'events': 5, 'dsn': 123}

    monkeypatch.setattr(refresh.bridge, 'PostgresSource', Source)
    monkeypatch.setattr(refresh.bridge, 'load_into', load)
    assert refresh.refresh_checkpoint('secret', host_aliases={'alias': 'host'}) == {
        'counts': {'events': 5},
        'recovery_info': {'mode': 'checkpoint', 'frontier': 4, 'elapsed_ms': 1.2}}
    assert seen == {'dsn': 'secret', 'checkpoint_publish': True, 'closed': True}


def test_failure_closes_source(monkeypatch):
    closed = []

    class Source:
        def __init__(self, **kwargs):
            pass

        def close(self):
            closed.append(True)

    def fail(*args):
        raise RuntimeError('secret DSN')

    monkeypatch.setattr(refresh.bridge, 'PostgresSource', Source)
    monkeypatch.setattr(refresh.bridge, 'load_into', fail)
    with pytest.raises(RuntimeError):
        refresh.refresh_checkpoint('secret')
    assert closed == [True]


def test_periodic_nonblocking_nonoverlapping_and_stop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(refresh, 'refresh_checkpoint', blocked)
    periodic = refresh.ProjectionRefresh('secret', interval=.01)

    async def exercise():
        periodic.start()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # Lease/job event-loop work continues while refresh is blocked.
            await asyncio.wait_for(asyncio.sleep(.05), .5)
            assert len(calls) == 1
            assert calls[0] != threading.get_ident()
        finally:
            periodic._stop.set()
            release.set()
            await asyncio.to_thread(periodic.stop)

    asyncio.run(exercise())
    assert not periodic._thread.is_alive()
    assert len(calls) == 1


def test_periodic_retries_and_sanitizes_failures(monkeypatch, caplog):
    retried = threading.Event()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        if len(calls) >= 2:
            retried.set()
        raise ValueError('postgres://password@secret')

    monkeypatch.setattr(refresh, 'refresh_checkpoint', fail)
    periodic = refresh.ProjectionRefresh('secret', interval=.01)
    periodic.start()
    try:
        assert retried.wait(2)
    finally:
        periodic.stop()
    assert 'ValueError' in caplog.text
    assert 'password' not in caplog.text
    assert 'postgres://' not in caplog.text


@pytest.mark.parametrize('interval', [0, -1, float('nan'), float('inf')])
def test_interval_bounded(interval):
    with pytest.raises(ValueError):
        refresh.ProjectionRefresh('secret', interval=interval)


# --- real-Postgres diagnostics and skip-when-unchanged -----------------------

DSN = os.environ.get('HERDR_PERF_PG_DSN')
needs_pg = pytest.mark.skipif(not DSN, reason='dedicated HERDR_PERF_PG_DSN required')


@asynccontextmanager
async def schema_db():
    import asyncpg

    from herdr_engineering.control_plane.db import Migrator

    admin = await asyncpg.connect(DSN)
    schema = 'refresh_' + uuid4().hex
    await admin.execute(f'CREATE SCHEMA {schema}')
    conn = await asyncpg.connect(DSN, server_settings={'search_path': schema})
    try:
        await Migrator(conn).apply(str(Path(__file__).parents[1] / 'deploy/control-plane/migrations'))
        sep = '&' if '?' in DSN else '?'
        yield conn, f'{DSN}{sep}search_path={schema}'
    finally:
        await conn.close()
        await admin.execute(f'DROP SCHEMA {schema} CASCADE')
        await admin.close()


async def _append(conn, event_id):
    await conn.execute(
        "INSERT INTO fabric_events(event_id,sequence,event_type,entity_type,entity_id,payload,source_timestamp) "
        "VALUES($1,0,'ServiceRegistered','service','s','{}',now())", event_id)


def _pass_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith('projection refresh:')]


@needs_pg
def test_failure_warning_carries_message_when_lock_is_held(caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        async with schema_db() as (conn, dsn):
            async with ProjectionStore(conn).publisher():
                await asyncio.to_thread(refresh.ProjectionRefresh(dsn)._pass)

    asyncio.run(scenario())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('CheckpointInvalid' in w and 'another projection checkpoint materializer is active' in w
               for w in warnings), warnings
    assert any(line.startswith('projection refresh: failed in ') for line in _pass_lines(caplog))


@needs_pg
def test_pass_skipped_when_frontier_current_and_runs_after_append(caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        async with schema_db() as (conn, dsn):
            periodic = refresh.ProjectionRefresh(dsn)
            await _append(conn, 'e1')
            await asyncio.to_thread(periodic._pass)   # no checkpoint: must publish
            await asyncio.to_thread(periodic._pass)   # nothing new: skip
            await _append(conn, 'e2')
            await asyncio.to_thread(periodic._pass)   # new row: publish again
            await asyncio.to_thread(periodic._pass)   # and current again

    asyncio.run(scenario())
    outcomes = [line.split()[2] for line in _pass_lines(caplog)]
    assert outcomes == ['published', 'skipped', 'published', 'skipped']


@needs_pg
def test_no_checkpoint_is_never_current():
    async def scenario():
        async with schema_db() as (conn, dsn):
            assert not await asyncio.to_thread(refresh.checkpoint_is_current, dsn)
            await _append(conn, 'e1')
            assert not await asyncio.to_thread(refresh.checkpoint_is_current, dsn)

    asyncio.run(scenario())


def test_currency_check_failure_falls_back_to_doing_the_pass():
    assert refresh.checkpoint_is_current('postgres://nobody@127.0.0.1:1/none') is False


def test_failure_message_redacts_urls():
    assert refresh._safe_message(ValueError('bad postgres://u:pw@h/db now')) == 'bad <url> now'
    assert len(refresh._safe_message(ValueError('x' * 900))) == 500
