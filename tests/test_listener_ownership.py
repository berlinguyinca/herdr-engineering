"""Listener ownership uses native pane roots and live OS process ancestry."""
from types import SimpleNamespace

import herdr_engineering.fabric_agent as fa


def resolver(monkeypatch):
    roots = [{'shell_pid': 100, 'shell_start': 'birth-100', 'session_id': 'herdr:host:default:w1%3Ap1',
              'host_id': 'host', 'herdr_session': 'default', 'pane_id': 'w1:p1', 'terminal_id': 'term'}]
    result = fa.ListenerOwnership('host')
    result._roots = roots
    result._expires = float('inf')
    states = {300: {'parent_pid': 200, 'start': 'birth-300'},
              200: {'parent_pid': 100, 'start': 'birth-200'},
              100: {'parent_pid': 1, 'start': 'birth-100'},
              400: {'parent_pid': 1, 'start': 'birth-400'}}
    monkeypatch.setattr(fa, '_process_identity', lambda pid, **kwargs: states.get(pid))
    monkeypatch.setattr(fa, '_system_unit', lambda pid: None)
    return result, states


def test_descendant_listener_is_linked_to_exact_native_pane(monkeypatch):
    result, _ = resolver(monkeypatch)
    proof = result.classify(300)
    assert proof['kind'] == 'herdr_process_ancestry'
    assert proof['ancestor_pids'] == [300, 200, 100]
    assert proof['session_id'] == 'herdr:host:default:w1%3Ap1'
    assert proof['listener_start'] == 'birth-300'


def test_unrelated_or_detached_process_never_inherits_native_ownership(monkeypatch):
    result, _ = resolver(monkeypatch)
    assert result.classify(400) is None


def test_recycled_native_shell_pid_invalidates_cached_proof(monkeypatch):
    result, states = resolver(monkeypatch)
    states[100]['start'] = 'new-process'
    assert result.classify(300) is None


def test_system_unit_proof_is_separate_from_herdr(monkeypatch):
    result, _ = resolver(monkeypatch)
    monkeypatch.setattr(fa, '_system_unit', lambda pid: 'database.service')
    assert result.classify(400)['kind'] == 'system_unit'
    assert result.classify(300)['kind'] == 'herdr_process_ancestry'


def test_native_snapshot_requires_matching_pane_and_positive_shell_pid(monkeypatch):
    monkeypatch.setattr(fa.shutil, 'which', lambda command: '/usr/bin/herdr')
    def run(args, **kwargs):
        assert 0 < kwargs['timeout'] <= 1
        if args[-3:] == ['session', 'list', '--json']:
            value = {'sessions': [{'name': 'default', 'running': True}]}
        elif args[-2:] == ['api', 'snapshot']:
            value = {'result': {'snapshot': {'panes': [{'pane_id': 'w1:p1', 'terminal_id': 'term'}]}}}
        else:
            assert args[-4:] == ['pane', 'process-info', '--pane', 'w1:p1']
            value = {'result': {'process_info': {'pane_id': 'w1:p1', 'shell_pid': 100}}}
        import json
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))
    monkeypatch.setattr(fa.subprocess, 'run', run)
    monkeypatch.setattr(fa, '_process_identity', lambda pid, **kwargs: {'parent_pid': 1, 'start': 'birth'})
    result = fa.ListenerOwnership('host')
    proof = result.classify(100, timeout=1)
    assert proof['session_id'] == 'herdr:host:default:w1%3Ap1'
    assert proof['shell_pid'] == 100


def test_reparented_intermediate_ancestor_invalidates_proof(monkeypatch):
    result, states = resolver(monkeypatch)
    calls = {}
    def identity(pid, **kwargs):
        calls[pid] = calls.get(pid, 0) + 1
        value = states.get(pid)
        if value is None:
            return None
        if pid == 200 and calls[pid] > 1:
            return dict(value, parent_pid=1)
        return dict(value)
    monkeypatch.setattr(fa, '_process_identity', identity)
    assert result.classify(300) is None


def test_linux_process_identity_changes_across_host_boots(monkeypatch):
    import io
    monkeypatch.setattr(fa.os, 'uname', lambda: SimpleNamespace(sysname='Linux'))
    stat = '300 (app) ' + ' '.join(['S', '1'] + ['0'] * 17 + ['1000'])
    monkeypatch.setattr('builtins.open', lambda *args, **kwargs: io.StringIO(stat))
    monkeypatch.setattr(fa, '_linux_boot_identity', lambda: 'old-boot')
    old = fa._process_identity(300)
    monkeypatch.setattr(fa, '_linux_boot_identity', lambda: 'new-boot')
    new = fa._process_identity(300)
    assert old['start'] != new['start']
