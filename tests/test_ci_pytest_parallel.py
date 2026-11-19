"""Tests for the parallel pytest settings the unit gate runs with (scripts/ci/pytest-parallel.sh, scripts/ci/unit-gates.sh).

The gate takes ~90 minutes serially, so it runs under pytest-xdist. The settings must stay overridable (the CI exec host's CPU
count is not known in advance), capped (a 128-core host must not start 128 workers that each import the world), and must keep
every test in a file on one worker (`--dist=loadfile`): tests are written against per-file module state, not per-test isolation.
These drive the real shell function in a subshell, and a real pytest-xdist run, because the guarantees are about those.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci" / "pytest-parallel.sh"
GATE = (ROOT / "scripts" / "ci" / "unit-gates.sh").read_text()
PYPROJECT = ROOT / "pyproject.toml"


@pytest.fixture(autouse=True)
def calm_host(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker-count tests must not depend on how busy the machine running them is: point the load average at a calm file."""
    calm = tmp_path_factory.mktemp("loadavg") / "loadavg"
    calm.write_text("0.00 0.00 0.00 1/100 1\n")
    monkeypatch.setenv("HERDR_LOADAVG_FILE", str(calm))


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
    assert fast_line.startswith("herdr_pytest_run_fast watched ")   # the helper adds PYTEST_PARALLEL and the one-shot rerun (test_ci_pytest_rerun.py)
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


def test_sigterm_to_the_watchdog_stops_the_command_and_exits_128_plus_signal(pidfiles) -> None:
    pidfile = pidfiles()
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


def test_cpu_in_a_descendant_that_called_setsid_counts_and_a_hang_kills_it(pidfiles) -> None:
    pidfile = pidfiles()
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


def test_a_daemon_orphaned_before_the_first_poll_still_counts_as_progress(pidfiles) -> None:
    pidfile = pidfiles()
    spawn = daemonize_cmd(pidfile, "end = time.time() + 7\nwhile time.time() < end: sum(range(10000))")
    # The silent root sleeps; only the orphaned daemon computes. Without finding it, hard=4 would kill at 124.
    done = watch("1", "4", f"import subprocess, time; {spawn}; time.sleep(8)")
    assert done.returncode == 0 and "hung" not in done.stderr, done.stderr


def test_a_hang_kill_stops_an_orphaned_daemon_too(pidfiles) -> None:
    pidfile = pidfiles()
    spawn = daemonize_cmd(pidfile, "time.sleep(1234)")
    done = watch("1", "3", f"import subprocess, time; {spawn}; time.sleep(1234)")
    assert done.returncode == 124
    assert wait_gone(wait_file(pidfile)), "a hang kill leaked the orphaned daemon"


def test_sigterm_to_the_watchdog_stops_an_orphaned_daemon_too(pidfiles) -> None:
    pidfile = pidfiles()
    spawn = daemonize_cmd(pidfile, "time.sleep(1234)")
    wd = subprocess.Popen([sys.executable, str(WATCHER), "100", "100", "--", sys.executable, "-c",
                           f"import subprocess, time; {spawn}; time.sleep(1234)"], stderr=subprocess.PIPE)
    daemon = wait_file(pidfile)
    time.sleep(1.5)
    wd.send_signal(signal.SIGTERM)
    assert wd.wait(timeout=30) == 128 + signal.SIGTERM
    assert wait_gone(daemon), "SIGTERM left the orphaned daemon running"


def test_a_leaked_busy_survivor_does_not_keep_the_watchdog_alive_and_is_stopped(pidfiles) -> None:
    pidfile = pidfiles()
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


VISIBLE = len(os.sched_getaffinity(0))
CAP = 8
# Quotas are chosen relative to the CPUs this test process may use, so the tests hold on any host (and under `taskset -c 0-1`).
Q = min(VISIBLE, CAP) - 1          # the largest quota that must shrink the worker count
needs_two_cpus = pytest.mark.skipif(Q < 1, reason="needs at least 2 visible CPUs to show a quota below them")


def cg(tmp_path: Path, *, v2: dict[str, str] | None = None, v1: dict[str, tuple[int, int]] | None = None,
       proc: str = "0::/") -> dict[str, str]:
    """A real cgroup tree: v2 maps 'a/b' -> cpu.max text; v1 maps 'job' -> (quota, period) under the cpu,cpuacct controller."""
    root = tmp_path / "cgroup"
    root.mkdir()
    for rel, text in (v2 or {}).items():
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel / "cpu.max").write_text(text + "\n")
    for rel, (quota, period) in (v1 or {}).items():
        d = root / "cpu,cpuacct" / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.cfs_quota_us").write_text(f"{quota}\n")
        (d / "cpu.cfs_period_us").write_text(f"{period}\n")
    (tmp_path / "proc-cgroup").write_text(proc + "\n")
    return {"HERDR_CGROUP_ROOT": str(root), "HERDR_PROC_CGROUP": str(tmp_path / "proc-cgroup")}


def workers_for(env: dict[str, str], **more: str) -> list[str]:
    return args_with(**env, **more)[:2]


@needs_two_cpus
def test_auto_is_capped_by_a_cgroup_cpu_quota_smaller_than_the_visible_cores(tmp_path: Path) -> None:
    """xdist's `auto` counts the cores in the affinity mask, not the CPU quota of the cgroup: a job granted fewer CPUs than it can see
    starts workers that starve each other (3 s socket reads timing out, `pg_ctl stop` past its wait)."""
    env = cg(tmp_path, v2={"job": f"{Q * 100000} 100000"}, proc="0::/job")
    args = args_with(**env)
    assert args[:2] == ["-n", str(Q)] and "--dist=loadfile" in args


@needs_two_cpus
def test_the_smallest_quota_up_the_cgroup_path_wins(tmp_path: Path) -> None:
    """Slurm and containers often put the limit on a parent: the job's own cgroup says `max` or something larger."""
    env = cg(tmp_path, v2={"a": f"{Q * 100000} 100000", "a/b": "max 100000", "a/b/c": f"{(Q + 5) * 100000} 100000"}, proc="0::/a/b/c")
    assert workers_for(env) == ["-n", str(Q)]
    (tmp_path / "t2").mkdir()
    env = cg(tmp_path / "t2", v2={"a": "max 100000", "a/b": f"{Q * 100000} 100000"}, proc="0::/a/b")
    assert workers_for(env) == ["-n", str(Q)]


@needs_two_cpus
def test_a_fractional_quota_rounds_down_but_never_to_zero(tmp_path: Path) -> None:
    assert workers_for(cg(tmp_path, v2={"j": "150000 100000"}, proc="0::/j")) == ["-n", "1"]
    (tmp_path / "x").mkdir()
    assert workers_for(cg(tmp_path / "x", v2={"j": "50000 100000"}, proc="0::/j")) == ["-n", "1"]


def test_an_unlimited_or_generous_quota_keeps_auto(tmp_path: Path) -> None:
    assert workers_for(cg(tmp_path, v2={"j": "max 100000"}, proc="0::/j")) == ["-n", "auto"]
    (tmp_path / "y").mkdir()
    generous = f"{max(VISIBLE, CAP) * 100000} 100000"          # at or above both the cap and the visible cores
    assert workers_for(cg(tmp_path / "y", v2={"j": generous}, proc="0::/j")) == ["-n", "auto"]


@needs_two_cpus
def test_cgroup_v1_reads_the_cpu_controller_path_from_proc_self_cgroup_and_its_parents(tmp_path: Path) -> None:
    env = cg(tmp_path, v1={"": (Q * 100000, 100000), "job": (-1, 100000)}, proc="4:cpu,cpuacct:/job")
    assert workers_for(env) == ["-n", str(Q)]


def test_an_explicit_worker_count_is_not_second_guessed_by_the_quota(tmp_path: Path) -> None:
    env = cg(tmp_path, v2={"j": "100000 100000"}, proc="0::/j")
    assert workers_for(env, HERDR_PYTEST_WORKERS="6") == ["-n", "6"]


def test_no_cgroup_files_means_auto(tmp_path: Path) -> None:
    assert workers_for(cg(tmp_path)) == ["-n", "auto"]


@needs_two_cpus
def test_the_scaling_really_reaches_pytest_as_a_worker_count(tmp_path: Path) -> None:
    """End to end for one case: the exact words the gate passes are `-n <Q>`, and xdist accepts them."""
    env = cg(tmp_path, v2={"job": f"{Q * 100000} 100000"}, proc="0::/job")
    assert args_with(**env) == ["-n", str(Q), f"--maxprocesses={CAP}", "--dist=loadfile"]


@needs_two_cpus
def test_the_gate_prints_the_same_effective_quota_the_worker_count_uses(tmp_path: Path) -> None:
    """Pipelines on hive-as-11-2-39 and -40 failed 7 of 7 while other nodes passed: the next failure must say what the node looked like."""
    assert "ci_host_facts" in GATE
    env = cg(tmp_path, v2={"a": f"{Q * 100000} 100000", "a/b": "max 100000"}, proc="0::/a/b")
    exports = "".join(f"export {k}={v}; " for k, v in env.items())
    done = subprocess.run(["bash", "-c", f'{exports}. "{ROOT}/scripts/ci/host-facts.sh"; ci_host_facts'], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    for key in ("host:", "cpus visible:", "loadavg:", "tmp filesystem:"):
        assert key in done.stdout, done.stdout
    assert f"cgroup cpu quota: {Q} CPUs" in done.stdout, done.stdout


# --- review nits from #824 -------------------------------------------------------------------------------------------------------


def load_watcher():
    spec = importlib.util.spec_from_file_location("run_watched_under_test", WATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def started_before(pid: int, when: float) -> bool:
    """True if the process now holding `pid` started no later than `when` (the pidfile's mtime): a pid reused after the file was
    written belongs to a stranger and must not be killed."""
    try:
        start_ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
    except (OSError, ValueError, IndexError, StopIteration):
        return False
    return boot + start_ticks / os.sysconf("SC_CLK_TCK") <= when + 1.0


def reap_pidfiles(paths: list[Path]) -> None:
    """Kill whatever pid each file names (if it is still ours to kill) and delete the file."""
    for path in paths:
        try:
            pid = int(path.read_text())
        except (OSError, ValueError):
            pid = None
        if pid and pid > 1 and started_before(pid, path.stat().st_mtime):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        path.unlink(missing_ok=True)


@pytest.fixture
def pidfiles(tmp_path: Path):
    """Factory for pidfile paths; teardown kills leaked daemons (a failing test must not leave sleepers) and removes the files."""
    made: list[Path] = []

    def make(name: str = "pid") -> Path:
        path = tmp_path / f"{name}.{len(made)}"
        made.append(path)
        return path

    yield make
    reap_pidfiles(made)


def test_the_pidfile_fixture_kills_a_leaked_process_and_removes_the_file(tmp_path: Path) -> None:
    pidfile = tmp_path / "leak"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1234)"])
    pidfile.write_text(str(sleeper.pid))
    reap_pidfiles([pidfile])
    assert sleeper.wait(timeout=10) == -signal.SIGKILL and not pidfile.exists()
    # A pidfile naming a process that started AFTER the file was written is a reused pid: leave the stranger alone.
    stale = tmp_path / "stale"
    stale.write_text("0")
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1234)"])
    try:
        time.sleep(1.2)
        stale.write_text(str(stranger.pid))
        os.utime(stale, (time.time() - 60, time.time() - 60))
        reap_pidfiles([stale])
        assert stranger.poll() is None and not stale.exists()
    finally:
        stranger.kill()
        stranger.wait()
    reap_pidfiles([tmp_path / "never-written"])      # a missing or empty pidfile is not an error
    (tmp_path / "empty").write_text("")
    reap_pidfiles([tmp_path / "empty"])


def out_probe(prelude: str, **popen: object) -> subprocess.CompletedProcess[str]:
    code = (
        "import importlib.util, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('rw', {str(WATCHER)!r}); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"{prelude}\n"
        "o = m.Out(); o.write(b'hello'); o.write(b'again')\n"
        "os.write(2, b'survived')\n"
    )
    return subprocess.run([sys.executable, "-I", "-c", code], stderr=subprocess.PIPE, text=True, timeout=30, **{"stdout": subprocess.PIPE, **popen})


def test_out_write_survives_a_full_nonblocking_stdout_pipe() -> None:
    r, w = os.pipe()
    os.set_blocking(w, False)
    try:
        while True:
            os.write(w, b"x" * 65536)
    except BlockingIOError:
        pass
    reader_leaves = threading.Timer(1.0, os.close, (r,))     # the reader goes away while the pipe is full: EPIPE, not a hang
    reader_leaves.start()
    try:
        done = out_probe("", stdout=w)
    finally:
        reader_leaves.join()
        os.close(w)
    assert done.returncode == 0 and "survived" in done.stderr, done.stderr


def test_out_write_waits_for_a_late_reader_of_a_nonblocking_stdout_and_loses_nothing() -> None:
    total = 1_000_000
    for via in ("out", "watchdog"):
        r, w = os.pipe()
        os.set_blocking(w, False)
        if via == "out":
            code = (
                "import importlib.util, sys\n"
                f"spec = importlib.util.spec_from_file_location('rw', {str(WATCHER)!r}); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                f"o = m.Out(); o.write(b'x' * {total}); assert not o.broken\n"
            )
            argv = [sys.executable, "-I", "-c", code]
        else:
            argv = [sys.executable, str(WATCHER), "100", "100", "--", sys.executable, "-c", f"import sys; sys.stdout.write('x' * {total})"]
        proc = subprocess.Popen(argv, stdout=w, stderr=subprocess.PIPE, text=True)
        os.close(w)
        time.sleep(1.5)                                      # the reader starts late: the pipe is full and EAGAIN is returned
        got = 0
        while chunk := os.read(r, 65536):
            got += len(chunk)
        os.close(r)
        assert proc.wait(timeout=60) == 0, (via, proc.stderr.read())
        assert got == total, (via, got)


def test_out_write_survives_a_closed_or_missing_stdout() -> None:
    closed = out_probe("os.close(1)")
    assert closed.returncode == 0 and "survived" in closed.stderr, closed.stderr
    missing = out_probe("sys.stdout = None")
    assert missing.returncode == 0 and "survived" in missing.stderr, missing.stderr


def test_the_group_is_not_signalled_once_the_root_is_reaped_unless_a_member_proves_it_is_ours(monkeypatch) -> None:
    mod = load_watcher()
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()                                              # reaped: its pid/pgid may now be reused by anyone
    tree = mod.Tree(proc.pid, "tok")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    tree.signal_all(signal.SIGTERM)
    assert calls == [], "signalled a process group whose leader was already reaped and which has no live member"
    # An unreaped root's group is always ours.
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        t2 = mod.Tree(live.pid, "tok", live)
        t2.signal_all(signal.SIGCONT)
        assert calls == [(live.pid, signal.SIGCONT)], "an unreaped root's group must still be signalled"
    finally:
        live.kill()
        live.wait()


def test_a_reaped_root_whose_pid_was_reused_by_an_unrelated_group_leader_is_never_group_signalled(monkeypatch) -> None:
    mod = load_watcher()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()                                               # reaped: returncode is set
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        tree = mod.Tree(stranger.pid, "tok", gone)            # as if the stranger had been handed the root's old pid
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
        tree.signal_all(signal.SIGCONT)
        assert calls == [], "signalled an unrelated process group that reused the root's pid"
    finally:
        stranger.kill()
        stranger.wait()


def test_a_reaped_root_whose_group_still_holds_a_known_member_is_group_signalled(monkeypatch) -> None:
    mod = load_watcher()
    root = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!"], stdout=subprocess.PIPE, text=True, start_new_session=True)
    member = int(root.stdout.readline())
    try:
        root.wait()                                           # the leader is reaped; its child still holds the group
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
        tree = mod.Tree(root.pid, "tok", root)
        tree.known[member] = mod.read_stat(member)[3] + 1     # same pid, wrong start time: not our member
        tree.signal_all(signal.SIGCONT)
        assert calls == []
        tree.known[member] = mod.read_stat(member)[3]
        tree.signal_all(signal.SIGCONT)
        assert calls == [(root.pid, signal.SIGCONT)]
    finally:
        os.kill(member, signal.SIGKILL)


def test_a_token_that_appears_in_the_environ_after_the_first_look_is_found(tmp_path: Path) -> None:
    mod = load_watcher()
    token = "late-token-1"
    code = (
        f"import os, sys, time; time.sleep(1.5); os.execve(sys.executable, [sys.executable, '-c', 'import time; time.sleep(30)'], "
        f"{{**os.environ, 'HERDR_RUN_WATCHED_TOKEN': {token!r}}})"
    )
    env = {k: v for k, v in os.environ.items() if k != "HERDR_RUN_WATCHED_TOKEN"}
    child = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        tree = mod.Tree(os.getpid(), token)
        start = mod.read_stat(child.pid)[3]
        assert tree.has_token(child.pid, start) is False        # before the exec: no token yet
        end = time.monotonic() + 15
        while time.monotonic() < end and not tree.has_token(child.pid, start):
            time.sleep(0.2)
        assert tree.has_token(child.pid, start), "a negative result was cached and the token was never seen"
    finally:
        child.kill()
        child.wait()


def test_cpu_of_a_reused_pid_with_a_new_start_time_counts_as_progress() -> None:
    mod = load_watcher()
    tree = mod.Tree(1, "tok")
    assert tree.note_cpu(4242, 100, 50.0) is True            # first sight
    assert tree.note_cpu(4242, 100, 50.0) is False           # idle
    assert tree.note_cpu(4242, 100, 51.0) is True            # CPU rose
    assert tree.note_cpu(4242, 999, 0.5) is True, "a new process on a reused pid has no history to compare with"
    assert tree.note_cpu(4242, 999, 0.5) is False


def test_signal_handlers_are_installed_before_the_command_is_spawned() -> None:
    main = next(n for n in ast.parse(WATCHER.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == "main")
    lines = {"popen": [], "handler": []}
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "Popen":
                lines["popen"].append(node.lineno)
            if node.func.attr == "signal" and isinstance(node.func.value, ast.Name) and node.func.value.id == "signal":
                lines["handler"].append(node.lineno)
    assert lines["popen"] and lines["handler"] and max(lines["handler"]) < min(lines["popen"])


def test_a_signal_sent_by_the_command_the_instant_it_starts_still_stops_it_cleanly(pidfiles) -> None:
    pidfile = pidfiles()
    script = f"import os, signal, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); os.kill(os.getppid(), signal.SIGTERM); time.sleep(1234)"
    done = subprocess.run([sys.executable, str(WATCHER), "100", "100", "--", sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 128 + signal.SIGTERM, (done.returncode, done.stderr)
    assert wait_gone(wait_file(pidfile)), "the command outlived the watchdog"


@pytest.mark.parametrize("args", [
    ["abc", "5", "--", "true"], ["5", "xyz", "--", "true"], ["-1", "5", "--", "true"], ["5", "0", "--", "true"],
    ["nan", "5", "--", "true"], ["5", "inf", "--", "true"], ["5", "5", "--"], ["5", "5", "true"], ["5", "--", "5", "true"],
])
def test_bad_arguments_print_a_usage_error_and_exit_2_without_a_traceback(args: list[str]) -> None:
    done = subprocess.run([sys.executable, str(WATCHER), *args], capture_output=True, text=True, timeout=30)
    assert done.returncode == 2, (args, done.returncode, done.stderr)
    assert "Traceback" not in done.stderr and "usage" in done.stderr.lower()


def test_a_missing_command_is_a_clear_error_not_a_traceback() -> None:
    done = subprocess.run([sys.executable, str(WATCHER), "5", "5", "--", "/nonexistent/definitely-not-here"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 127 and "Traceback" not in done.stderr and "definitely-not-here" in done.stderr


def test_a_hang_kill_sends_sigterm_first_so_the_command_can_clean_up(tmp_path: Path) -> None:
    marker = tmp_path / "got-term"
    script = (
        "import signal, sys, time\n"
        f"def h(*_):\n    open({str(marker)!r}, 'w').write('x'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, h)\n"
        "time.sleep(1234)"
    )
    done = watch("1", "2", script)
    assert done.returncode == 124 and marker.exists(), "the hang kill went straight to SIGKILL"
    # ...and a command that ignores SIGTERM is still killed after the grace.
    stubborn = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1234)"
    assert watch("1", "2", stubborn).returncode == 124


def test_cleanup_output_written_by_a_sigterm_handler_is_forwarded_and_does_not_stall(tmp_path: Path) -> None:
    script = (
        "import signal, sys, time\n"
        "def h(*_):\n    sys.stdout.write('c' * 200000); sys.stdout.flush(); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, h)\n"
        "time.sleep(1234)"
    )
    began = time.monotonic()
    done = watch("1", "2", script)
    assert done.returncode == 124
    assert done.stdout.count("c") == 200000, len(done.stdout)
    assert time.monotonic() - began < 8, "the hang kill stalled on a full pipe until SIGKILL"


@pytest.mark.parametrize("value", ["soon", "nan", "inf", "-1"])
def test_a_bad_orphan_grace_value_is_a_usage_error_and_never_starts_the_command(tmp_path: Path, value: str) -> None:
    marker = tmp_path / "started"
    done = watch("5", "5", f"open({str(marker)!r}, 'w').write('x')", HERDR_WATCHED_ORPHAN_GRACE=value)
    assert done.returncode == 2 and "Traceback" not in done.stderr and not marker.exists()


def test_a_flooding_tree_cannot_hold_off_the_sigkill_after_the_grace(tmp_path: Path) -> None:
    flood = ("import signal, sys, time\n"
             "def h(*_):\n    while True: sys.stdout.write('f' * 65536)\n"      # floods only once told to stop, and never exits
             "signal.signal(signal.SIGTERM, h)\ntime.sleep(1234)")
    began = time.monotonic()
    done = watch("1", "1", flood)
    assert done.returncode == 124 and time.monotonic() - began < 20, "the drain loop starved the SIGKILL"


# --- a loaded host gets fewer workers (pipeline 1710: load 155 on 4 CPUs, two workers died) ------------------------------------------

def loadavg(tmp_path: Path, one_minute: float) -> dict[str, str]:
    f = tmp_path / "loadavg"
    f.write_text(f"{one_minute:.2f} {one_minute:.2f} {one_minute:.2f} 3/900 12345\n")
    return {"HERDR_LOADAVG_FILE": str(f)}


def test_a_heavily_loaded_host_gets_one_worker_not_eight(tmp_path: Path) -> None:
    assert args_with(**loadavg(tmp_path, 155.0))[:2] == ["-n", "1"]


def test_a_calm_host_keeps_auto(tmp_path: Path) -> None:
    assert args_with(**loadavg(tmp_path, 0.0))[:2] == ["-n", "auto"]


@needs_two_cpus
def test_workers_are_the_cpus_the_load_leaves_free(tmp_path: Path) -> None:
    free = min(VISIBLE, CAP) - 1                      # leave `free` CPUs idle: that many workers, below the cap
    assert args_with(**loadavg(tmp_path, float(VISIBLE - free)))[:2] == ["-n", str(free)]


def test_an_explicit_worker_count_wins_over_the_load(tmp_path: Path) -> None:
    assert args_with(HERDR_PYTEST_WORKERS="6", **loadavg(tmp_path, 155.0))[:2] == ["-n", "6"]


def test_a_missing_or_unreadable_loadavg_means_no_load_bound(tmp_path: Path) -> None:
    assert args_with(HERDR_LOADAVG_FILE=str(tmp_path / "nope"))[:2] == ["-n", "auto"]
    (tmp_path / "junk").write_text("not a load\n")
    assert args_with(HERDR_LOADAVG_FILE=str(tmp_path / "junk"))[:2] == ["-n", "auto"]


def test_host_facts_prints_the_same_load_file_the_worker_bound_reads(tmp_path: Path) -> None:
    done = subprocess.run(["bash", "-c", f'. "{ROOT}/scripts/ci/host-facts.sh"; ci_host_facts'], capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, **loadavg(tmp_path, 7.25)})
    assert "loadavg: 7.25 7.25 7.25" in done.stdout, done.stdout
