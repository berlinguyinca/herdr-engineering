"""Build-coordination tests (build-storm prevention gate).

Covers the per-host flock-based slot gate in ``herdr_engineering.build_coord``:
single-flight bounding of concurrent builds, load-aware backoff, and release on
child exit.
"""
import os
import sys
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from herdr_engineering.build_coord import (
    BuildGate,
    _load_per_core,
    _default_state_dir,
    run_gated,
)


@pytest.fixture()
def gate_dir(tmp_path):
    return tmp_path


def test_load_per_core_is_positive():
    assert _load_per_core() >= 0.0


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


def test_holds_slot_across_exec(gate_dir):
    """The flock must survive execvp so it's held for the child's lifetime."""
    env = dict(os.environ, HERDR_ENGINEERING_STATE=str(gate_dir))
    script = (
        "import sys; sys.path.insert(0, %r); "
        "from herdr_engineering.build_coord import run_gated; "
        "sys.exit(run_gated(['sleep', '2'], slots=2, max_load=0))"
        % "/home/wohlgemuth/IdeaProjects/herdr-engineering"
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
            "import sys; sys.path.insert(0, %r); "
            "from herdr_engineering.build_coord import run_gated; "
            "sys.exit(run_gated(['python3', '-c', 'print(\"BYPA$$ED\")'], "
            "slots=1, max_load=0, wait_seconds=0.5, "
            "bypass_on_timeout=True, state_dir=%r))"
            % ("/home/wohlgemuth/IdeaProjects/herdr-engineering", str(state))
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
