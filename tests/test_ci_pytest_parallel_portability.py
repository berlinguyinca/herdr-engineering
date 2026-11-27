"""Regression for #936: the CPU-quota tests must collect and run on every platform, not only Linux.

`tests/test_ci_pytest_parallel.py` reads the real CPU affinity at module scope, and `os.sched_getaffinity` does not exist on
macOS. That raised during collection, and because a collection error aborts the whole run, a laptop could not run any subset of
the suite without `--ignore`. The tests that pin CPUs (`taskset`, Linux-only) likewise had no way to say "not here".

These tests load that module the way the suite does and drive the real helper, so they hold on either platform: on Linux they
assert the affinity number is the real one, and everywhere they assert that an impossible CPU count is reported as a skip
instead of an error.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "test_ci_pytest_parallel.py"


def _load():
    """The module under test, executed for its module scope like any other collected file."""
    spec = importlib.util.spec_from_file_location("ci_pytest_parallel_under_test", TARGET)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cpu_quota_module_collects_on_this_platform() -> None:
    """`--collect-only` must succeed here. Before #936 this returned non-zero on macOS and took the whole run down with it."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(TARGET)],
        cwd=ROOT,
        shell=False,
        capture_output=True,
        text=True,
        timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    report = f"{done.stdout[-3000:]}\n{done.stderr[-1000:]}"
    assert done.returncode == 0, f"collection did not succeed on {sys.platform}:\n{report}"
    assert "ERROR" not in done.stdout and "sched_getaffinity" not in done.stdout, report


def test_the_module_exposes_a_visible_cpu_count_without_affinity_support() -> None:
    """Module scope must define the quota basis on any platform, so `Q` and the skip markers below it still compute."""
    module = _load()
    assert module.VISIBLE >= 1
    assert 0 <= module.Q <= module.VISIBLE, (
        "the largest quota that must shrink workers cannot exceed the visible CPUs"
    )
    if hasattr(os, "sched_getaffinity"):
        assert module.VISIBLE == len(os.sched_getaffinity(0)), (
            "on Linux the quota basis must stay the real affinity mask"
        )
    else:
        assert module.VISIBLE == (os.cpu_count() or 1), (
            "off Linux the quota basis falls back to the CPU count"
        )


def test_pinning_an_unreachable_cpu_count_skips_instead_of_erroring(tmp_path: Path) -> None:
    """Asking for more CPUs than this process may use must be a skip. On macOS it must skip for the same reason: no affinity
    call and no `taskset` binary, rather than an AttributeError escaping through a helper."""
    module = _load()
    impossible = module.VISIBLE + 10_000
    with pytest.raises(pytest.skip.Exception) as skipped:
        module.args_with(cpus=impossible, HERDR_PYTEST_QUOTA="100000 100000")
    reason = str(skipped.value)
    if sys.platform != "linux" or shutil.which("taskset") is None:
        assert "taskset" in reason or "affinity" in reason, (
            f"the skip must name what this host lacks: {reason}"
        )


def test_a_pin_larger_than_the_affinity_mask_still_names_the_shortfall() -> None:
    """The original Linux skip path must survive the portability change: asking for one CPU more than the mask allows is a skip
    that says how many CPUs this process may use, not an error and not a silent pass."""
    module = _load()
    if (
        not hasattr(os, "sched_getaffinity")
        or shutil.which("taskset") is None
        or module.VISIBLE < 2
    ):
        pytest.skip("needs Linux affinity, taskset, and at least 2 visible CPUs")
    with pytest.raises(pytest.skip.Exception) as skipped:
        module.args_with(cpus=module.VISIBLE + 1, HERDR_PYTEST_QUOTA="100000 100000")
    assert "allowed CPUs" in str(skipped.value), str(skipped.value)
