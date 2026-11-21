"""The unit gate's slow tail runs CONCURRENTLY with the fast xdist pass (it used to follow it: ~5.7 min serial after ~14.7), and both
passes report where their time went (`--durations` plus a per-test-file / per-worker summary) so `--dist=loadfile` imbalance is visible.

Real pytest runs in throwaway projects; the overlap of the two passes is proven from timestamps the tests themselves write.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.e2e.pg import ThrowawayPostgres

ROOT = Path(__file__).resolve().parents[1]
PARALLEL = ROOT / "scripts" / "ci" / "pytest-parallel.sh"
RERUN = ROOT / "scripts" / "ci" / "pytest-rerun.sh"
GATE = (ROOT / "scripts" / "ci" / "unit-gates.sh").read_text()
FAST = "not slow"
SLOW = "slow"

STAMP = '''
import os, time
def stamp(name, seconds):
    d = os.path.dirname(__file__)
    open(os.path.join(d, name + ".start"), "w").write(repr(time.time()))
    time.sleep(seconds)
    open(os.path.join(d, name + ".end"), "w").write(repr(time.time()))
'''


def project(tmp_path: Path, fast_body: str = "stamp('fast', 3)", slow_body: str = "stamp('slow', 3)") -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow\n")
    (proj / "test_fast_a.py").write_text(f"{STAMP}\ndef test_fast():\n    {fast_body}\n")
    (proj / "test_fast_b.py").write_text("def test_other():\n    pass\n")
    (proj / "test_slow.py").write_text(f"{STAMP}\nimport pytest\n\n@pytest.mark.slow\ndef test_slow():\n    {slow_body}\n")
    return proj


def sh(script: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{PARALLEL}"; . "{RERUN}"; {script}'],
        capture_output=True, text=True, cwd=cwd, timeout=300,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", **env},
    )


PRELUDE = 'plain() { "$@"; }\nPYTEST_PARALLEL=(-n 2 --dist=loadfile)\nherdr_pytest_report_load\n'


def gate(proj: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return sh(f'{PRELUDE}herdr_pytest_run_gate plain plain "{FAST}" "{SLOW}"', proj, **env)


def span(proj: Path, name: str) -> tuple[float, float]:
    return float((proj / f"{name}.start").read_text()), float((proj / f"{name}.end").read_text())


def test_the_slow_tail_overlaps_the_fast_pass_and_both_pass(tmp_path: Path) -> None:
    proj = project(tmp_path)
    done = gate(proj)
    assert done.returncode == 0, done.stdout + done.stderr
    (f0, f1), (s0, s1) = span(proj, "fast"), span(proj, "slow")
    assert s0 < f1 and f0 < s1, "the slow test ran after the fast pass instead of alongside it"
    assert any(line.startswith("[slow] ") for line in done.stdout.splitlines()), done.stdout   # its output is prefixed, not interleaved raw


def test_opting_out_runs_the_tail_after_the_fast_pass(tmp_path: Path) -> None:
    proj = project(tmp_path)
    done = gate(proj, HERDR_PYTEST_SLOW_CONCURRENT="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert span(proj, "fast")[1] <= span(proj, "slow")[0]


def test_a_failing_fast_pass_fails_the_gate_and_the_tail_still_finishes(tmp_path: Path) -> None:
    proj = project(tmp_path, fast_body="stamp('fast', 1); assert False")
    done = gate(proj)
    assert done.returncode == 1, done.stdout + done.stderr
    assert (proj / "slow.end").exists()


def test_a_failing_slow_tail_fails_the_gate(tmp_path: Path) -> None:
    proj = project(tmp_path, slow_body="stamp('slow', 1); assert False")
    done = gate(proj)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "slow tail" in done.stdout and "failed" in done.stdout


def test_a_tail_that_collects_nothing_fails_the_gate_on_purpose(tmp_path: Path) -> None:
    proj = project(tmp_path)
    (proj / "test_slow.py").write_text("def test_not_slow():\n    pass\n")
    done = gate(proj)
    assert done.returncode == 5, done.stdout + done.stderr         # exit 5 = nothing collected, the existing intent


def test_both_failing_returns_the_fast_status_not_the_tails(tmp_path: Path) -> None:
    proj = project(tmp_path, fast_body="assert False")          # fast exits 1 ...
    (proj / "test_slow.py").write_text("def test_not_slow():\n    pass\n")   # ... the tail collects nothing and exits 5
    assert gate(proj).returncode == 1


def test_a_killed_tail_with_no_status_fails_closed(tmp_path: Path) -> None:
    proj = project(tmp_path)
    done = sh(f'{PRELUDE}killed() {{ kill -9 $BASHPID; }}\nherdr_pytest_run_gate plain killed "{FAST}" "{SLOW}"', proj)
    assert done.returncode == 1, done.stdout + done.stderr


def test_the_tail_runs_idle_io_class_where_ionice_exists_and_still_runs_without_it(tmp_path: Path) -> None:
    proj = project(tmp_path)
    probe = f'{PRELUDE}slowrunner() {{ echo "cmd=$*"; "$@"; }}\nherdr_pytest_run_gate plain slowrunner "{FAST}" "{SLOW}"'
    has = subprocess.run("command -v ionice >/dev/null && ionice -c3 true", shell=True).returncode == 0
    out = sh(probe, proj).stdout
    assert ("ionice -c3 nice -n 5 python" in out) == has and "nice -n 5 python" in out
    bare = tmp_path / "bare"
    bare.mkdir()
    for tool in ("bash", "nice", "sed", "cat", "mktemp", "rm", "tee", "grep", "sleep", "date", "env", "dirname"):
        found = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if found:
            (bare / tool).symlink_to(found)
    (bare / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bare / "python").chmod(0o755)
    done = sh(probe, proj, PATH=f"{bare}")
    assert done.returncode == 0 and "ionice" not in done.stdout, done.stdout + done.stderr


def test_the_tail_runs_under_its_own_watchdog_hang_limit(tmp_path: Path) -> None:
    proj = project(tmp_path)
    done = sh(f'{PRELUDE}slowrunner() {{ echo "hang=$HERDR_PYTEST_HANG_SECONDS"; "$@"; }}\n'
              f'herdr_pytest_run_gate plain slowrunner "{FAST}" "{SLOW}"', proj, HERDR_PYTEST_HANG_SECONDS="11")
    assert "[slow] hang=11" in done.stdout


def test_the_tail_does_not_share_the_pytest_cache_with_the_fast_pass(tmp_path: Path) -> None:
    proj = project(tmp_path)
    assert gate(proj).returncode == 0
    assert (proj / ".pytest_cache").exists()       # the fast pass writes it ...
    done = sh(f'{PRELUDE}slowrunner() {{ echo "args=$*"; "$@"; }}\nherdr_pytest_run_gate plain slowrunner "{FAST}" "{SLOW}"', proj)
    assert "-p no:cacheprovider" in done.stdout    # ... the concurrent tail must not


def test_each_pass_prints_durations_and_a_per_file_and_per_worker_summary(tmp_path: Path) -> None:
    proj = project(tmp_path)
    done = gate(proj)
    out = done.stdout
    assert out.count("slowest 40 durations") == 2, out            # fast pass and slow tail
    assert "per-test-file time" in out and "test_fast_a.py" in out
    assert "per-worker busy time" in out and ("gw0" in out or "gw1" in out)


def test_the_report_args_are_empty_unless_loaded_so_other_callers_are_unchanged(tmp_path: Path) -> None:
    done = sh('echo "${#PYTEST_REPORT[@]}"', tmp_path)
    assert done.stdout.strip() == "0"


def test_the_gate_uses_the_concurrent_helper_with_both_passes_under_the_watchdog() -> None:
    assert "herdr_pytest_report_load" in GATE
    line = next(ln for ln in GATE.splitlines() if ln.startswith("herdr_pytest_run_gate"))
    assert "watched" in line and "watched_slow" in line
    assert '"$HERDR_PYTEST_FAST_MARKEXPR"' in line and '"$HERDR_PYTEST_SLOW_MARKEXPR"' in line
    assert 'HERDR_PYTEST_HANG_SECONDS="${HERDR_PYTEST_SLOW_HANG_SECONDS:-4200}"' in GATE
    assert "timeout" not in line
    helper = RERUN.read_text()
    tail = [ln for ln in helper.splitlines() if '"$slow_runner"' in ln and "-m" in ln]
    assert tail and all("python -m pytest" in ln for ln in tail), "the tail must use `python -m pytest`, never bare `pytest`"


def test_the_slow_tests_own_database_and_tmp_dirs_cannot_collide_with_the_workers(tmp_path: Path) -> None:
    """Why concurrency is safe: the one slow test starts its own throwaway PostgreSQL on a unix socket in a pid-named mkdtemp dir."""
    source = (ROOT / "tests" / "e2e" / "pg.py").read_text()
    assert "mkdtemp(prefix=f\"t16pg-{os.getpid()}-\")" in source
    options = ThrowawayPostgres(Path("/nonexistent"), tmp_path)._options()
    assert "unix_socket_directories=" in options, options   # socket-only: no TCP port to collide with the workers' clusters
