"""Tests for the parallel pytest settings the unit gate runs with (scripts/ci/pytest-parallel.sh, scripts/ci/unit-gates.sh).

The gate takes ~90 minutes serially, so it runs under pytest-xdist. The settings must stay overridable (the CI exec host's CPU
count is not known in advance), capped (a 128-core host must not start 128 workers that each import the world), and must keep
every test in a file on one worker (`--dist=loadfile`): tests are written against per-file module state, not per-test isolation.
These drive the real shell function in a subshell, and a real pytest-xdist run, because the guarantees are about those.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
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
            env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_XDIST")},   # we may be a worker ourselves
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    parallel = run(markexpr("HERDR_PYTEST_FAST_MARKEXPR"), "-n", "2", "--dist=loadfile")
    assert "test_fast" in parallel and "test_slow" not in parallel and "test_integ" not in parallel
    tail = run(markexpr("HERDR_PYTEST_SLOW_MARKEXPR"))
    assert "test_slow PASSED" in tail and "test_fast" not in tail and "test_integ" not in tail
    assert "[gw" not in tail and "created: " not in tail   # serial: no xdist worker


def test_the_gate_runs_the_parallel_pass_then_a_serial_tail_both_under_the_watchdog() -> None:
    assert "run-watched.py" in GATE and WATCHER.exists()
    assert GATE.index('"$HERDR_PYTEST_FAST_MARKEXPR"') < GATE.index('"$HERDR_PYTEST_SLOW_MARKEXPR"')
    fast_line = next(line for line in GATE.splitlines() if "HERDR_PYTEST_FAST_MARKEXPR" in line and "pytest" in line)
    slow_line = next(line for line in GATE.splitlines() if "HERDR_PYTEST_SLOW_MARKEXPR" in line and "pytest" in line)
    assert fast_line.startswith("watched ") and "PYTEST_PARALLEL" in fast_line
    assert " watched python" in slow_line and "PYTEST_PARALLEL" not in slow_line
    assert "timeout" not in fast_line + slow_line           # no guessed flat cap on the run


def watch(soft: str, hard: str, script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(WATCHER), soft, hard, "--", sys.executable, "-u", "-c", script],
                          capture_output=True, text=True, timeout=60, env={**os.environ, **env})


def test_the_watchdog_kills_a_silent_idle_hang_with_124() -> None:
    done = watch("1", "3", "import time; time.sleep(30)")
    assert done.returncode == 124 and "hung" in done.stderr and "soft limit" in done.stderr


def test_the_watchdog_never_kills_a_slow_run_that_keeps_printing() -> None:
    done = watch("1", "3", "import time\nfor i in range(6):\n    print(i); time.sleep(1)")
    assert done.returncode == 0 and "soft limit" in done.stderr and "hung" not in done.stderr


def test_the_watchdog_never_kills_a_silent_run_that_is_busy_on_cpu() -> None:
    done = watch("1", "4", "import time\nend = time.time() + 6\nwhile time.time() < end: sum(range(10000))")
    assert done.returncode == 0 and "hung" not in done.stderr


def test_the_watchdog_passes_the_exit_status_through() -> None:
    assert watch("5", "5", "raise SystemExit(3)").returncode == 3


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"


def wait_gone(pid: int, seconds: float = 15) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return False


def test_sigterm_to_the_watchdog_stops_the_command_and_exits_128_plus_signal(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(1234)"
    wd = subprocess.Popen([sys.executable, str(WATCHER), "100", "100", "--", sys.executable, "-c", script],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.monotonic() + 15
    while not pidfile.exists() and time.monotonic() < end:
        time.sleep(0.1)
    child = int(pidfile.read_text())
    assert alive(child)
    time.sleep(0.5)
    wd.send_signal(signal.SIGTERM)
    assert wd.wait(timeout=30) == 128 + signal.SIGTERM
    assert wait_gone(child), "the command outlived the watchdog"


def test_a_busy_parent_is_not_hung_after_a_cpu_heavy_child_exits() -> None:
    script = (
        "import subprocess, sys, time\n"
        "subprocess.run([sys.executable, '-c', 'import time\\nend = time.time() + 4\\nwhile time.time() < end: sum(range(10000))'])\n"
        "end = time.time() + 6\n"
        "while time.time() < end: sum(range(10000))"
    )
    done = watch("100", "4", script)
    assert done.returncode == 0 and "hung" not in done.stderr, done.stderr


def test_cpu_in_a_descendant_that_called_setsid_counts_and_a_hang_kills_it(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    daemon = f"import os, time; os.setsid(); open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(1234)"
    # Silent, idle parent; its own-session child sleeps: no progress anywhere -> killed as hung, daemon included.
    script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {daemon!r}]); time.sleep(1234)"
    done = watch("1", "4", script)
    assert done.returncode == 124
    assert wait_gone(int(pidfile.read_text())), "a hang kill left a setsid descendant running"
    # The same shape, but the other-session child burns CPU while the parent is silent: not a hang.
    busy = "import os, time; os.setsid(); end = time.time() + 6\nwhile time.time() < end: sum(range(10000))"
    script = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {busy!r}])"
    assert watch("1", "4", script).returncode == 0


def test_a_signal_killed_command_exits_128_plus_signal_not_a_wrapped_status() -> None:
    done = watch("5", "5", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
    assert done.returncode == 128 + signal.SIGKILL


def test_the_gate_gives_pytest_timeout_room_to_name_a_hung_slow_test_before_the_watchdog_fires() -> None:
    m = re.search(r'HERDR_PYTEST_HANG_SECONDS="\$\{HERDR_PYTEST_SLOW_HANG_SECONDS:-(\d+)\}"', GATE)
    assert m and int(m.group(1)) > 3600


def daemonize_cmd(pidfile: Path, body: str) -> str:
    """A shell that starts a setsid python in the background and exits at once: the postmaster-via-pg_ctl shape."""
    code = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n{body}"
    return f"subprocess.run(['bash', '-c', 'setsid {sys.executable} -c \"$0\" & exit 0', {code!r}])"


def wait_file(path: Path) -> int:
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if path.exists() and path.read_text():
            return int(path.read_text())
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared")


def test_a_daemon_orphaned_before_the_first_poll_still_counts_as_progress(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    spawn = daemonize_cmd(pidfile, "end = time.time() + 7\nwhile time.time() < end: sum(range(10000))")
    # The silent root sleeps; only the orphaned daemon computes. Without finding it, hard=4 would kill at 124.
    done = watch("1", "4", f"import subprocess, time; {spawn}; time.sleep(8)")
    assert done.returncode == 0 and "hung" not in done.stderr, done.stderr


def test_a_hang_kill_stops_an_orphaned_daemon_too(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    spawn = daemonize_cmd(pidfile, "time.sleep(1234)")
    done = watch("1", "3", f"import subprocess, time; {spawn}; time.sleep(1234)")
    assert done.returncode == 124
    assert wait_gone(wait_file(pidfile)), "a hang kill leaked the orphaned daemon"


def test_sigterm_to_the_watchdog_stops_an_orphaned_daemon_too(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    spawn = daemonize_cmd(pidfile, "time.sleep(1234)")
    wd = subprocess.Popen([sys.executable, str(WATCHER), "100", "100", "--", sys.executable, "-c",
                           f"import subprocess, time; {spawn}; time.sleep(1234)"], stderr=subprocess.PIPE)
    daemon = wait_file(pidfile)
    time.sleep(1.5)
    wd.send_signal(signal.SIGTERM)
    assert wd.wait(timeout=30) == 128 + signal.SIGTERM
    assert wait_gone(daemon), "SIGTERM left the orphaned daemon running"


def test_a_leaked_busy_survivor_does_not_keep_the_watchdog_alive_and_is_stopped(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    spawn = daemonize_cmd(pidfile, "while True: sum(range(10000))")
    started = time.monotonic()
    done = watch("100", "100", f"import subprocess; {spawn}", HERDR_WATCHED_ORPHAN_GRACE="3")
    assert done.returncode == 0                      # the root's own status
    assert time.monotonic() - started < 30
    assert wait_gone(wait_file(pidfile)), "the leaked busy process was left running"


def test_a_closed_reader_does_not_kill_or_hang_the_run() -> None:
    done = subprocess.run(
        f'{sys.executable} {WATCHER} 100 100 -- {sys.executable} -u -c '
        '"import time\nfor i in range(5): print(i); time.sleep(0.3)" | head -1',
        shell=True, capture_output=True, text=True, timeout=60, env={**os.environ},
    )
    assert done.stdout == "0\n" and "Traceback" not in done.stderr and "BrokenPipe" not in done.stderr, done.stderr


def _cpu_budget_args(cgroup_root: Path, **env: str) -> list[str]:
    return args_with(HERDR_CGROUP_ROOT=str(cgroup_root), **env)


def test_auto_is_capped_by_a_cgroup_cpu_quota_smaller_than_the_visible_cores(tmp_path: Path) -> None:
    """xdist's `auto` counts the cores the process may be scheduled on, not the CPU quota of its cgroup: a job granted 2 CPUs on a
    24-core node would start 8 workers that all stall each other (3 s socket reads timing out, `pg_ctl stop` past its wait)."""
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    args = _cpu_budget_args(tmp_path)
    assert args[:2] == ["-n", "2"]
    assert "--dist=loadfile" in args


def test_a_quota_of_one_and_a_half_cpus_rounds_down_but_never_to_zero(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert _cpu_budget_args(tmp_path)[:2] == ["-n", "1"]
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    assert _cpu_budget_args(tmp_path)[:2] == ["-n", "1"]


def test_an_unlimited_or_generous_quota_keeps_auto(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert _cpu_budget_args(tmp_path)[:2] == ["-n", "auto"]
    (tmp_path / "cpu.max").write_text("1600000 100000\n")   # 16 CPUs: the cap of 8 is the limit, as before
    assert _cpu_budget_args(tmp_path)[:2] == ["-n", "auto"]


def test_cgroup_v1_quota_files_are_read_too(tmp_path: Path) -> None:
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("300000\n")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert _cpu_budget_args(tmp_path)[:2] == ["-n", "3"]


def test_an_explicit_worker_count_is_not_second_guessed_by_the_quota(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    assert _cpu_budget_args(tmp_path, HERDR_PYTEST_WORKERS="6")[:2] == ["-n", "6"]


def test_no_cgroup_files_means_auto(tmp_path: Path) -> None:
    assert _cpu_budget_args(tmp_path)[:2] == ["-n", "auto"]


def test_the_gate_prints_the_host_facts_that_explain_a_starved_run() -> None:
    """Pipelines on hive-as-11-2-39 and -40 failed 7 of 7 while other nodes passed: the next failure must say what the node looked like."""
    assert "ci_host_facts" in GATE
    done = subprocess.run(["bash", "-c", f'. "{ROOT}/scripts/ci/host-facts.sh"; ci_host_facts'], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for key in ("host:", "cpus visible:", "cgroup cpu quota:", "loadavg:", "tmp filesystem:"):
        assert key in done.stdout, done.stdout
