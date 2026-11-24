"""Control-plane worker reconnect and give-up, and clean SIGTERM shutdown of `control-plane web` and `worker`.

Real Postgres (the registry_pg fixture, restarted mid-test) and real subprocesses that are sent SIGTERM; no mocks."""
# ruff: noqa: F811  (pytest fixtures are imported by name)
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane import worker
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture

ROOT = Path(__file__).resolve().parents[2]


class _Probe:
    """A real injected dependency: ticks once per pass and touches the database on the connection it is given."""

    def __init__(self):
        self.ticks = 0
        self.conns = []

    async def tick(self, conn):
        self.conns.append(conn)
        await conn.fetchval("SELECT 1")
        self.ticks += 1
        return {}


async def _until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def test_worker_reconnects_after_postgres_restart(registry_pg, tmp_path):
    probe = _Probe()
    heartbeat = tmp_path / "hb"
    connects = []

    async def connect():
        conn = await asyncpg.connect(registry_pg["dsn"], timeout=5, command_timeout=10)
        connects.append(conn)
        return conn

    async def go():
        stop = asyncio.Event()
        first = await connect()
        task = asyncio.create_task(worker.run_worker(
            first, interval=0.05, stop=stop, connect=connect, backoff_base=0.05, backoff_cap=0.2,
            max_failed_passes=100, heartbeat=heartbeat, share_housekeeper=probe))
        await _until(lambda: probe.ticks >= 2)
        await asyncio.to_thread(registry_pg["restart"])
        before = probe.ticks
        await _until(lambda: probe.ticks >= before + 3 and probe.conns[-1] is not first)
        stop.set()
        await asyncio.wait_for(task, 10)
        assert len(connects) >= 2
        assert heartbeat.exists()

    asyncio.run(go())


def test_worker_gives_up_after_consecutive_failed_passes():
    attempts = []

    async def connect():
        attempts.append(1)
        raise OSError("postgres unreachable")

    async def go():
        await worker.run_worker(None, interval=0.01, connect=connect, backoff_base=0.001, backoff_cap=0.002,
                                max_failed_passes=4)

    with pytest.raises(worker.WorkerGaveUp):
        asyncio.run(go())
    assert len(attempts) == 4


def test_worker_touches_heartbeat_only_on_successful_passes(tmp_path):
    class _Bad:
        async def list(self, **kwargs):
            raise RuntimeError("boom")

    class _Fine:
        async def list(self, **kwargs):
            return []

    ok_file, bad_file = tmp_path / "ok", tmp_path / "bad"

    async def go(path, store):
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(None, interval=0.01, stop=stop, heartbeat=path,
                                                     service_store=store))
        await asyncio.sleep(0.2)
        stop.set()
        await task

    asyncio.run(go(ok_file, _Fine()))
    asyncio.run(go(bad_file, _Bad()))
    assert ok_file.exists() and not bad_file.exists()


def test_stop_abandons_a_stuck_pass_after_the_grace_period():
    class _Stuck:
        async def list(self, **kwargs):
            await asyncio.sleep(3600)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(None, interval=0.01, stop=stop, pass_grace=0.2,
                                                     service_store=_Stuck()))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())


def _spawn(*argv):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
    return subprocess.Popen([sys.executable, "-m", "herdr_engineering.cli", *argv], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for(predicate, proc, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if proc.poll() is not None:
            raise AssertionError(f"exited early rc={proc.returncode}: {proc.stderr.read()[-2000:]}")
        time.sleep(0.1)
    raise AssertionError("timed out waiting for the process")


def _wait_for_line(proc, needle, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if needle in line:
            return
        if not line and proc.poll() is not None:
            raise AssertionError(f"exited early rc={proc.returncode}: {proc.stderr.read()[-2000:]}")
    raise AssertionError("timed out waiting for the process")


def _assert_sigterm_exits_cleanly(proc):
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        rc = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("did not exit within 15 s of SIGTERM") from None
    assert rc == 0, proc.stderr.read()[-2000:]
    assert time.monotonic() - started < 15


def test_control_plane_web_exits_zero_on_sigterm(tmp_path):
    proc = _spawn("control-plane", "web", "--host", "127.0.0.1", "--port", "0",
                  "--fleet-config", str(tmp_path / "absent.yaml"))
    try:
        _wait_for_line(proc, '"serving"')
        _assert_sigterm_exits_cleanly(proc)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_control_plane_worker_exits_zero_on_sigterm(registry_pg, tmp_path):
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(json.dumps({"version": 1, "hosts": {}, "sections": {"control_plane": {"roles": ["control-plane"], "values": {"pg_dsn": registry_pg["dsn"]}}}}))
    fleet.chmod(0o600)
    heartbeat = tmp_path / "heartbeat"
    proc = _spawn("control-plane", "worker", "--fleet-config", str(fleet), "--interval", "0.2",
                  "--heartbeat-file", str(heartbeat), "--migrations", str(ROOT / "deploy/control-plane/migrations"))
    try:
        _wait_for(heartbeat.exists, proc)
        _assert_sigterm_exits_cleanly(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
