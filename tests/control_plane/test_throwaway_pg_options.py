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


# Launchers that already stop with `-m immediate` and fsync off and are not converted to the helper.
_IMMEDIATE_ONLY = {"_private_pg.py", "test_project_intelligence_migration.py"}


def launcher_problems(name: str, text: str) -> list[str]:
    """What is wrong with a test file that starts its own private Postgres (empty list = fine)."""
    if name in _IMMEDIATE_ONLY:
        ok = "fsync=off" in text and '"-m", "immediate"' in text
        return [] if ok else [f"{name}: an allowlisted launcher must keep fsync=off and `-m immediate`"]
    problems = []
    if "FAST_TEST_OPTIONS" not in text:
        problems.append(f"{name}: does not start with FAST_TEST_OPTIONS (it would fsync)")
    if "stop_postgres" not in text:
        problems.append(f"{name}: does not stop through tests.control_plane.pg_stop.stop_postgres")
    return problems


def _launchers() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    skip = {Path(__file__).name, "pg_stop.py", "test_pg_stop.py"}
    return [p for p in root.rglob("*.py") if p.name not in skip and "listen_addresses" in p.read_text() and p.parts[-2] != "playwright"]


def test_every_private_postgres_launcher_skips_disk_syncs_and_stops_through_the_shared_helper():
    """CI pipelines 1428/1444/1453/1454: `pg_ctl -m fast -w stop` (60 s wait) returned 1 on the shared node and failed the test that
    owned the fixture."""
    launchers = _launchers()
    assert len(launchers) >= 8, launchers
    problems = [m for p in launchers for m in launcher_problems(p.name, p.read_text())]
    assert not problems, problems


def test_the_guard_rejects_launchers_that_stop_with_a_bare_pg_ctl():
    options = "FAST_TEST_OPTIONS"
    bare = f'subprocess.run([pg_ctl, "-D", data, "-w", "stop"], check=True)  # {options}'
    shell = f'os.system(f"pg_ctl -D {{data}} -m fast -w stop")  # {options}'
    assert launcher_problems("bare_launcher.py", bare)
    assert launcher_problems("shell_launcher.py", shell)
    assert not launcher_problems("good.py", f"stop_postgres(pg_ctl, data)  # {options}")
    assert launcher_problems("unsynced.py", "stop_postgres(pg_ctl, data)")


def test_the_guard_checks_the_allowlisted_launchers_too():
    assert launcher_problems("_private_pg.py", "pg_ctl stop")
    assert not launcher_problems("_private_pg.py", 'fsync=off ... "-m", "immediate"')
