"""Shared teardown and speed options for the throwaway Postgres clusters the tests start.

Why this exists: `pg_ctl -m fast -w stop` waits 60 s by default and exits 1 when the shutdown is not done by then. On the shared CI
node (several pipelines, one disk) the shutdown checkpoint can take that long, and the failure then landed on whichever test owned
the fixture: CalledProcessError at teardown, or 'the database system is shutting down' for the module's later tests (pipelines 1428,
1444, 1453, 1454). The clusters are throwaway, so they skip every disk sync, and a stop that still stalls ends in an immediate stop."""
from __future__ import annotations

import subprocess
from pathlib import Path

# Nothing in a test cluster needs to survive power loss, so no fsync and no full-page writes. synchronous_commit stays on on purpose: with
# it off, an immediate stop (the fallback below) loses the last few hundred ms of commits; with fsync=off alone a committed row is in the
# WAL in the OS cache and survives any postmaster stop.
FAST_TEST_OPTIONS = "-c fsync=off -c full_page_writes=off"

_FAST_WAIT_SECONDS = 300
_IMMEDIATE_WAIT_SECONDS = 120


def _ctl(pg_ctl: str | Path, data: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(pg_ctl), "-D", str(data), *args], capture_output=True, text=True)


def postgres_running(pg_ctl: str | Path, data: str | Path) -> bool:
    return _ctl(pg_ctl, data, "status").returncode == 0


def stop_postgres(pg_ctl: str | Path, data: str | Path, *, timeout: int = _FAST_WAIT_SECONDS, fallback: bool = True,
                  immediate: bool = False) -> None:
    """Stop the cluster at `data`. Fast shutdown first, waiting `timeout` seconds; if that fails and the postmaster is still up,
    an immediate stop (a throwaway cluster needs no clean shutdown). Raises RuntimeError only when the cluster is still running
    afterwards. An already-stopped cluster is fine. With `immediate=True` the clean stop is skipped. With `fallback=False` (a restart that must keep its clean shutdown) a failed
    fast stop raises instead of crashing the cluster."""
    if immediate:                                   # a teardown that never needed the clean shutdown
        _ctl(pg_ctl, data, "-m", "immediate", "-w", "-t", str(_IMMEDIATE_WAIT_SECONDS), "stop")
        if postgres_running(pg_ctl, data):
            raise RuntimeError("pg_ctl could not stop the cluster (immediate)")
        return
    fast = _ctl(pg_ctl, data, "-m", "fast", "-w", "-t", str(timeout), "stop")
    if fast.returncode == 0 or not postgres_running(pg_ctl, data):
        return
    if not fallback:
        raise RuntimeError(f"pg_ctl fast stop failed ({fast.returncode}): {fast.stderr.strip()}")
    immediate = _ctl(pg_ctl, data, "-m", "immediate", "-w", "-t", str(_IMMEDIATE_WAIT_SECONDS), "stop")
    if postgres_running(pg_ctl, data):
        raise RuntimeError(f"pg_ctl could not stop the cluster: fast: {fast.stderr.strip()}; immediate: {immediate.stderr.strip()}")
