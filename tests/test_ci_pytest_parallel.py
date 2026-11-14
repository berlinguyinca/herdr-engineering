"""Tests for the parallel pytest settings the unit gate runs with (scripts/ci/pytest-parallel.sh, scripts/ci/unit-gates.sh).

The gate takes ~90 minutes serially, so it runs under pytest-xdist. The settings must stay overridable (the CI exec host's CPU
count is not known in advance), capped (a 128-core host must not start 128 workers that each import the world), and must keep
every test in a file on one worker (`--dist=loadfile`): tests are written against per-file module state, not per-test isolation.
These drive the real shell function in a subshell, and a real pytest-xdist run, because the guarantees are about those.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci" / "pytest-parallel.sh"
GATE = (ROOT / "scripts" / "ci" / "unit-gates.sh").read_text()
PYPROJECT = ROOT / "pyproject.toml"


def args_with(**env: str) -> list[str]:
    exports = "".join(f"export {k}={v}; " for k, v in env.items())
    done = subprocess.run(
        ["bash", "-c", f'unset HERDR_PYTEST_WORKERS HERDR_PYTEST_MAXPROCESSES; {exports}. "{HELPER}"; herdr_pytest_parallel_args'],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def test_default_is_auto_workers_with_a_cap_and_loadfile() -> None:
    args = args_with()
    assert args[:2] == ["-n", "auto"]
    cap = [a for a in args if a.startswith("--maxprocesses=")]
    assert len(cap) == 1 and int(cap[0].split("=")[1]) >= 2
    assert "--dist=loadfile" in args


def test_worker_count_and_cap_are_overridable() -> None:
    args = args_with(HERDR_PYTEST_WORKERS="3", HERDR_PYTEST_MAXPROCESSES="5")
    assert args[:2] == ["-n", "3"]
    assert "--maxprocesses=5" in args


def test_zero_workers_means_a_plain_serial_run() -> None:
    assert args_with(HERDR_PYTEST_WORKERS="0") == []


def test_a_non_numeric_worker_count_is_refused_not_guessed() -> None:
    done = subprocess.run(
        ["bash", "-c", f'export HERDR_PYTEST_WORKERS=lots; . "{HELPER}"; herdr_pytest_parallel_args'],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert done.returncode != 0 and "HERDR_PYTEST_WORKERS" in done.stderr


def load_array(**env: str) -> list[str]:
    """What unit-gates.sh actually passes to pytest, one element per line, under the gate's own `set -euo pipefail`."""
    exports = "".join(f"export {k}={v}; " for k, v in env.items())
    done = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; unset HERDR_PYTEST_WORKERS HERDR_PYTEST_MAXPROCESSES; {exports}. "{HELPER}"; '
                       'herdr_pytest_parallel_load; for w in ${PYTEST_PARALLEL[@]+"${PYTEST_PARALLEL[@]}"}; do printf "[%s]\\n" "$w"; done'],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr
    return [line[1:-1] for line in done.stdout.splitlines()]


def test_the_loaded_array_keeps_the_dash_n_flag_that_echo_would_eat() -> None:
    # Regression: assembling the words with `echo $words` made `-n` an echo option, so CI ran `pytest auto ...` (exit 4).
    assert load_array(HERDR_PYTEST_WORKERS="3", HERDR_PYTEST_MAXPROCESSES="5") == ["-n", "3", "--maxprocesses=5", "--dist=loadfile"]
    assert load_array()[:2] == ["-n", "auto"]


def test_the_loaded_array_is_empty_for_a_serial_run_and_the_gate_survives_set_u() -> None:
    assert load_array(HERDR_PYTEST_WORKERS="0") == []


def test_the_loader_fails_on_a_bad_override() -> None:
    done = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; export HERDR_PYTEST_WORKERS=lots; . "{HELPER}"; herdr_pytest_parallel_load; echo reached'],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert done.returncode != 0 and "reached" not in done.stdout


def test_the_gate_uses_the_helper_and_still_raises_the_fd_limit_first() -> None:
    assert "pytest-parallel.sh" in GATE and "herdr_pytest_parallel_load" in GATE
    assert GATE.index("herdr_ensure_fd_limit") < GATE.index("herdr_pytest_parallel_load")
    assert "python -m pytest" in GATE  # bare `pytest` imports another editable checkout


def test_xdist_is_a_pinned_dev_dependency() -> None:
    dev = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]["dev"]
    pins = [d for d in dev if d.startswith("pytest-xdist")]
    assert pins and re.fullmatch(r"pytest-xdist==\d+\.\d+\.\d+", pins[0]), pins


def test_a_zero_or_non_numeric_cap_is_refused() -> None:
    for bad in ("0", "lots"):
        done = subprocess.run(
            ["bash", "-c", f'unset HERDR_PYTEST_WORKERS HERDR_PYTEST_MAXPROCESSES; export HERDR_PYTEST_MAXPROCESSES={bad}; . "{HELPER}"; herdr_pytest_parallel_args'],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert done.returncode != 0 and "HERDR_PYTEST_MAXPROCESSES" in done.stderr, (bad, done.stderr)
        assert done.stdout == ""


def test_xdist_dependencies_are_pinned_exactly() -> None:
    dev = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]["dev"]
    assert any(re.fullmatch(r"execnet==\d+\.\d+\.\d+", d) for d in dev), dev


def test_loadfile_spreads_files_over_workers_but_keeps_each_file_on_one(tmp_path: Path) -> None:
    """A real two-worker run on four small files: more than one worker is used, and every file stays on exactly one."""
    names = ("test_a", "test_b", "test_c", "test_d")
    for name in names:
        (tmp_path / f"{name}.py").write_text(
            "import os, time\n"
            + "".join(
                f"def test_{i}(record_property):\n    time.sleep(0.05)\n"
                "    record_property('w', os.environ['PYTEST_XDIST_WORKER'])\n"
                for i in range(6)
            )
        )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    report = tmp_path / "r.xml"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(tmp_path / "pytest.ini"),
         "-n", "2", "--dist=loadfile", f"--junitxml={report}", str(tmp_path)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    xml = report.read_text()
    all_workers: set[str] = set()
    for name in names:
        workers = re.findall(rf'classname="{name}"[^>]*>\s*<properties>\s*<property name="w" value="(gw\d+)"', xml)
        assert len(workers) == 6, (name, workers)
        assert len(set(workers)) == 1, (name, workers)
        all_workers |= set(workers)
    assert len(all_workers) > 1, f"all files ran on {all_workers}: the run was not parallel"


# --- slow tests run in a serial tail, and the gate is protected against hangs ---------------------------------------------------

WATCHER = ROOT / "scripts" / "ci" / "run-watched.py"


def markexpr(name: str) -> str:
    done = subprocess.run(["bash", "-c", f'. "{HELPER}"; printf %s "${name}"'], capture_output=True, text=True, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    return done.stdout


def collected(expr: str) -> set[str]:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", expr,
         "tests/control_plane/test_postgres_recovery.py"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert done.returncode in (0, 5), done.stdout + done.stderr
    return {line for line in done.stdout.splitlines() if "::" in line}


def test_the_cardinality_test_is_slow_with_an_explicit_timeout_and_runs_only_in_the_tail() -> None:
    name = "test_production_cardinality_snapshot_recovery_and_database_restart"
    fast, slow = collected(markexpr("HERDR_PYTEST_FAST_MARKEXPR")), collected(markexpr("HERDR_PYTEST_SLOW_MARKEXPR"))
    assert any(name in t for t in slow) and not any(name in t for t in fast)
    assert fast and not (fast & slow)
    everything = collected("not e2e and not integration")   # pyproject's own default expression
    assert fast | slow == everything                         # coverage is identical
    source = (ROOT / "tests" / "control_plane" / "test_postgres_recovery.py").read_text()
    assert re.search(r"@pytest\.mark\.slow\s+@pytest\.mark\.timeout\((\d+)\)\s+def " + name, source)
    assert int(re.search(r"mark\.timeout\((\d+)\)", source).group(1)) > 900


def test_a_real_run_deselects_slow_tests_from_the_parallel_pass_and_runs_them_in_the_tail(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text(
        "import os, pytest\n"
        "def test_fast(record_property):\n    record_property('w', os.environ.get('PYTEST_XDIST_WORKER', 'serial'))\n"
        "@pytest.mark.slow\ndef test_slow(record_property):\n    record_property('w', os.environ.get('PYTEST_XDIST_WORKER', 'serial'))\n"
        "@pytest.mark.integration\ndef test_integ():\n    pass\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -m 'not e2e and not integration'\nmarkers =\n    slow\n    integration\n    e2e\n")

    def run(expr: str, *extra: str) -> str:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-c", str(tmp_path / "pytest.ini"), "-m", expr,
             "-v", *extra, str(tmp_path)],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    parallel = run(markexpr("HERDR_PYTEST_FAST_MARKEXPR"), "-n", "2", "--dist=loadfile")
    assert "test_fast" in parallel and "test_slow" not in parallel and "test_integ" not in parallel
    tail = run(markexpr("HERDR_PYTEST_SLOW_MARKEXPR"))
    assert "test_slow PASSED" in tail and "test_fast" not in tail and "test_integ" not in tail
    assert "gw0" not in tail and "gw1" not in tail      # serial: no xdist worker


def test_the_gate_runs_the_parallel_pass_then_a_serial_tail_both_under_the_watchdog() -> None:
    assert "run-watched.py" in GATE and WATCHER.exists()
    assert GATE.index('"$HERDR_PYTEST_FAST_MARKEXPR"') < GATE.index('"$HERDR_PYTEST_SLOW_MARKEXPR"')
    fast_line = next(line for line in GATE.splitlines() if "HERDR_PYTEST_FAST_MARKEXPR" in line and "pytest" in line)
    slow_line = next(line for line in GATE.splitlines() if "HERDR_PYTEST_SLOW_MARKEXPR" in line and "pytest" in line)
    assert fast_line.startswith("watched ") and "PYTEST_PARALLEL" in fast_line
    assert slow_line.startswith("watched ") and "PYTEST_PARALLEL" not in slow_line
    assert "timeout" not in fast_line + slow_line           # no guessed flat cap on the run


def watch(soft: str, hard: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(WATCHER), soft, hard, "--", sys.executable, "-u", "-c", script],
                          capture_output=True, text=True, timeout=60)


def test_the_watchdog_kills_a_silent_idle_hang_with_124() -> None:
    done = watch("1", "3", "import time; time.sleep(30)")
    assert done.returncode == 124 and "hung" in done.stderr and "soft limit" in done.stderr


def test_the_watchdog_never_kills_a_slow_run_that_keeps_printing() -> None:
    done = watch("1", "3", "import time\nfor i in range(6):\n    print(i); time.sleep(1)")
    assert done.returncode == 0 and "soft limit" in done.stderr and "hung" not in done.stderr


def test_the_watchdog_never_kills_a_silent_run_that_is_busy_on_cpu() -> None:
    done = watch("1", "2", "import time\nend = time.time() + 5\nwhile time.time() < end: sum(range(10000))")
    assert done.returncode == 0 and "hung" not in done.stderr


def test_the_watchdog_passes_the_exit_status_through() -> None:
    assert watch("5", "5", "raise SystemExit(3)").returncode == 3
