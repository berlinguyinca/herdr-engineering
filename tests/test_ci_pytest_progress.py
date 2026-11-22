"""Tests for `scripts/ci/pytest-plugins/pytest_ci_progress.py` (issue #937).

Every test here runs a real pytest in a subprocess - real xdist workers, real files, no mocks - because the thing under test is
whether a human reading a killed CI step can tell what was still running. Assertions read the plugin's two outputs: the JSONL record
file and the lines it writes to the process's real stdout (which pytest's own capture cannot swallow).

Budget defaults come from measured CI walls, not guesses: since the shards landed (`#919`, pipelines 1958-2012) a shard's median is
346-611 s and its maximum 741 s, while pipeline 1754's single-pass gate ran 6829 s and pipeline 1978's shard 3 was `killed` at 1950 s
with nothing in the log about what it was doing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PLUGINS = REPO / "scripts" / "ci" / "pytest-plugins"
PLUGIN = "pytest_ci_progress"


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway pytest project: `files` maps a test filename to its source."""
    tests = tmp_path / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\naddopts = -q\n", encoding="utf-8"
    )
    for name, body in files.items():
        (tests / name).write_text(body, encoding="utf-8")
    return tmp_path


def _environ(env: dict[str, str]) -> dict[str, str]:
    """The environment a nested pytest pass runs with.

    The unit gate runs this file under xdist. A nested pass must not inherit the worker's identity (see the plugin's role rule), nor an
    ambient shard, cadence or plugin that would change what is being measured.
    """
    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(PLUGINS) + (
        os.pathsep + environ["PYTHONPATH"] if environ.get("PYTHONPATH") else ""
    )
    for name in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_TESTRUNUID",
        "PYTEST_PLUGINS",
        "HERDR_UNIT_SHARD",
        "HERDR_PYTEST_PROGRESS_EVERY_SECONDS",
        "HERDR_PYTEST_PROGRESS_EVERY_TESTS",
        "HERDR_PYTEST_PROGRESS_BUDGET_SECONDS",
        "HERDR_PYTEST_PROGRESS_DIR",
    ):
        environ.pop(name, None)
    environ.update(env)
    return environ


def _command(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]


def _run(
    tmp_path: Path,
    *,
    env: dict[str, str],
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(args),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_environ(env),
        timeout=300,
    )


def _records(progress_dir: Path) -> list[dict[str, Any]]:
    log = progress_dir / "progress.jsonl"
    if not log.exists():
        return []
    return [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# 1.5 s of sleep is long enough for a 0.2 s emitter to catch it in flight, and short enough not to slow the suite.
SLEEPY = "import time\n\n\ndef test_sleepy():\n    time.sleep(1.5)\n"
# xdist has to boot an interpreter per worker before the first test starts, and on a loaded CI node that can take longer than the sleep
# above - which is why the two tests that must catch a test *in flight* use this one: the window stays open while the workers come up.
SLEEPY_LONG = "import time\n\n\ndef test_sleepy():\n    time.sleep(6.0)\n"
FAST = "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"


def test_progress_records_are_written_while_the_run_is_still_running(tmp_path: Path) -> None:
    """A parallel pass leaves records that say, while it runs, what each worker is inside.

    The failure this closes (#937): pipeline 1978's shard 3 was killed at 1950 s and the log said nothing about the test it was in, so
    the next session re-measured from scratch. Records must arrive during the run, not at teardown - `pytest_file_times` already gives a
    teardown summary, and a teardown summary is worthless when the step is killed mid-run.
    """
    root = _project(tmp_path, {"test_fast.py": FAST, "test_sleepy.py": SLEEPY_LONG})
    progress = tmp_path / "progress"
    done = _run(
        root,
        env={
            "HERDR_PYTEST_PROGRESS_DIR": str(progress),
            "HERDR_PYTEST_PROGRESS_EVERY_SECONDS": "0.2",
        },
        args=["-n", "2", "-p", PLUGIN],
    )
    assert done.returncode == 0, done.stdout + done.stderr

    records = _records(progress)
    assert records, "a parallel run must leave progress records"
    assert records[-1].get("final") is True, "the last record marks the end of the pass"
    assert records[-1]["done"] == 3, f"every test must be counted, got {records[-1]}"

    # The whole point: at least one record names the test that was still running when it was written.
    running = [
        (state.get("current") or "")
        for record in records
        for state in record.get("worker_states", [])
    ]
    assert any("test_sleepy.py::test_sleepy" in node for node in running), (
        "a record taken during the 1.5 s test must name it as still running; the killed-shard case is exactly a long or wedged test, "
        f"records said: {running}"
    )
    # Progress must reach stdout too: a step that dies before the file is collected still shows the last line in the log.
    assert "[ci-progress]" in done.stdout, done.stdout[-800:]


def test_the_budget_dump_names_the_running_test_and_the_run_keeps_going(tmp_path: Path) -> None:
    """Overrunning the shard budget prints a diagnostic dump and does NOT stop the pass.

    The issue asks for an explicit wall-clock budget per shard "rather than dying at the platform limit". A budget that kills a slow but
    live run is worse than no budget - `scripts/ci/unit-gates.sh` says so deliberately ("a slow but live run is never killed"), and the
    watchdog in `scripts/ci/run-watched.py` owns stopping wedged ones. So the budget's job is evidence: name what is running, per worker,
    and keep going.
    """
    root = _project(tmp_path, {"test_fast.py": FAST, "test_sleepy.py": SLEEPY_LONG})
    progress = tmp_path / "progress"
    done = _run(
        root,
        env={
            "HERDR_PYTEST_PROGRESS_DIR": str(progress),
            "HERDR_PYTEST_PROGRESS_EVERY_SECONDS": "0.2",
            "HERDR_PYTEST_PROGRESS_BUDGET_SECONDS": "0.3",
        },
        args=["-n", "2", "-p", PLUGIN],
    )
    assert done.returncode == 0, (
        "a budget exceedance must not change the result of a live run: " + done.stdout[-800:]
    )
    assert "[ci-budget]" in done.stdout, done.stdout[-800:]
    dumps = [record for record in _records(progress) if record.get("kind") == "budget"]
    assert dumps, "the exceedance must be recorded, not only printed"
    named = [
        (state.get("current") or "")
        for record in dumps
        for state in record.get("worker_states", [])
    ]
    assert any("test_sleepy.py::test_sleepy" in node for node in named), (
        f"the budget dump must name the test still running, got {named}"
    )


def test_records_label_the_shard_they_belong_to(tmp_path: Path) -> None:
    """Three shards run at once, so a record without its shard is unusable evidence.

    `HERDR_UNIT_SHARD=<i>/<n>` is what `scripts/ci/unit-gates.sh` runs a shard with (see `unit-shard.py`); the record carries it so three
    logs can be told apart when they are read back.
    """
    root = _project(tmp_path, {"test_fast.py": FAST})
    progress = tmp_path / "progress"
    done = _run(
        root,
        env={
            "HERDR_PYTEST_PROGRESS_DIR": str(progress),
            "HERDR_PYTEST_PROGRESS_EVERY_SECONDS": "0.1",
            "HERDR_UNIT_SHARD": "2/3",
        },
        args=["-p", PLUGIN],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    records = _records(progress)
    assert records and all(record.get("shard") == "2/3" for record in records), records


def test_a_serial_run_reports_the_main_worker(tmp_path: Path) -> None:
    """`HERDR_PYTEST_WORKERS=0` runs serially (no xdist), and the gate still gets progress.

    A serial pass is the documented fallback when the xdist machinery breaks (`scripts/ci/pytest-rerun.sh`), so the plugin cannot depend
    on workers existing: without them there is one state, named `main`.
    """
    root = _project(tmp_path, {"test_fast.py": FAST, "test_sleepy.py": SLEEPY})
    progress = tmp_path / "progress"
    done = _run(
        root,
        env={
            "HERDR_PYTEST_PROGRESS_DIR": str(progress),
            "HERDR_PYTEST_PROGRESS_EVERY_SECONDS": "0.2",
        },
        args=["-p", PLUGIN],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    records = _records(progress)
    workers = {
        state.get("worker") for record in records for state in record.get("worker_states", [])
    }
    assert "main" in workers, f"a serial pass must report `main`, got {workers}"
    assert records[-1]["done"] == 3


def test_the_plugin_changes_no_test_result(tmp_path: Path) -> None:
    """Progress is a reader, not a participant: same suite, same counts, same exit code with and without it.

    `pytest_file_times` makes the same promise ("only reads reports"), and the gate's verdict is the one thing the CI log must not be able
    to perturb.
    """
    root = _project(
        tmp_path,
        {
            "test_mixed.py": (
                "import pytest\n\n\n"
                "def test_pass():\n    assert True\n\n\n"
                "def test_fail():\n    assert False\n\n\n"
                "@pytest.mark.skip(reason='by design')\n"
                "def test_skip():\n    pass\n"
            )
        },
    )
    progress = tmp_path / "progress"
    with_plugin = _run(root, env={"HERDR_PYTEST_PROGRESS_DIR": str(progress)}, args=["-p", PLUGIN])
    without = _run(root, env={}, args=[])
    assert with_plugin.returncode == without.returncode != 0
    for line in ("1 failed", "1 passed", "1 skipped"):
        assert line in with_plugin.stdout and line in without.stdout, (
            f"{line!r} must appear in both runs:\n--with--\n{with_plugin.stdout[-600:]}\n--without--\n{without.stdout[-600:]}"
        )


# --- the gate's wiring (scripts/ci/pytest-parallel.sh, scripts/ci/unit-gates.sh) -----------------------------------------------

HELPER = REPO / "scripts" / "ci" / "pytest-parallel.sh"
GATE = (REPO / "scripts" / "ci" / "unit-gates.sh").read_text(encoding="utf-8")


def sh(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run `script` in bash with the helper sourced, the way the other CI-helper tests do it."""
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; . "{HELPER}"; {script}'],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_report_load_loads_progress_alongside_file_times(tmp_path: Path) -> None:
    """Both reporting plugins go on every pass, including the rerun and the slow tail.

    `PYTEST_REPORT` is the array every pytest call in `scripts/ci/pytest-rerun.sh` expands, so loading the progress plugin here rather
    than at one call site is what makes the slow tail and the xdist-rerun pass report progress too.
    """
    done = sh(
        'herdr_pytest_report_load; printf "%s\\n" "${PYTEST_REPORT[@]}"',
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    args = done.stdout.split()
    assert "-p" in args
    assert "pytest_file_times" in args and "pytest_ci_progress" in args, args
    assert "--durations=40" in args, "the teardown report stays; progress supplements it"


def test_progress_budget_is_per_shard_and_measured(tmp_path: Path) -> None:
    """A shard gets 900 s, an unsharded pass 1800 s, and an operator's value wins.

    The numbers come from CI, not from taste: shards run 346-611 s median with a 741 s maximum (pipelines 1958-2012), so 900 s is past any
    healthy shard and far below the ~1950 s at which pipeline 1978's shard 3 was killed. `unit-gates-main` measured 999-1405 s, so an
    unsharded pass gets 1800 s.
    """
    shard = sh(
        "export HERDR_UNIT_SHARD=2/3; unset HERDR_PYTEST_PROGRESS_BUDGET_SECONDS; "
        "herdr_pytest_progress_defaults > /dev/null; "
        'echo "$HERDR_PYTEST_PROGRESS_BUDGET_SECONDS $HERDR_PYTEST_PROGRESS_EVERY_SECONDS $HERDR_PYTEST_PROGRESS_EVERY_TESTS"',
        tmp_path,
    )
    assert shard.returncode == 0, shard.stderr
    budget, every, tests = shard.stdout.split()
    assert (budget, every, tests) == ("900", "60", "200")

    whole = sh(
        "unset HERDR_UNIT_SHARD HERDR_PYTEST_PROGRESS_BUDGET_SECONDS; "
        "herdr_pytest_progress_defaults > /dev/null; "
        'echo "$HERDR_PYTEST_PROGRESS_BUDGET_SECONDS"',
        tmp_path,
    )
    assert whole.stdout.strip() == "1800", whole.stdout + whole.stderr

    override = sh(
        "export HERDR_UNIT_SHARD=1/3 HERDR_PYTEST_PROGRESS_BUDGET_SECONDS=0; "
        "herdr_pytest_progress_defaults > /dev/null; "
        'echo "$HERDR_PYTEST_PROGRESS_BUDGET_SECONDS"',
        tmp_path,
    )
    assert override.stdout.strip() == "0", (
        "0 must stay available: the budget is evidence, not a mandate"
    )


def test_progress_defaults_are_written_where_a_stopped_step_still_leaves_them(
    tmp_path: Path,
) -> None:
    """The record file lives under the gitignored `.tmp/`, and the directory exists before the pass starts."""
    done = sh(
        "unset HERDR_PYTEST_PROGRESS_DIR; herdr_pytest_progress_defaults > /dev/null; "
        'echo "$HERDR_PYTEST_PROGRESS_DIR"; test -d "$HERDR_PYTEST_PROGRESS_DIR" && echo exists',
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    directory, marker = done.stdout.split()
    assert directory.endswith(".tmp/ci-progress"), directory
    assert marker == "exists"
    assert ".tmp/" in (REPO / ".gitignore").read_text(encoding="utf-8"), (
        "progress under .tmp/ must stay untracked: a gate that dirties the worktree fails the structural gates for the wrong reason"
    )


def test_a_bad_progress_override_fails_the_gate_instead_of_running_silently(tmp_path: Path) -> None:
    """A typo in a CI override must stop the gate the way `HERDR_PYTEST_WORKERS` does, not quietly disable the evidence."""
    for name, value in (
        ("HERDR_PYTEST_PROGRESS_EVERY_SECONDS", "soon"),
        ("HERDR_PYTEST_PROGRESS_EVERY_TESTS", "1000-ish"),
        ("HERDR_PYTEST_PROGRESS_BUDGET_SECONDS", "15m"),
    ):
        done = sh(f"export {name}={value}; herdr_pytest_progress_defaults", tmp_path)
        assert done.returncode != 0, f"{name}={value} must be refused"
        assert name in done.stderr, done.stderr


def test_the_gate_calls_the_progress_defaults_before_the_passes_run() -> None:
    """`unit-gates.sh` has to load the defaults, or the plugin runs on its own built-in defaults and the budgets are fiction."""
    assert "herdr_pytest_progress_defaults" in GATE
    assert GATE.index("herdr_pytest_progress_defaults") < GATE.index("herdr_pytest_run_gate")


def test_a_nested_pass_is_still_the_controller_however_it_was_started(tmp_path: Path) -> None:
    """Inheriting `PYTEST_XDIST_WORKER` must not make a pass believe it is a worker.

    The unit gate runs pytest under xdist, and a pytest that a test starts in a subprocess inherits that variable. If the plugin decided
    its role from the environment, the nested controller would think it was a worker and emit nothing - a CI-progress plugin that goes
    silent exactly in the shape CI runs in.
    """
    root = _project(tmp_path, {"test_fast.py": FAST})
    progress = tmp_path / "progress"
    done = _run(
        root,
        env={
            "HERDR_PYTEST_PROGRESS_DIR": str(progress),
            "HERDR_PYTEST_PROGRESS_EVERY_SECONDS": "0.1",
            "PYTEST_XDIST_WORKER": "gw7",
            "PYTEST_XDIST_TESTRUNUID": "not-a-real-run",
        },
        args=["-p", PLUGIN],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    records = _records(progress)
    assert records and records[-1]["done"] == 2, records
    assert not any("gw7" in str(record) for record in records), (
        f"a pass that is a controller must report itself as `main`, not as the inherited worker: {records}"
    )


def test_two_passes_sharing_a_progress_directory_stay_separate(tmp_path: Path) -> None:
    """The gate's fast pass and its slow tail run at once in one directory, and must not read each other's workers.

    `herdr_pytest_run_gate` in `scripts/ci/pytest-rerun.sh` starts the slow tail while the first pass's rerun may still be alive, both
    pointed at the same `HERDR_PYTEST_PROGRESS_DIR`. With one flat set of worker state files, the parallel pass would report the serial
    pass's `main` as one of its workers, and either pass's startup cleanup would delete the other's evidence mid-run - so the lines CI
    prints would be wrong in the one situation they exist for.
    """
    root = _project(
        tmp_path,
        {
            "test_a.py": "import time\n\n\ndef test_a1():\n    time.sleep(1.2)\n\n\ndef test_a2():\n    time.sleep(1.2)\n",
            "test_b.py": "import time\n\n\ndef test_b1():\n    time.sleep(1.2)\n\n\ndef test_b2():\n    time.sleep(1.2)\n",
        },
    )
    progress = tmp_path / "progress"
    common = {
        "HERDR_PYTEST_PROGRESS_DIR": str(progress),
        "HERDR_PYTEST_PROGRESS_EVERY_SECONDS": "0.2",
    }
    parallel = subprocess.Popen(
        _command(["-n", "2", "-p", PLUGIN, "tests/test_a.py"]),
        cwd=root,
        env=_environ(common),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    serial = subprocess.Popen(
        _command(["-p", PLUGIN, "tests/test_b.py"]),
        cwd=root,
        env=_environ(common),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    parallel_out = parallel.communicate(timeout=300)
    serial_out = serial.communicate(timeout=300)
    assert parallel.returncode == 0, parallel_out[0] + parallel_out[1]
    assert serial.returncode == 0, serial_out[0] + serial_out[1]

    records = _records(progress)
    # A parallel pass's first line is written in `pytest_configure`, before xdist has created its session and therefore before the run id
    # exists. That one line may be unresolved; anything later must say who wrote it.
    for record in [record for record in records if not record.get("pass")]:
        assert record["t"] == 0.0 and not record["worker_states"], (
            f"only the first line of a pass may predate the run id: {record}"
        )
    by_pass: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("pass"):
            by_pass.setdefault(str(record["pass"]), []).append(record)
    assert len(by_pass) == 2, (
        f"two concurrent passes must not be filed as one, got ids {sorted(by_pass)}"
    )

    for pass_id, pass_records in by_pass.items():
        finals = [record for record in pass_records if record.get("final")]
        assert finals and finals[-1]["done"] == 2, f"pass {pass_id} lost tests: {pass_records}"
        workers = {state["worker"] for record in pass_records for state in record["worker_states"]}
        # The serial pass (`local-<pid>`) is its own only worker; the parallel pass reports its xdist workers and never the other pass's
        # `main`, which would read as a fifth worker having appeared out of nowhere.
        expected = {"main"} if pass_id.startswith("local-") else {"gw0", "gw1"}
        assert workers and workers <= expected, (
            f"pass {pass_id} reported {sorted(workers)}, which are not its own workers"
        )


def test_the_plugin_is_loadable_by_name_from_the_plugins_directory() -> None:
    """CI loads plugins by name with `-p`, so the name must resolve on the PYTHONPATH the gate sets.

    `scripts/ci/pytest-parallel.sh` puts `scripts/ci/pytest-plugins` on PYTHONPATH for `pytest_file_times`; the progress plugin has to be
    importable the same way or the gate runs without it and silently measures nothing - which is how a CI fix becomes a CI fix that proves
    nothing.
    """
    done = subprocess.run(
        [sys.executable, "-c", f"import {PLUGIN}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PLUGINS)},
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
