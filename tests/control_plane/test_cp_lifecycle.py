"""Control-plane worker reconnect and give-up, and clean SIGTERM/SIGINT shutdown of `control-plane web` and `worker`.

Real Postgres (the registry_pg fixture, restarted mid-test), real asyncpg connections, real sockets and real subprocesses
that are sent signals. No mocks. The only hand-written objects are the small `tick` steps handed to the worker through its
own injection points (they run real SQL on the worker's connection or block on a real socket)."""
# ruff: noqa: F811  (pytest fixtures are imported by name)
import asyncio
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane import models, worker
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture

ROOT = Path(__file__).resolve().parents[2]


class _Probe:
    """Counts passes and touches the database on the connection it is given."""

    def __init__(self):
        self.ticks = 0
        self.conns = []

    async def tick(self, conn):
        self.conns.append(conn)
        await conn.fetchval("SELECT 1")
        self.ticks += 1
        return {}


class _BrokenStep:
    """A step that raises on every pass although Postgres is healthy: real SQL against a table that does not exist."""

    def __init__(self):
        self.calls = 0

    async def tick(self, conn):
        self.calls += 1
        await conn.fetchval("SELECT 1 FROM table_that_does_not_exist")


class _SlowStep:
    """One legitimately long step: the database itself sleeps."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.finished = False

    async def tick(self, conn):
        await conn.fetchval("SELECT pg_sleep($1)", self.seconds)
        self.finished = True
        return {}


class _SilentServer:
    """A real listening socket that accepts and never answers."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = threading.Event()
        self._held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._held.append(conn)
            self.accepted.set()

    def close(self):
        self.sock.close()
        for conn in self._held:
            conn.close()


class _BlockedOnSocket:
    """A step stuck reading a real socket that never answers; with `stubborn` it ignores its first cancellation."""

    def __init__(self, port, stubborn=False):
        self.port = port
        self.stubborn = stubborn
        self.entered = asyncio.Event()
        self.cancels = 0

    async def tick(self, conn):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            while True:
                self.entered.set()
                try:
                    await reader.read(1)
                except asyncio.CancelledError:
                    self.cancels += 1
                    if not self.stubborn or self.cancels > 1:       # the second one (asyncio.run's cleanup) ends it
                        raise
        finally:
            writer.close()


DEAD_DSN = "postgresql://127.0.0.1:1/postgres"      # nothing listens on port 1: a real refused connection


async def _until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def _connector(dsn, connects):
    async def connect():
        conn = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        connects.append(conn)
        return conn
    return connect


def test_worker_reconnects_after_postgres_restart_and_closes_the_current_connection_on_stop(registry_pg, tmp_path):
    probe = _Probe()
    heartbeat = tmp_path / "hb"
    connects = []
    connect = _connector(registry_pg["dsn"], connects)

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
        # run_worker owns what it opened: nothing is left open, including the connection created by the reconnect
        assert all(c.is_closed() for c in connects), [c.is_closed() for c in connects]

    asyncio.run(go())


def test_worker_gives_up_after_consecutive_connection_failures():
    attempts = []

    async def connect():
        attempts.append(1)
        return await asyncpg.connect(DEAD_DSN, timeout=2)

    async def go():
        await worker.run_worker(None, interval=0.01, connect=connect, backoff_base=0.001, backoff_cap=0.002,
                                max_failed_passes=4)

    with pytest.raises(worker.WorkerGaveUp):
        asyncio.run(go())
    assert len(attempts) == 4


def test_backoff_grows_past_the_interval_up_to_the_cap():
    # (interval 30, base 1, cap 120): 30 s while 2**(n-1) is below it, then doubling, then flat at the cap
    delays = [max(30.0, min(worker.BACKOFF_CAP, 1.0 * 2 ** (n - 1))) for n in range(1, 10)]
    assert delays == [30, 30, 30, 30, 30, 32, 64, 120, 120]
    assert sum(delays) == 486                      # about 8 minutes before the 10th failure gives up


def test_a_step_that_raises_every_pass_on_a_healthy_database_does_not_make_the_worker_give_up(registry_pg):
    broken, probe = _BrokenStep(), _Probe()
    connects = []

    async def go():
        stop = asyncio.Event()
        conn = await _connector(registry_pg["dsn"], connects)()
        task = asyncio.create_task(worker.run_worker(
            conn, interval=0.02, stop=stop, connect=_connector(registry_pg["dsn"], connects), backoff_base=0.01,
            backoff_cap=0.02, max_failed_passes=3, annotation_housekeeper=broken, share_housekeeper=probe))
        await _until(lambda: broken.calls >= 12)          # four times the give-up threshold
        assert not task.done()                            # still running, did not raise WorkerGaveUp
        assert probe.ticks >= 10                          # and the other step ran on every pass
        stop.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert len(connects) == 1                             # no needless reconnects either


def test_run_one_pass_isolates_steps_only_when_asked(registry_pg):
    async def go(isolate):
        conn = await asyncpg.connect(registry_pg["dsn"])
        broken, probe = _BrokenStep(), _Probe()
        try:
            return probe, await worker.run_one_pass(conn, annotation_housekeeper=broken, share_housekeeper=probe,
                                                    isolate_steps=isolate)
        finally:
            await conn.close()

    probe, summary = asyncio.run(go(True))
    assert probe.ticks == 1 and "annotation_housekeeper" in summary["step_errors"]
    with pytest.raises(asyncpg.UndefinedTableError):
        asyncio.run(go(False))


def test_a_closed_connection_aborts_the_pass_even_with_isolated_steps(registry_pg):
    async def go():
        conn = await asyncpg.connect(registry_pg["dsn"])
        await conn.close()
        await worker.run_one_pass(conn, share_housekeeper=_Probe(), isolate_steps=True)

    with pytest.raises(asyncpg.InterfaceError):
        asyncio.run(go())


def test_worker_touches_heartbeat_while_the_database_answers_and_not_while_it_is_unreachable(registry_pg, tmp_path):
    ok_file, bad_file = tmp_path / "ok", tmp_path / "bad"

    async def healthy():
        conn = await asyncpg.connect(registry_pg["dsn"])
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(
            conn, interval=0.01, stop=stop, connect=_connector(registry_pg["dsn"], []), heartbeat=ok_file,
            service_store=models.ServiceStore(conn)))     # a real store on the real connection
        await _until(ok_file.exists)
        stop.set()
        await task

    async def unreachable():
        async def connect():
            return await asyncpg.connect(DEAD_DSN, timeout=2)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(None, interval=0.01, stop=stop, connect=connect, backoff_base=0.01,
                                                     backoff_cap=0.02, heartbeat=bad_file))
        await asyncio.sleep(0.5)
        stop.set()
        await task

    asyncio.run(healthy())
    asyncio.run(unreachable())
    assert ok_file.exists() and not bad_file.exists()


def test_heartbeat_is_touched_during_one_long_step_not_only_after_the_pass(registry_pg, tmp_path):
    heartbeat = tmp_path / "hb"
    slow = _SlowStep(2.0)

    async def go():
        conn = await asyncpg.connect(registry_pg["dsn"])
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_worker(
            conn, interval=60, stop=stop, connect=_connector(registry_pg["dsn"], []), heartbeat=heartbeat,
            share_housekeeper=slow))
        await _until(heartbeat.exists, timeout=1.5)       # well before the 2 s step ends
        assert not slow.finished
        stop.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())


def test_stop_abandons_a_pass_stuck_on_a_socket_after_the_grace_period(registry_pg):
    server = _SilentServer()

    async def go():
        stop = asyncio.Event()
        step = _BlockedOnSocket(server.port)
        conn = await asyncpg.connect(registry_pg["dsn"])
        task = asyncio.create_task(worker.run_worker(conn, interval=0.01, stop=stop, pass_grace=0.2,
                                                     share_housekeeper=step))
        await step.entered.wait()
        stop.set()
        await asyncio.wait_for(task, 5)
        await conn.close()

    try:
        asyncio.run(go())
    finally:
        server.close()


def test_stop_does_not_wait_forever_for_a_pass_that_ignores_cancellation(registry_pg):
    server = _SilentServer()

    async def go():
        stop = asyncio.Event()
        step = _BlockedOnSocket(server.port, stubborn=True)
        conn = await asyncpg.connect(registry_pg["dsn"])
        task = asyncio.create_task(worker.run_worker(conn, interval=0.01, stop=stop, pass_grace=0.2, unwind=0.3,
                                                     share_housekeeper=step))
        await step.entered.wait()
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, 5)
        elapsed = time.monotonic() - started
        await conn.close()
        return elapsed

    try:
        elapsed = asyncio.run(go())
    finally:
        server.close()
    assert elapsed < 3                                    # grace 0.2 + unwind 0.3, not forever


class _Proc:
    """A real subprocess with stdout read by a thread (so deadlines fire) and stderr written to a file (never blocks)."""

    def __init__(self, tmp_path, *argv, name="proc"):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
        self.err_path = tmp_path / f"{name}.stderr"
        self._err = open(self.err_path, "w")
        self.popen = subprocess.Popen(argv if argv[0] == sys.executable else [sys.executable, "-m", "herdr_engineering.cli", *argv],
                                      cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=self._err, text=True)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.popen.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def stderr(self):
        return self.err_path.read_text()[-2000:]

    def wait_for_line(self, needle, timeout=60.0):
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                raise AssertionError(f"exited early rc={self.popen.wait()}: {self.stderr()}")
            if needle in line:
                return
        raise AssertionError(f"timed out waiting for {needle!r}: {self.stderr()}")

    def wait_for(self, predicate, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.popen.poll() is not None:
                raise AssertionError(f"exited early rc={self.popen.returncode}: {self.stderr()}")
            time.sleep(0.1)
        raise AssertionError(f"timed out: {self.stderr()}")

    def stop_with(self, signum, timeout=15):
        self.popen.send_signal(signum)
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            raise AssertionError(f"did not exit within {timeout} s of signal {signum}: {self.stderr()}") from None

    def cleanup(self):
        if self.popen.poll() is None:
            self.popen.kill()
        self.popen.wait()
        self._err.close()


def _fleet(tmp_path, dsn):
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(json.dumps({"version": 1, "hosts": {}, "sections": {
        "control_plane": {"roles": ["control-plane"], "values": {"pg_dsn": dsn}}}}))
    fleet.chmod(0o600)
    return fleet


MIGRATIONS = str(ROOT / "deploy/control-plane/migrations")


def test_control_plane_web_exits_zero_on_sigterm(tmp_path):
    proc = _Proc(tmp_path, "control-plane", "web", "--host", "127.0.0.1", "--port", "0",
                 "--fleet-config", str(tmp_path / "absent.yaml"))
    try:
        proc.wait_for_line('"serving"')
        assert proc.stop_with(signal.SIGTERM) == 0, proc.stderr()
    finally:
        proc.cleanup()


def test_wait_for_line_deadline_fires_on_a_silent_process(tmp_path):
    proc = _Proc(tmp_path, sys.executable, "-c", "import time; time.sleep(30)")
    try:
        started = time.monotonic()
        with pytest.raises(AssertionError, match="timed out"):
            proc.wait_for_line("never", timeout=0.5)
        assert time.monotonic() - started < 5
    finally:
        proc.cleanup()


@pytest.mark.parametrize("signum,expected", [(signal.SIGTERM, 0), (signal.SIGINT, 130)])
def test_control_plane_worker_stops_cleanly_on_a_signal(registry_pg, tmp_path, signum, expected):
    heartbeat = tmp_path / "heartbeat"
    proc = _Proc(tmp_path, "control-plane", "worker", "--fleet-config", str(_fleet(tmp_path, registry_pg["dsn"])),
                 "--interval", "0.2", "--heartbeat-file", str(heartbeat), "--migrations", MIGRATIONS)
    try:
        proc.wait_for(heartbeat.exists)
        assert proc.stop_with(signum) == expected, proc.stderr()
    finally:
        proc.cleanup()


@pytest.mark.parametrize("once", [False, True])
@pytest.mark.parametrize("signum,expected", [(signal.SIGTERM, 0), (signal.SIGINT, 130)])
def test_a_signal_during_startup_exits_cleanly(tmp_path, signum, expected, once):
    server = _SilentServer()          # the migration connection opens and then waits forever for Postgres to answer
    argv = ["control-plane", "worker", "--fleet-config", str(_fleet(tmp_path, f"postgresql://127.0.0.1:{server.port}/x")),
            "--migrations", MIGRATIONS, "--connect-timeout", "60"]
    proc = _Proc(tmp_path, *argv, *(["--once"] if once else []))
    try:
        assert server.accepted.wait(60), proc.stderr()
        assert proc.stop_with(signum, timeout=10) == expected, proc.stderr()
    finally:
        proc.cleanup()
        server.close()


def test_a_second_sigint_kills_a_teardown_that_is_stuck(tmp_path):
    script = (
        "import asyncio, sys\n"
        "from herdr_engineering.cli import _StopSignals\n"
        "async def main():\n"
        "    stop = asyncio.Event()\n"
        "    signals = _StopSignals(); signals.install(asyncio.get_running_loop(), stop)\n"
        "    print('ready', flush=True)\n"
        "    await stop.wait()\n"
        "    print('stopping', flush=True)\n"
        "    await asyncio.sleep(3600)          # a teardown that never finishes\n"
        "asyncio.run(main())\n")
    proc = _Proc(tmp_path, sys.executable, "-c", script)
    try:
        proc.wait_for_line("ready")
        proc.popen.send_signal(signal.SIGINT)
        proc.wait_for_line("stopping")
        time.sleep(0.3)
        assert proc.popen.poll() is None                      # the first Ctrl-C asked for a clean stop; it is stuck
        assert proc.stop_with(signal.SIGINT, timeout=10) == -signal.SIGINT   # the second one kills it
    finally:
        proc.cleanup()
