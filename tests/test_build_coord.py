"""Build-coordination tests (build-storm prevention gate).

Covers the per-host flock-based slot gate in ``herdr_engineering.build_coord``:
single-flight bounding of concurrent builds, load-aware backoff, and release on
child exit.
"""
import os
import subprocess
import sys
import time

import pytest

_REPO_ROOT = str(__import__('pathlib').Path(__file__).resolve().parents[1])

from herdr_engineering.build_coord import (
    BuildGate,
    _default_slots,
    _default_state_dir,
    _load_per_core,
    run_gated,
)


@pytest.fixture()
def gate_dir(tmp_path):
    return tmp_path


def test_load_per_core_is_positive():
    assert _load_per_core() >= 0.0


def test_default_slots_scales_with_cores():
    """Default slots stay conservative even on high-core interactive hosts."""
    import os as _os
    cores = _os.cpu_count() or 4
    assert _default_slots() == max(2, min(4, cores - 4))
    assert _default_slots() >= 2  # never below 2
    # On any real host the default should not collapse to 1.
    assert _default_slots() >= 2


def test_default_slots_cap_high_core_hosts(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    assert _default_slots() == 4


def test_default_state_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path))
    assert _default_state_dir() == tmp_path / "build-gates"


def test_acquire_and_release_single_slot(gate_dir):
    gate = BuildGate(state_dir=gate_dir, slots=2)
    fd, slot = gate.acquire(["true"], max_load=0)
    assert slot == 0
    assert fd is not None
    assert gate.status()[0]["held"] is True
    os.close(fd)  # releasing the flock frees the slot
    assert gate.status()[0]["held"] is False


def test_second_acquire_uses_other_slot(gate_dir):
    gate = BuildGate(state_dir=gate_dir, slots=2)
    fd0, slot0 = gate.acquire(["a"], max_load=0)
    fd1, slot1 = gate.acquire(["b"], max_load=0)
    assert {slot0, slot1} == {0, 1}
    os.close(fd0)
    os.close(fd1)


def test_all_slots_busy_raises_timeout(gate_dir):
    gate = BuildGate(state_dir=gate_dir, slots=1)
    fd, _ = gate.acquire(["a"], max_load=0)
    with pytest.raises(TimeoutError):
        gate.acquire(["b"], max_load=0, wait_seconds=0.2)
    os.close(fd)


def test_contention_records_telemetry(gate_dir):
    """When a build has to wait for a slot, a telemetry line is appended so
    the gate's --slots/--max-load can be tuned from real contention data."""
    import json as _json
    gate = BuildGate(state_dir=gate_dir, slots=1)
    fd, _ = gate._try_acquire(0, ["hold"])  # occupy the only slot
    try:
        with pytest.raises(TimeoutError):
            gate.acquire(["b"], max_load=0, wait_seconds=0.2)
        tele = gate_dir / "telemetry.jsonl"
        assert tele.exists()
        lines = tele.read_text().strip().splitlines()
        assert lines, "expected at least one contention telemetry line"
        rec = _json.loads(lines[-1])
        assert rec["event"] == "contention"
        assert rec["slots"] == 1
        assert "load_core" in rec
        assert "ts" in rec
        assert rec["busy_pids"]  # the held slot's pid is recorded
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_holds_slot_across_exec(gate_dir):
    """The flock must survive execvp so it's held for the child's lifetime."""
    env = dict(os.environ, HERDR_ENGINEERING_STATE=str(gate_dir))
    script = (
        "import sys; sys.path.insert(0, " + repr(_REPO_ROOT) + "); "
        "from herdr_engineering.build_coord import run_gated; "
        "sys.exit(run_gated(['sleep', '2'], slots=2, max_load=0))"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], env=env)
    child_dir = gate_dir / "build-gates"  # child stores under HERDR_ENGINEERING_STATE/build-gates
    try:
        time.sleep(0.8)  # child should be inside `sleep 2` holding the slot
        gate = BuildGate(state_dir=child_dir, slots=2)
        assert gate.status()[0]["held"] is True
    finally:
        proc.wait(timeout=10)
    # after the child exits the slot is free
    gate = BuildGate(state_dir=child_dir, slots=2)
    assert gate.status()[0]["held"] is False


def test_run_gated_no_command_returns_2(gate_dir):
    assert run_gated([], json_out=False) == 2


def test_bypass_on_timeout_runs_ungated(tmp_path):
    """When all slots stay busy past --wait, bypass-on-timeout runs the build
    ungated instead of failing (best-effort coordination)."""
    import subprocess

    from herdr_engineering.build_coord import BuildGate

    state = tmp_path / "state"
    gate = BuildGate(state_dir=state, slots=1)
    # Hold the single slot so any gated acquire times out immediately.
    fd, _ = gate._try_acquire(0, ["hold"])
    try:
        script = (
            "import sys; sys.path.insert(0, " + repr(_REPO_ROOT) + "); "
            "from herdr_engineering.build_coord import run_gated; "
            "sys.exit(run_gated(['python3', '-c', 'print(\"BYPA$$ED\")'], "
            "slots=1, max_load=0, wait_seconds=0.5, "
            f"bypass_on_timeout=True, state_dir={str(state)!r}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=20)
        assert out.returncode == 0
        assert "BYPA$$ED" in out.stdout
        assert "running ungated" in out.stderr
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_sccache_server_daemon_does_not_pin_slot_or_maintenance_lock(tmp_path):
    """The per-policy sccache server outlives the build; started by the wrapper
    without the lock fds it must not keep the slot flock or LOCK_SH alive."""
    import fcntl

    state = tmp_path / "state"
    pidfile = tmp_path / "daemon.pid"
    stub = tmp_path / "sccache"          # a real daemonizing executable, not a mock
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os,sys,time\n"
        "if sys.argv[1:] == ['--start-server'] and os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        "    time.sleep(8); os._exit(0)\n")
    stub.chmod(0o755)
    script = (
        "import fcntl, os, sys; sys.path.insert(0, " + repr(_REPO_ROOT) + "); "
        "from herdr_engineering.build_coord import _run_gated; "
        f"os.makedirs({str(state)!r}, exist_ok=True); "
        f"m = os.open({str(state / 'build-maintenance.lock')!r}, os.O_RDWR|os.O_CREAT); "
        "fcntl.flock(m, fcntl.LOCK_SH); os.set_inheritable(m, True); "
        "sys.exit(_run_gated(['true'], slots=1, max_load=0, record=True, "
        f"state_dir={str(state)!r}, child_env=dict(os.environ, RUSTC_WRAPPER={str(stub)!r})))"
    )
    try:
        r = subprocess.run([sys.executable, "-c", script], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        assert r.returncode == 0
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(int(pidfile.read_text()), 0)  # the server really outlives the build
        gate = BuildGate(state_dir=state, slots=1)
        assert gate.status()[0]["held"] is False
        fd = os.open(state / "build-maintenance.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # cleanup/evict can take it
        finally:
            os.close(fd)
        os.close(gate.acquire(["second"], wait_seconds=1)[0])  # second build gets the slot
    finally:
        try:
            os.kill(int(pidfile.read_text()), 9)
        except Exception:
            pass
