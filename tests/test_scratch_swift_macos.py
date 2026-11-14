"""Native macOS SwiftPM acceptance; selected explicitly by the macOS CI job."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from herdr_engineering.scratch_reaper import Kind, Policy, RunStatus, run_once


@pytest.mark.integration
def test_native_swift_custom_scratch_outputs_are_reclaimed_and_rebuildable(tmp_path: Path) -> None:
    package = tmp_path / "package"
    source = package / "Sources/Core/core.swift"
    source.parent.mkdir(parents=True)
    (package / "Package.swift").write_text(
        '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name:"RetentionProof", targets:[.target(name:"Core")])\n'
    )
    source.write_text("public func answer() -> Int { 42 }\n")
    root = tmp_path / f"claude-{os.getuid()}"
    scratch = root / "-fixture/01234567-89ab-4cde-8fab-0123456789ab/scratchpad/custom-sp.1"
    environment = dict(
        os.environ,
        CLANG_MODULE_CACHE_PATH=str(tmp_path / "clang-modules"),
        SWIFTPM_MODULECACHE_OVERRIDE=str(tmp_path / "swift-modules"),
    )
    command = ["swift", "build", "--package-path", str(package), "--scratch-path", str(scratch)]
    built = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    assert (scratch / "workspace-state.json").exists()
    source.write_text("public func answer() -> Int { 43 } // unique edit\n")
    expected = source.read_bytes()
    report = run_once(
        root,
        tmp_path / "quarantine",
        tmp_path / "state",
        Policy(ttl_seconds=0, idle_seconds=0, min_free_bytes=0),
        apply=True,
        gate_state=tmp_path / "gate",
        live_roots=[],
    )
    assert report.status is RunStatus.OK, report.errors
    assert any(c.kind is Kind.SWIFT for c in report.removed)
    assert report.removed_bytes > 0
    assert (scratch / "workspace-state.json").exists()
    assert source.read_bytes() == expected
    rebuilt = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert source.read_bytes() == expected
