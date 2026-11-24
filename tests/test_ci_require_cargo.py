"""scripts/ci/require-cargo.sh: module-gates.sh must not report green in CI when its Rust-toolchain gates cannot run."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "require-cargo.sh"


def run(tmp_path: Path, *, with_cargo: bool, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the real script with a PATH holding only bash plumbing, and the host's real `cargo`/`rustc` only when asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "env", "bash"):
        found = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if found:
            (bin_dir / tool).symlink_to(found)
    if with_cargo:
        for tool in ("cargo", "rustc"):
            real = shutil.which(tool)
            if real is None:
                pytest.skip("no real Rust toolchain on this host")
            (bin_dir / tool).symlink_to(real)
    base = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    return subprocess.run([str(Path(subprocess.run(["bash", "-c", "command -v bash"], capture_output=True, text=True).stdout.strip())), str(SCRIPT)],
                          env={**base, **env}, capture_output=True, text=True)


def test_toolchain_present_runs_the_gates(tmp_path):
    assert run(tmp_path, with_cargo=True, CI="true").returncode == 0


def test_ci_without_a_toolchain_fails(tmp_path):
    result = run(tmp_path, with_cargo=False, CI="true")
    assert result.returncode == 1, result.stderr
    assert "HERDR_ALLOW_NO_CARGO=1" in result.stderr


def test_woodpecker_event_without_a_toolchain_fails(tmp_path):
    assert run(tmp_path, with_cargo=False, CI_PIPELINE_EVENT="pull_request").returncode == 1


def test_ci_with_an_explicit_opt_out_skips_loudly(tmp_path):
    result = run(tmp_path, with_cargo=False, CI="true", HERDR_ALLOW_NO_CARGO="1")
    assert result.returncode == 2
    assert "SKIPPED" in result.stderr


def test_a_local_run_without_a_toolchain_skips_loudly(tmp_path):
    result = run(tmp_path, with_cargo=False)
    assert result.returncode == 2
    assert "SKIPPED" in result.stderr


def test_module_gates_calls_the_guard_and_does_not_skip_on_its_own():
    text = (ROOT / "scripts" / "ci" / "module-gates.sh").read_text()
    assert "scripts/ci/require-cargo.sh" in text
    assert 'command -v cargo' not in text
