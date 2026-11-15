"""The throwaway cluster must not depend on /dev/shm (CI containers clean or shrink it under a running postmaster)."""
import re
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


# Launchers (repo-relative path) that already stop with `-m immediate` and fsync off and are not converted to the helper.
_IMMEDIATE_ONLY = {
    "tests/control_plane/_private_pg.py",
    "tests/control_plane/test_project_intelligence_migration.py",
}

_RAW_FAST_STOP = re.compile(r"""["']-m["']\s*,\s*["']fast["']|-m\s+fast|pg_ctl[^\n]*\bstop\b|["']stop["']\s*\]""")


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not re.match(r"\s*(from|import)\s", ln) and not ln.lstrip().startswith("#")]


def launcher_problems(path: str, text: str) -> list[str]:
    """What is wrong with a test file that starts its own private Postgres (empty list = fine).
    `path` is repo-relative with forward slashes."""
    if path in _IMMEDIATE_ONLY:
        ok = "fsync=off" in text and '"-m", "immediate"' in text
        return [] if ok else [f"{path}: an allowlisted launcher must keep fsync=off and `-m immediate`"]
    code = "\n".join(_code_lines(text))
    problems = []
    if "FAST_TEST_OPTIONS" not in code:
        problems.append(f"{path}: does not start with FAST_TEST_OPTIONS outside its import (it would fsync)")
    if "stop_postgres(" not in code:
        problems.append(f"{path}: does not call tests.control_plane.pg_stop.stop_postgres(")
    if _RAW_FAST_STOP.search(code):
        problems.append(f"{path}: stops with a raw pg_ctl stop / `-m fast` instead of stop_postgres")
    return problems


ROOT = Path(__file__).resolve().parents[2]


def _launchers() -> list[Path]:
    skip = {Path(__file__).name, "pg_stop.py", "test_pg_stop.py"}
    return [p for p in ROOT.rglob("*.py") if p.name not in skip and "listen_addresses" in p.read_text() and p.parts[-2] != "playwright"]


def test_every_private_postgres_launcher_skips_disk_syncs_and_stops_through_the_shared_helper():
    """CI pipelines 1428/1444/1453/1454: `pg_ctl -m fast -w stop` (60 s wait) returned 1 on the shared node and failed the test that
    owned the fixture."""
    launchers = _launchers()
    assert len(launchers) >= 8, launchers
    problems = [m for p in launchers for m in launcher_problems(p.relative_to(ROOT).as_posix(), p.read_text())]
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
    assert launcher_problems("tests/control_plane/_private_pg.py", "pg_ctl stop")
    assert not launcher_problems("tests/control_plane/_private_pg.py", 'fsync=off ... "-m", "immediate"')


def test_the_guard_rejects_a_launcher_that_imports_both_names_but_stops_with_a_raw_fast_stop():
    text = ("from tests.control_plane.pg_stop import FAST_TEST_OPTIONS, stop_postgres\n"
            "opts = FAST_TEST_OPTIONS\nstop_postgres(pg_ctl, data)\n"
            'subprocess.run([pg_ctl, "-D", data, "-m", "fast", "-w", "stop"])\n')
    assert any("raw" in m for m in launcher_problems("tests/x/t.py", text))


def test_the_guard_rejects_a_launcher_that_imports_both_names_and_never_uses_them():
    text = "from tests.control_plane.pg_stop import FAST_TEST_OPTIONS, stop_postgres\n"
    assert len(launcher_problems("tests/x/t.py", text)) == 2


def test_the_allowlist_matches_the_repo_relative_path_not_the_bare_filename():
    assert launcher_problems("tests/elsewhere/_private_pg.py", "pg_ctl stop") != []
    assert launcher_problems("tests/control_plane/_private_pg.py", 'fsync=off "-m", "immediate"') == []
