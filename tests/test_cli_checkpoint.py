import json

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane import db, projection_refresh


@pytest.mark.parametrize('failure', [None, 'migrate', 'load'])
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

    def load(dsn, **policy):
        calls.append('load')
        assert policy == {'host_aliases': {'a': 'b'}, 'inspector_limits': cp['inspector_limits']}
        if failure == 'load':
            raise RuntimeError('postgres://secret')
        return {'counts': {'events': 2}, 'recovery_info': {'mode': 'checkpoint'}}

    monkeypatch.setattr(cli, '_cp_connect', connect)
    monkeypatch.setattr(db.Migrator, 'apply', migrate)
    monkeypatch.setattr(projection_refresh, 'refresh_checkpoint', load)
    result = cli.main(['control-plane', 'checkpoint', '--fleet-config', '/fleet',
                       '--migrations', '/custom'])
    output = capsys.readouterr().out
    assert 'secret' not in output
    assert result == (1 if failure else 0)
    assert calls == ['connect', 'migrate', 'close'] + ([] if failure == 'migrate' else ['load'])
    assert json.loads(output)['ok'] is (not failure)


def test_checkpoint_requires_dsn(monkeypatch, capsys):
    monkeypatch.setattr(cli, '_load_cp_fleet_config', lambda path: (None, {}))
    assert cli.main(['control-plane', 'checkpoint', '--fleet-config', '/missing']) == 1
    assert 'requires control_plane.pg_dsn' in capsys.readouterr().out
