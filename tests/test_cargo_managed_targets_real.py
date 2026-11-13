"""Automatic target placement tested with two actual Git worktrees and Rust tests."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from herdr_engineering.cargo_host import install


@pytest.mark.integration
def test_two_worktrees_receive_isolated_managed_targets(tmp_path: Path):
    cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
    if cargo is None or rustc is None:
        pytest.skip("real Cargo and rustc required")
    home = tmp_path / "home"
    install(home, Path(cargo), Path(sys.executable))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"fabric_build_cache": {"enabled": True,
                       "local": {"manage_targets": True, "min_free_disk_percent": 0, "capacity_gib": 1}}}))
    env = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"), "RUSTC": rustc,
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "HERDR_ENGINEERING_CONFIG": str(config), "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    for key in ("RUSTC_WRAPPER", "SCCACHE_SERVER_PORT", "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"):
        env.pop(key, None)
    first = tmp_path / "first"
    (first / "src").mkdir(parents=True)
    (first / "Cargo.toml").write_text('[package]\nname="workspace-isolation"\nversion="0.1.0"\nedition="2021"\n')
    (first / "src/lib.rs").write_text('pub fn answer()->u32{1}\n#[test]fn correct(){assert_eq!(answer(),1)}\n')
    subprocess.run(["git", "init", "-q", "-b", "fixture", str(first)], check=True)
    subprocess.run(["git", "-C", str(first), "add", "Cargo.toml", "src/lib.rs"], check=True)
    subprocess.run(["git", "-C", str(first), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"], check=True)
    second = tmp_path / "second"
    subprocess.run(["git", "-C", str(first), "worktree", "add", "-qb", "second", str(second)], check=True)
    (second / "src/lib.rs").write_text('pub fn answer()->u32{2}\n#[test]fn correct(){assert_eq!(answer(),2)}\n')
    for workspace in (first, second):
        result = subprocess.run([str(home / ".local/bin/cargo"), "test", "--offline"], cwd=workspace,
                                env=env, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert (workspace / "target").is_symlink()
        assert (workspace / "target/debug").is_dir()
    outer = subprocess.run([str(Path(sys.executable).parent / "herdr-eng"), "build-coordinate",
                            "--max-load", "0", "--", str(home / ".local/bin/cargo"), "test", "--offline"],
                           cwd=first, env=env, capture_output=True, text=True, timeout=90)
    assert outer.returncode == 0, outer.stderr
    assert (first / "target").is_symlink()
    markers = list((tmp_path / "state/build-gates/targets").glob("*/.herdr-cargo-target.json"))
    assert len(markers) == 2
    assert {json.loads(marker.read_text())["workspace"] for marker in markers} == {str(first), str(second)}
    first_marker = next(marker for marker in markers if json.loads(marker.read_text())["workspace"] == str(first))
    cleaned = subprocess.run([str(home / ".local/bin/cargo"), "clean"], cwd=first,
                             env=env, capture_output=True, text=True, timeout=30)
    assert cleaned.returncode == 0, cleaned.stderr
    assert not (first_marker.parent / "debug").exists()
    assert any((marker.parent / "debug").exists() for marker in markers if marker != first_marker)
