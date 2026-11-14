"""stop_postgres: a throwaway cluster's teardown must not fail a test whose assertions all passed (CI pipelines 1428/1444/1453/1454).

Real Postgres, real signals. A backend that cannot answer the fast-shutdown SIGTERM (SIGSTOPped here, standing in for a node whose
disk or CPU is starved) holds a `fast` stop open past its wait; the helper must then fall back to `immediate` and leave the
cluster down instead of raising CalledProcessError and leaving a half-shut-down postmaster for the module's later tests."""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.control_plane.pg_stop import FAST_TEST_OPTIONS, stop_postgres
from tests.e2e.pg import find_bins


@pytest.fixture
def cluster(tmp_path):
    bins = find_bins({})
    if bins is None or os.getuid() == 0:
        pytest.skip("PostgreSQL server binaries unavailable")
    data, sock = tmp_path / "data", Path(tmp_path, "s")
    sock.mkdir()
    subprocess.run([str(bins / "initdb"), "-D", str(data), "-A", "trust", "-U", "t", "--no-sync"], check=True, capture_output=True)
    subprocess.run([str(bins / "pg_ctl"), "-D", str(data), "-w", "-l", str(tmp_path / "log"), "-o",
                    f"-c listen_addresses='' -k {sock} -c dynamic_shared_memory_type=mmap {FAST_TEST_OPTIONS}", "start"],
                   check=True, capture_output=True)
    yield bins, data, sock
    subprocess.run([str(bins / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)


def running(bins, data):
    return subprocess.run([str(bins / "pg_ctl"), "-D", str(data), "status"], capture_output=True).returncode == 0


def test_a_normal_stop_leaves_the_cluster_down(cluster):
    bins, data, _ = cluster
    stop_postgres(bins / "pg_ctl", data)
    assert not running(bins, data)


def test_stopping_a_cluster_that_is_already_down_is_not_an_error(cluster):
    bins, data, _ = cluster
    stop_postgres(bins / "pg_ctl", data)
    stop_postgres(bins / "pg_ctl", data)
    assert not running(bins, data)


def test_a_fast_stop_that_cannot_finish_falls_back_to_immediate(cluster):
    bins, data, sock = cluster
    # A live session that never exits: a second psql that sleeps inside the server, then freeze its backend.
    sleeper = subprocess.Popen([str(bins / "psql"), "-h", str(sock), "-U", "t", "-d", "postgres", "-qAt", "-c",
                                "select pg_backend_pid(), pg_sleep(600)"], stdout=subprocess.PIPE, text=True)
    try:
        backend = None
        deadline = time.monotonic() + 15
        while backend is None and time.monotonic() < deadline:
            out = subprocess.run([str(bins / "psql"), "-h", str(sock), "-U", "t", "-d", "postgres", "-qAt", "-c",
                                  "select pid from pg_stat_activity where query like '%pg_sleep(600)%' and pid <> pg_backend_pid()"],
                                 capture_output=True, text=True).stdout.split()
            backend = int(out[0]) if out else None
            time.sleep(0.1)
        assert backend
        os.kill(backend, signal.SIGSTOP)  # cannot act on the shutdown signal: the fast stop stalls
        started = time.monotonic()
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.run([str(bins / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "-t", "2", "stop"], check=True, capture_output=True)
        assert time.monotonic() - started < 30  # the premise: plain fast stop really does fail here
        stop_postgres(bins / "pg_ctl", data, timeout=2)
        assert not running(bins, data)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_the_test_options_skip_disk_syncs(cluster):
    assert "fsync=off" in FAST_TEST_OPTIONS and "synchronous_commit=off" in FAST_TEST_OPTIONS
