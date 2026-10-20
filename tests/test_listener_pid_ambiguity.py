"""One shared port is not proof that every bind belongs to the first process."""
from types import SimpleNamespace

import pytest

import herdr_engineering.fabric_agent as fa


def scan_linux(monkeypatch, lines):
    monkeypatch.setattr(fa.shutil, 'which', lambda _: '/usr/bin/ss')
    monkeypatch.setattr(fa.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='\n'.join(lines)))
    return fa._scan_linux()[8080]


def line(address, processes=''):
    return f'LISTEN 0 128 {address}:8080 0.0.0.0:* {processes}'


@pytest.mark.parametrize('lines', [
    [line('127.0.0.1','users:(("node",pid=100,fd=4))'), line('10.0.0.1','users:(("python",pid=200,fd=5))')],
    [line('0.0.0.0','users:(("node",pid=100,fd=4),("node",pid=200,fd=5))')],
    [line('127.0.0.1','users:(("node",pid=100,fd=4))'), line('10.0.0.1')],
    [line('10.0.0.1'), line('127.0.0.1','users:(("node",pid=100,fd=4))')],
])
def test_linux_conflicting_or_unknown_owner_is_ambiguous(monkeypatch, lines):
    info = scan_linux(monkeypatch, lines)
    assert info.get('pid') is None
    assert info['pid_ambiguous'] is True


def test_linux_duplicate_binds_same_pid_remain_identifiable(monkeypatch):
    info = scan_linux(monkeypatch, [line('0.0.0.0','users:(("node",pid=100,fd=4))'),
                                   line('[::]','users:(("node",pid=100,fd=5))')])
    assert info['pid'] == 100
    assert not info.get('pid_ambiguous')


@pytest.mark.parametrize('second_pid', ['200', 'unknown'])
def test_macos_conflicting_or_missing_pid_is_ambiguous(monkeypatch, second_pid):
    monkeypatch.setattr(fa.shutil, 'which', lambda _: '/usr/sbin/lsof')
    text = '\n'.join(['COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME',
                     'node 100 user 4u IPv4 123 0t0 TCP 127.0.0.1:8080',
                     f'node {second_pid} user 5u IPv6 124 0t0 TCP [::1]:8080'])
    monkeypatch.setattr(fa.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=text))
    info = fa._scan_macos()[8080]
    assert info.get('pid') is None and info['pid_ambiguous'] is True


def test_successful_empty_linux_scan_is_complete_but_failure_is_not(monkeypatch):
    monkeypatch.setattr(fa.shutil, 'which', lambda _: '/usr/bin/ss')
    monkeypatch.setattr(fa.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='', returncode=0))
    assert fa._scan_linux() == {}
    assert fa._scan_linux().complete is True
    monkeypatch.setattr(fa.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='', returncode=1))
    assert fa._scan_linux().complete is False
    monkeypatch.setattr(fa.shutil, 'which', lambda _: None)
    assert fa._scan_linux().complete is False


def test_malformed_listener_row_makes_scan_incomplete(monkeypatch):
    monkeypatch.setattr(fa.shutil, 'which', lambda _: '/usr/bin/ss')
    monkeypatch.setattr(fa.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='truncated listener', returncode=0))
    assert fa._scan_linux().complete is False


def test_unknown_pid_alongside_known_pid_on_same_linux_line_is_ambiguous(monkeypatch):
    info = scan_linux(monkeypatch, [line('0.0.0.0', 'users:(("node",pid=100,fd=4),("hidden",fd=5))')])
    assert info.get('pid') is None and info['pid_ambiguous'] is True
