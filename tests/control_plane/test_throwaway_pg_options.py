"""The throwaway cluster must not depend on /dev/shm (CI containers clean or shrink it under a running postmaster)."""
from pathlib import Path

from tests.e2e.pg import ThrowawayPostgres


def test_throwaway_postgres_keeps_dynamic_shared_memory_out_of_dev_shm(tmp_path):
    options = ThrowawayPostgres(Path("/nonexistent"), tmp_path)._options()
    assert "dynamic_shared_memory_type=mmap" in options and "listen_addresses=''" in options


def test_every_private_postgres_launcher_keeps_dynamic_shared_memory_out_of_dev_shm():
    """Eight test helpers start their own socket-only cluster. A launcher without the option puts parallel-query segments
    in /dev/shm, which CI containers clean under a running postmaster ('could not open shared memory segment')."""
    root = Path(__file__).resolve().parents[1]
    launchers = [p for p in root.rglob("*.py") if p.name != Path(__file__).name and "listen_addresses" in p.read_text()
                 and p.parts[-2] != "e2e"]
    assert len(launchers) >= 8, launchers
    missing = [str(p.relative_to(root)) for p in launchers if "dynamic_shared_memory_type=mmap" not in p.read_text()]
    assert not missing, f"private Postgres launchers without dynamic_shared_memory_type=mmap: {missing}"


def test_every_private_postgres_launcher_skips_disk_syncs_and_stops_through_the_shared_helper():
    """CI pipelines 1428/1444/1453/1454: `pg_ctl -m fast -w stop` (60 s wait) returned 1 on the shared node and failed the test that
    owned the fixture. Launchers use FAST_TEST_OPTIONS (fsync=off ...) and tests.control_plane.pg_stop.stop_postgres instead."""
    root = Path(__file__).resolve().parents[1]
    launchers = [p for p in root.rglob("*.py") if p.name not in {Path(__file__).name, "pg_stop.py", "test_pg_stop.py"}
                 and "listen_addresses" in p.read_text() and p.parts[-2] not in {"playwright"}]
    assert len(launchers) >= 8, launchers
    unsynced = [str(p.relative_to(root)) for p in launchers if "FAST_TEST_OPTIONS" not in (t := p.read_text()) and "fsync=off" not in t]
    raw_stop = [str(p.relative_to(root)) for p in launchers if '"-m", "fast"' in p.read_text() or "'-m', 'fast'" in p.read_text()]
    assert not unsynced, f"launchers that still fsync: {unsynced}"
    assert not raw_stop, f"launchers that stop with a bare `pg_ctl -m fast`: {raw_stop}"
