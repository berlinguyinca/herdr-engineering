"""Maintenance must never overlap a coordinated build, including slot bypass."""
import fcntl
import os
import subprocess
import sys

import pytest

from herdr_engineering.build_coord import BuildGate


@pytest.mark.parametrize('bypass', [False, True])
def test_build_holds_maintenance_lock_across_exec(tmp_path, bypass):
    state = tmp_path / 'gate'
    state.mkdir()
    held = None
    if bypass:
        held, _ = BuildGate(state_dir=state, slots=1)._try_acquire(0, ['hold'])
    ready = tmp_path / 'ready'
    script = (
        'from herdr_engineering.build_coord import run_gated; '
        f'run_gated({[sys.executable, "-c", "import pathlib,time; pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(1)"]!r}, '
        f'state_dir={str(state)!r}, slots=1, max_load=0, '
        f'wait_seconds=0.1, bypass_on_timeout={bypass!r})'
    )
    proc = subprocess.Popen([sys.executable, '-c', script])
    try:
        import time
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        with (state / 'build-maintenance.lock').open('a') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert proc.wait(timeout=5) == 0
        with (state / 'build-maintenance.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        proc.wait(timeout=5)
        if held is not None:
            os.close(held)


def test_cleanup_cli_available():
    from herdr_engineering.cli import _build_parser
    args = _build_parser().parse_args(['build-cleanup', 'run', '--dry-run'])
    assert args.dry_run


def test_server_detection_ignores_clients_and_other_users(tmp_path):
    from herdr_engineering.build_maintenance import server_running
    process = tmp_path / '42'
    process.mkdir()
    (process / 'cmdline').write_bytes(b'/usr/bin/herdr\0remote-client-bridge\0')
    assert not server_running(tmp_path)
    (process / 'cmdline').write_bytes(b'/usr/bin/herdr\0server\0')
    assert server_running(tmp_path)
    (process / 'cmdline').write_bytes(b'/usr/bin/herdr\0--session\0abc\0server\0')
    assert server_running(tmp_path)


def test_watch_sleeps_without_server_and_stops(tmp_path):
    import threading

    from herdr_engineering.build_maintenance import watch
    calls = []
    stop = threading.Event()
    def wait(interval):
        stop.set()
    watch(lambda: calls.append(True), interval=1, stop=stop,
          is_running=lambda: False, wait=wait)
    assert calls == []


def test_watch_runs_with_server_and_survives_failure():
    import threading

    from herdr_engineering.build_maintenance import watch
    stop = threading.Event()
    calls = []
    def cleanup():
        calls.append(True)
        raise OSError('scan failed')
    watch(cleanup, interval=1, stop=stop, is_running=lambda: True,
          wait=lambda interval: stop.set())
    assert calls == [True]


def test_manual_run_reports_error_exit_for_invalid_root(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from herdr_engineering.build_maintenance import command
    monkeypatch.setenv('HERDR_ENGINEERING_STATE', str(tmp_path / 'state'))
    args = Namespace(cleanup_command='run', root=[str(tmp_path / 'missing')], dry_run=True)
    assert command(args) == 1
    assert 'error' in capsys.readouterr().out
