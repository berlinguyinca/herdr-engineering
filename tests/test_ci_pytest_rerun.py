"""Tests for the one-shot rerun of the unit gate's parallel pytest pass (scripts/ci/pytest-rerun.sh, wired in scripts/ci/unit-gates.sh).

Pipeline main-cron #1710: two pytest-xdist workers died on a CI host with load 155 on 4 CPUs, xdist's scheduler then raised an
internal KeyError and pytest exited 3, though the same commit passed in four other runs. The gate reruns the parallel pass ONCE,
with fewer workers, when the xdist machinery broke; a genuine test failure, a hang (watchdog 124) and a passing run never rerun.
Everything here drives the real shell functions around a real pytest-xdist run whose worker really dies (os._exit).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RERUN = ROOT / "scripts" / "ci" / "pytest-rerun.sh"
PARALLEL = ROOT / "scripts" / "ci" / "pytest-parallel.sh"
GATE = (ROOT / "scripts" / "ci" / "unit-gates.sh").read_text()
CRASH_LINE = "node down: Not properly terminated"


def sh(script: str, cwd: Path = ROOT, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{PARALLEL}"; . "{RERUN}"; {script}'],
        capture_output=True, text=True, cwd=cwd, timeout=300,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", **env},
    )


def wanted(status: int, text: str, tmp_path: Path) -> bool:
    log = tmp_path / "log.txt"
    log.write_text(text)
    return sh(f'herdr_pytest_rerun_wanted {status} "{log}"').returncode == 0


# --- the decision -------------------------------------------------------------------------------------------------------------

def test_exit_3_internal_error_is_rerun_even_without_a_signature(tmp_path: Path) -> None:
    assert wanted(3, "INTERNALERROR> something\n", tmp_path)


def test_a_worker_crash_signature_is_rerun_at_exit_1(tmp_path: Path) -> None:
    assert wanted(1, f"..F[gw0] {CRASH_LINE}\n", tmp_path)
    assert wanted(1, "FAILED x - worker 'gw3' crashed while running 'tests/test_a.py::test_b'\n", tmp_path)
    assert wanted(1, "INTERNALERROR>   File \".../xdist/dsession.py\", line 306\nINTERNALERROR> KeyError: <WorkerController gw5>\n", tmp_path)


def test_a_genuine_failure_is_never_rerun(tmp_path: Path) -> None:
    assert not wanted(1, "FAILED tests/test_a.py::test_b - assert 1 == 2\n1 failed, 40 passed\n", tmp_path)


def test_a_pass_and_a_watchdog_hang_are_never_rerun_even_with_a_signature_in_the_log(tmp_path: Path) -> None:
    assert not wanted(0, f"[gw0] {CRASH_LINE}\n", tmp_path)
    assert not wanted(124, f"[gw0] {CRASH_LINE}\n", tmp_path)
    assert not wanted(124, "", tmp_path)


def test_other_statuses_without_a_signature_are_not_rerun(tmp_path: Path) -> None:
    for status in (2, 4, 5):
        assert not wanted(status, "usage error\n", tmp_path)


# --- the rerun arguments ------------------------------------------------------------------------------------------------------

def rerun_args(**env: str) -> list[str]:
    done = sh("herdr_pytest_rerun_args", **env)
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def test_the_rerun_uses_two_workers_by_default_and_is_overridable() -> None:
    assert rerun_args() == ["-n", "2", "--maxprocesses=2", "--dist=loadfile"]
    assert rerun_args(HERDR_PYTEST_RERUN_WORKERS="3") == ["-n", "3", "--maxprocesses=3", "--dist=loadfile"]


def test_rerun_workers_zero_means_serial() -> None:
    assert rerun_args(HERDR_PYTEST_RERUN_WORKERS="0") == []


def test_a_bad_rerun_worker_count_is_refused() -> None:
    done = sh("herdr_pytest_rerun_args", HERDR_PYTEST_RERUN_WORKERS="lots")
    assert done.returncode != 0 and "HERDR_PYTEST_RERUN_WORKERS" in done.stderr


# --- the whole pass, around a real xdist run ----------------------------------------------------------------------------------

CONFTEST = '''
import os
def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput"):          # the controller only, not the xdist workers
        with open(os.environ["RUNS_FILE"], "a") as f:
            f.write("run\\n")
'''


def project(tmp_path: Path, body: str) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "conftest.py").write_text(CONFTEST)
    (proj / "test_a.py").write_text("def test_one():\n    pass\n")
    (proj / "test_b.py").write_text(body)
    (proj / "test_c.py").write_text("def test_three():\n    pass\n")
    return proj


def run_pass(proj: Path, runner: str = "plain", workers: str = "2", **env: str) -> tuple[subprocess.CompletedProcess[str], int]:
    runs = proj / "runs.txt"
    parallel = f"PYTEST_PARALLEL=(-n {workers} --dist=loadfile)" if workers else "PYTEST_PARALLEL=()"
    prelude = ('plain() { "$@"; }\n'
               f'watched() {{ python3 "{ROOT}/scripts/ci/run-watched.py" 1 3 -- "$@"; }}\n{parallel}\n')
    done = sh(f'{prelude}herdr_pytest_run_fast {runner} "not e2e"', cwd=proj, RUNS_FILE=str(runs), **env)
    return done, len(runs.read_text().split()) if runs.exists() else 0


WORKER_DIES_ONCE = '''
import os
def test_two():
    marker = os.path.join(os.path.dirname(__file__), "died-once")
    if os.environ.get("PYTEST_XDIST_WORKER") and not os.path.exists(marker):
        open(marker, "w").close()
        os._exit(1)            # a real, unclean worker death
'''


def test_a_worker_that_really_dies_triggers_exactly_one_rerun_that_passes_and_says_why(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, WORKER_DIES_ONCE))
    out = done.stdout + done.stderr
    assert "crashed" in out or CRASH_LINE in out, out      # the genuine xdist output, so the signature test is not circular
    assert runs == 2, out
    assert done.returncode == 0, out
    assert "rerunning the parallel pytest pass once" in out and "-n 2" in out, out


def test_a_worker_that_always_dies_reruns_once_and_still_fails(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, "import os\ndef test_two():\n    os._exit(1)\n"))
    assert runs == 2 and done.returncode != 0, done.stdout + done.stderr


def test_a_genuine_test_failure_is_not_rerun(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, "def test_two():\n    assert 1 == 2\n"))
    assert runs == 1 and done.returncode == 1, done.stdout + done.stderr
    assert "rerunning" not in done.stdout + done.stderr


def test_a_passing_pass_runs_once(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, "def test_two():\n    pass\n"))
    assert runs == 1 and done.returncode == 0, done.stdout + done.stderr


def test_a_hang_killed_by_the_real_watchdog_is_not_rerun(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, "import time\ndef test_two():\n    time.sleep(1234)\n"), runner="watched")
    assert done.returncode == 124 and runs == 1, done.stdout + done.stderr
    assert "rerunning" not in done.stdout + done.stderr


def test_a_serial_pass_has_no_xdist_to_blame_and_is_not_rerun(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, "import sys\ndef test_two():\n    sys.exit(3)\n"), workers="")
    assert runs == 1 and done.returncode != 0, done.stdout + done.stderr


def test_the_serial_rerun_option_runs_without_xdist(tmp_path: Path) -> None:
    done, runs = run_pass(project(tmp_path, WORKER_DIES_ONCE), HERDR_PYTEST_RERUN_WORKERS="0")
    out = done.stdout + done.stderr
    assert runs == 2 and done.returncode == 0 and "serially" in out, out


def test_the_gate_wires_the_rerun_around_the_parallel_pass_only() -> None:
    assert "pytest-rerun.sh" in GATE
    gate = next(line for line in GATE.splitlines() if line.startswith("herdr_pytest_run_gate"))
    assert '"$HERDR_PYTEST_FAST_MARKEXPR"' in gate
    helper = RERUN.read_text()
    assert 'herdr_pytest_run_fast "$fast_runner" "$fast_expr"' in helper           # the fast pass keeps its one-shot rerun
    assert all("rerun" not in ln for ln in helper.splitlines() if '"$slow_runner"' in ln)   # the slow tail never reruns
