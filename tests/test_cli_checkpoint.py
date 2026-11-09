import json

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane import db, projection_refresh
from herdr_engineering.control_plane.projection_recovery import CheckpointBudgets
from herdr_engineering.control_plane.projection_store import ProjectionStore


@pytest.mark.parametrize('failure', [None, 'migrate', 'indexes', 'load'])
def test_explicit_checkpoint_order_cleanup_and_sanitization(monkeypatch, capsys, failure):
    calls = []
    cp = {'pg_dsn': 'postgres://secret', 'host_aliases': {'a': 'b'},
          'inspector_limits': {'max_events': 42}}
    monkeypatch.setattr(cli, '_load_cp_fleet_config', lambda path: (None, cp))

    class Conn:
        async def close(self):
            calls.append('close')

    async def connect(dsn):
        assert dsn == cp['pg_dsn']
        calls.append('connect')
        return Conn()

    async def migrate(self, path):
        assert path == '/custom'
        calls.append('migrate')
        if failure == 'migrate':
            raise RuntimeError('postgres://secret')

    async def indexes(self):
        calls.append('indexes')
        if failure == 'indexes':
            raise RuntimeError('postgres://secret')

    def load(dsn, **policy):
        calls.append('load')
        assert policy == {'host_aliases': {'a': 'b'}, 'inspector_limits': cp['inspector_limits'],
                          'checkpoint_budgets': CheckpointBudgets(100_000, 600_000, 2_000_000), 'lock_wait': 0.0}
        if failure == 'load':
            raise RuntimeError('postgres://secret')
        return {'counts': {'events': 2}, 'recovery_info': {'mode': 'checkpoint'}}

    monkeypatch.setattr(cli, '_cp_connect', connect)
    monkeypatch.setattr(db.Migrator, 'apply', migrate)
    monkeypatch.setattr(projection_refresh, 'refresh_checkpoint', load)
    monkeypatch.setattr(ProjectionStore, 'ensure_indexes', indexes)
    result = cli.main(['control-plane', 'checkpoint', '--fleet-config', '/fleet',
                       '--migrations', '/custom'])
    output = capsys.readouterr().out
    assert 'secret' not in output
    assert result == (1 if failure else 0)
    expected = ['connect', 'migrate']
    if failure != 'migrate':
        expected.append('indexes')
    expected.append('close')
    if failure not in {'migrate', 'indexes'}:
        expected.append('load')
    assert calls == expected
    assert json.loads(output)['ok'] is (not failure)
    if failure:
        assert json.loads(output)['stage'] == {
            'migrate': 'migration', 'indexes': 'index_preparation', 'load': 'materialization'}[failure]


def test_checkpoint_requires_dsn(monkeypatch, capsys):
    monkeypatch.setattr(cli, '_load_cp_fleet_config', lambda path: (None, {}))
    assert cli.main(['control-plane', 'checkpoint', '--fleet-config', '/missing']) == 1
    assert 'requires control_plane.pg_dsn' in capsys.readouterr().out


@pytest.mark.parametrize('once,configured,fail_worker', [
    (False, True, False), (False, True, True), (True, True, False),
    (False, False, False),
])
def test_worker_refresh_lifecycle_preserves_upstream_flows(
        monkeypatch, capsys, once, configured, fail_worker):
    import asyncio
    import threading
    from types import SimpleNamespace

    from herdr_engineering.control_plane import worker

    events = []
    cp = {'pg_dsn': 'postgres://secret', 'host_aliases': {'a': 'b'},
          'inspector_limits': {}} if configured else {}
    main_thread = threading.get_ident()

    class Conn:
        async def close(self):
            events.append('close')

    conn = Conn()

    async def connect(dsn):
        events.append('connect')
        return conn

    async def migrate(self, path):
        events.append('migrate')

    class Refresh:
        def __init__(self, dsn, **kwargs):
            assert dsn == cp['pg_dsn']
            assert kwargs == {'host_aliases': {'a': 'b'}, 'inspector_limits': {},
                              'checkpoint_budgets': CheckpointBudgets(100_000, 600_000, 2_000_000),
                              'heap_release': True}
            events.append('refresh-init')

        def start(self):
            events.append('refresh-start')

        def stop(self):
            # Shutdown does not block the worker's asyncio loop.
            assert threading.get_ident() != main_thread
            events.append('refresh-stop')

    sentinels = {key: object() for key in (
        'inspector_snapshotter', 'fabric_compactor', 'share_housekeeper', 'artifact_reconciler')}

    async def run(conn_arg, **kwargs):
        assert conn_arg is conn
        assert all(kwargs[key] is value for key, value in sentinels.items())
        assert kwargs['worker_id'] == 'host'
        assert kwargs['artifact_job_runner'] is None
        events.append('worker')
        if not once:
            assert isinstance(kwargs['stop'], asyncio.Event)
            await asyncio.sleep(0)
        if fail_worker:
            raise RuntimeError('worker failed')
        return {} if once else None

    monkeypatch.setattr(cli, '_worker_control_plane_section', lambda path: cp)
    monkeypatch.setattr(cli, '_cp_config', lambda path: {'postgres_dsn': 'postgres://secret'})
    monkeypatch.setattr(cli, '_cp_connect', connect)
    monkeypatch.setattr(cli, '_maybe_rustfs_client', lambda cp: None)
    monkeypatch.setattr(db.Migrator, 'apply', migrate)
    monkeypatch.setattr(projection_refresh, 'ProjectionRefresh', Refresh)
    for helper, key in [('_worker_snapshot_writer', 'inspector_snapshotter'),
                        ('_worker_fabric_compactor', 'fabric_compactor'),
                        ('_worker_share_housekeeper', 'share_housekeeper'),
                        ('_worker_artifact_reconciler', 'artifact_reconciler')]:
        monkeypatch.setattr(cli, helper, lambda *args, key=key: sentinels[key])
    monkeypatch.setattr(worker, 'run_worker', run)
    monkeypatch.setattr(worker, 'run_one_pass', run)
    args = SimpleNamespace(fleet_config='/fleet', migrations='/migrations',
                           once=once, interval=.01, host_id='host')
    if fail_worker:
        with pytest.raises(RuntimeError, match='worker failed'):
            cli._cmd_cp_worker(args)
    else:
        assert cli._cmd_cp_worker(args) == 0
    expected = ['connect', 'migrate']
    if configured and not once:
        expected += ['refresh-init', 'refresh-start']
    expected += ['worker']
    if configured and not once:
        expected += ['refresh-stop']
    expected += ['close']
    assert events == expected


@pytest.mark.parametrize('recovery_info', [
    None, {}, {'mode': 'legacy_full_replay', 'reason': 'postgres://secret'},
    {'mode': 'full_replay', 'reason': 'RuntimeError'},
    {'mode': 'checkpoint'},
])
def test_cli_rejects_successful_load_without_publication(monkeypatch, capsys, recovery_info):
    closed = []

    class Conn:
        async def close(self):
            closed.append('migration')

    class Source:
        def __init__(self, **kwargs):
            assert kwargs == {'dsn': 'postgres://secret', 'checkpoint_publish': True,
                              'checkpoint_budgets': CheckpointBudgets(100_000, 600_000, 2_000_000)}
            self.recovery_info = recovery_info

        def close(self):
            closed.append('source')

    async def connect(dsn):
        return Conn()

    async def migrate(self, path):
        pass

    async def indexes(self):
        pass

    monkeypatch.setattr(ProjectionStore, 'ensure_indexes', indexes)
    monkeypatch.setattr(cli, '_load_cp_fleet_config', lambda path: (None, {'pg_dsn': 'postgres://secret'}))
    monkeypatch.setattr(cli, '_cp_connect', connect)
    monkeypatch.setattr(db.Migrator, 'apply', migrate)
    monkeypatch.setattr(projection_refresh.bridge, 'PostgresSource', Source)
    monkeypatch.setattr(projection_refresh.bridge, 'load_into', lambda *args: {'events': 123})
    assert cli.main(['control-plane', 'checkpoint', '--fleet-config', '/fleet']) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {
        'ok': False, 'error': 'CheckpointNotPublished', 'stage': 'materialization'}
    assert 'secret' not in output
    assert closed == ['migration', 'source']


def test_fleet_config_budgets_are_validated_and_defaulted():
    assert cli._checkpoint_budgets_from({}) == CheckpointBudgets(100_000, 600_000, 2_000_000)
    assert cli._checkpoint_budgets_from(
        {'projection_checkpoint': {'max_dirty_rows': 9}}) == CheckpointBudgets(100_000, 9, 2_000_000)
    with pytest.raises(SystemExit, match='fleet config control_plane.projection_checkpoint.max_dirty_rows'):
        cli._checkpoint_budgets_from({'projection_checkpoint': {'max_dirty_rows': 0}})
