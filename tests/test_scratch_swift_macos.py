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


@pytest.mark.integration
def test_tmp_alias_argv_reference_is_protected_by_canonical_root_inspection(tmp_path: Path) -> None:
    import plistlib
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="herdr-tmp-alias-", dir="/tmp") as folder:
        base = Path(folder)
        assert str(base.resolve()).startswith("/private/tmp/")
        root = base / f"claude-{os.getuid()}"
        anchor = root / "-fixture/01234567-89ab-4cde-8fab-0123456789ab/scratchpad/dd"
        (anchor / "Build/Products").mkdir(parents=True)
        (anchor / "Build/Products/output.o").write_bytes(b"generated")
        (anchor / "info.plist").write_bytes(
            plistlib.dumps(
                {"WorkspacePath": str(tmp_path / "source/Project.xcodeproj/project.xcworkspace")}
            )
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                'import time;print("ready",flush=True);time.sleep(300)',
                str(anchor),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None and child.stdout.readline().strip() == "ready"
            report = run_once(
                root,
                base / "quarantine",
                base / "state",
                Policy(ttl_seconds=0, idle_seconds=0, min_free_bytes=0),
                apply=True,
                gate_state=base / "gate",
                live_roots=[],
            )
            assert not report.removed
            assert (anchor / "Build/Products/output.o").exists()
        finally:
            child.terminate()
            child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()
