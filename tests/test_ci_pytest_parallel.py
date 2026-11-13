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
