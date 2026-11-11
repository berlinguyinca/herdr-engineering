"""Tests for the file-descriptor floor (scripts/ci/ensure-fd-limit.sh) and the hang ceiling in pyproject.

Both exist for the same reason: a run that ends without a verdict is more expensive than a run that fails. A full suite run leaks
roughly 3,000 file descriptors, and on a host with the common 1,024 soft limit it dies around 38% with `Too many open files` and
no summary line (issue #651). These tests drive the real shell function in a subshell, because the thing being guaranteed is a
shell behaviour, not a Python one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "ci" / "ensure-fd-limit.sh"
GATE = ROOT / "scripts" / "ci" / "unit-gates.sh"
PYPROJECT = ROOT / "pyproject.toml"


def run_shell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=ROOT)


def run_with(soft: str | None = None, hard: str | None = None, floor: str | None = None) -> subprocess.CompletedProcess[str]:
    """Source the guard in a subshell with forced limits.

    Soft is set before hard: lowering the hard limit below the current soft limit is an error, and on Linux
    `ulimit -n unlimited` clamps to the hard limit rather than reporting the word.
    """
    prelude = f"export HERDR_FD_FLOOR={floor}; " if floor is not None else ""
    if soft is not None:
        prelude += f"ulimit -Sn {soft} 2>/dev/null || exit 90; "
    if hard is not None:
        prelude += f"ulimit -Hn {hard} 2>/dev/null || exit 90; "
    return run_shell(prelude + f'. "{GUARD}"; herdr_ensure_fd_limit')


def host_hard_limit() -> int | str:
    out = run_shell("ulimit -Hn").stdout.strip()
    return int(out) if out.isdigit() else out


def limits_can_be_raised_to_the_floor() -> bool:
    hard = host_hard_limit()
    return hard == "unlimited" or (isinstance(hard, int) and hard >= 16384)


needs_headroom = pytest.mark.skipif(
    not limits_can_be_raised_to_the_floor(), reason="host hard limit is below the floor, so nothing can be raised"
)



# ------------------------------------------------------------------ the guard's three outcomes ------------------------------------------

@needs_headroom
def test_a_low_soft_limit_is_raised_and_the_run_proceeds():
    result = run_with(soft="256")
    assert result.returncode == 0, result.stderr
    assert "raised" in result.stdout, result.stdout
    assert "floor" in result.stdout


def test_a_limit_already_past_the_floor_is_left_alone():
    result = run_with(soft="20000")
    if result.returncode == 90:
        pytest.skip("host will not set a 20,000 soft fd limit")
    assert result.returncode == 0, result.stderr
    assert "met" in result.stdout, result.stdout
    assert "raised" not in result.stdout, "a limit already past the floor must not be touched"


@needs_headroom
def test_a_limit_that_cannot_be_raised_far_enough_refuses_with_an_actionable_message():
    """The point of the guard: a clear refusal beats a truncated log that looks like a hang."""
    result = run_with(soft="512", hard="512")
    if result.returncode == 90:
        pytest.skip("host will not lower this process's fd limits")
    assert result.returncode == 1, (result.stdout, result.stderr)
    message = result.stderr
    assert "REFUSING" in message
    assert "Too many open files" in message, "the refusal must say what would otherwise happen"
    assert "ulimit -n" in message, "the refusal must name the fix"


@needs_headroom
def test_the_floor_is_overridable_for_hosts_that_genuinely_need_less():
    result = run_with(soft="512", hard="512", floor="128")
    if result.returncode == 90:
        pytest.skip("host will not lower this process's fd limits")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "met" in result.stdout


def test_an_unlimited_soft_limit_is_reported_and_accepted():
    """Linux clamps `ulimit -Sn unlimited` to the hard limit, so accept either spelling of "plenty"."""
    result = run_shell(f'ulimit -Sn unlimited 2>/dev/null || exit 90; . "{GUARD}"; herdr_ensure_fd_limit')
    if result.returncode == 90:
        pytest.skip("host will not set an unlimited soft fd limit")
    assert result.returncode == 0, result.stderr
    reported = result.stdout.split(":", 1)[1].split("(")[0].strip()
    assert reported == "unlimited" or int(reported) >= 16384, result.stdout


def test_the_guard_never_lowers_a_limit_it_was_asked_to_check():
    before = run_shell('ulimit -Sn 20000 2>/dev/null || exit 90; ulimit -Sn').stdout.strip()
    if not before.isdigit():
        pytest.skip("cannot set a 20,000 soft fd limit on this host")
    result = run_shell(f'ulimit -Sn 20000 2>/dev/null; . "{GUARD}"; herdr_ensure_fd_limit >/dev/null; ulimit -Sn')
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) >= int(before), "raising must never reduce the limit"


# ------------------------------------------------------------------ the wiring that makes it count ---------------------------------------

def test_unit_gates_runs_the_guard_before_pytest():
    """A guard nobody calls is a comment. It must run before pytest, or it protects nothing."""
    script = GATE.read_text()
    sourced = script.index("ensure-fd-limit.sh")
    pytest_at = script.index("pytest")
    assert sourced < pytest_at, "the fd floor must be applied before the suite starts"
    assert re.search(r"herdr_ensure_fd_limit \|\| exit 1", script), "a refusal must stop the gate"


def test_the_guard_is_sourceable_without_running():
    """journal.sh is sourced the same way; sourcing must not execute the check or print."""
    result = run_shell(f'. "{GUARD}"; echo "sourced-clean"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sourced-clean"


def test_pyproject_declares_a_hang_ceiling():
    """pytest-timeout is what turns a stall into a test name; without it a hang costs the whole pipeline."""
    text = PYPROJECT.read_text()
    assert re.search(r"^timeout = \d+", text, re.MULTILINE), "no per-test timeout configured"
    seconds = int(re.search(r"^timeout = (\d+)", text, re.MULTILINE).group(1))
    assert 300 <= seconds <= 1800, f"timeout {seconds}s is neither a hang detector nor generous enough"
    assert "pytest-timeout" in text, "the dev extra must install the plugin the ini option needs"


def test_the_measured_leak_is_recorded_where_the_next_person_will_read_it():
    """The number is the argument. If it is ever fixed at the source, this test is the place that gets updated."""
    assert "3,000" in GUARD.read_text() or "3000" in GUARD.read_text()
