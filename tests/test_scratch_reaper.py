"""Real-filesystem safety and retention acceptance for agent scratch output."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_engineering.scratch_reaper import Candidate, Policy, Report, RunStatus, run_once

type Tree = tuple[Path, Path, Path, Path, Path]

SESSION = "01234567-89ab-4cde-8fab-0123456789ab"


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    root = tmp_path / f"claude-{os.getuid()}"
    session = root / "-project" / SESSION
    derived = session / "scratchpad" / "dd1.numbered"
    (derived / "Build/Products").mkdir(parents=True)
    (derived / "Build/Intermediates.noindex").mkdir()
    (derived / "Build/Products/output.o").write_bytes(b"generated" * 1024)
    (derived / "ModuleCache.noindex").mkdir()
    (derived / "ModuleCache.noindex/module.pcm").write_bytes(b"module" * 1024)
    (derived / "Logs").mkdir()
    (derived / "Logs/build.log").write_text("retain diagnostic evidence")
    (derived / "TestResults").mkdir()
    (derived / "TestResults/report.json").write_text('{"passed":true}')
    with (derived / "info.plist").open("wb") as stream:
        plistlib.dump(
            {"WorkspacePath": str(session / "source/Project.xcodeproj/project.xcworkspace")}, stream
        )
    (session / "notes.md").write_text("unique session work")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    return root, session, derived, quarantine, tmp_path / "state"


def policy() -> Policy:
    return Policy(ttl_seconds=0, idle_seconds=0, max_bytes=32 * 2**30, min_free_bytes=0)


def sweep(
    tree: tuple[Path, Path, Path, Path, Path], settings: Policy, *, apply: bool = False
) -> Report:
    root, _session, _derived, quarantine, state = tree
    return run_once(
        root,
        quarantine,
        state,
        settings,
        apply=apply,
        gate_state=state.parent / "build-gates",
        live_roots=[],
    )


def paths(items: Sequence[Candidate]) -> set[Path]:
    return {Path(item.path) for item in items}


def test_default_dry_run_finds_numbered_derived_data_but_changes_nothing(tree: Tree) -> None:
    report = sweep(tree, policy())
    root, session, derived, _quarantine, _state = tree
    assert report.status is RunStatus.DRY_RUN
    assert derived / "Build" in paths(report.would_remove)
    assert derived / "ModuleCache.noindex" in paths(report.would_remove)
    assert (derived / "Build/Products/output.o").exists()
    assert (session / "notes.md").read_text() == "unique session work"
    assert root.is_dir()


def test_live_session_retains_notes_while_idle_build_output_is_removed(
    tree: Tree, tmp_path: Path
) -> None:
    root, session, derived, quarantine, state = tree
    logs = tmp_path / "session-logs"
    logs.mkdir()
    (logs / (SESSION + ".jsonl")).write_text(
        '{"type":"user","message":{"role":"user","content":"working"}}\n'
    )
    report = run_once(
        root,
        quarantine,
        state,
        replace(policy(), session_ttl_seconds=0),
        apply=True,
        gate_state=tmp_path / "build-gates",
        live_roots=[logs],
    )
    assert derived / "Build" in paths(report.removed)
    assert not (derived / "Build").exists()
    assert (session / "notes.md").exists()
    assert (derived / "info.plist").exists()
    assert (derived / "Logs/build.log").exists()
    assert (derived / "TestResults/report.json").exists()
    assert not list(quarantine.iterdir())


def test_keep_marker_on_session_protects_disposable_descendants(tree: Tree) -> None:
    _root, session, derived, _q, _s = tree
    (session / ".keep").touch()
    report = sweep(tree, policy(), apply=True)
    assert not report.removed
    assert (derived / "Build/Products/output.o").exists()
    assert report.protected


def test_name_only_directory_is_never_treated_as_derived_data(tree: Tree) -> None:
    root, _session, _derived, _q, _s = tree
    unknown = root / "dd2.looks-generated"
    (unknown / "Build/Products").mkdir(parents=True)
    (unknown / "Build/Products/source.swift").write_text("unique source")
    report = sweep(tree, policy(), apply=True)
    assert unknown / "Build" not in paths(report.removed)
    assert (unknown / "Build/Products/source.swift").exists()


def test_pressure_evicts_idle_output_before_normal_ttl(tree: Tree) -> None:
    settings = replace(policy(), ttl_seconds=86400)
    initial = sweep(tree, settings, apply=True)
    assert not initial.removed
    report = sweep(tree, replace(settings, max_bytes=0), apply=True)
    assert report.removed
    assert not (tree[2] / "Build").exists()
    assert (tree[2] / "Logs/build.log").exists()


def test_pressure_never_overrides_recent_output_grace(tree: Tree) -> None:
    settings = replace(policy(), ttl_seconds=86400, idle_seconds=3600, max_bytes=0)
    report = sweep(tree, settings, apply=True)
    assert report.status is RunStatus.PRESSURE_BLOCKED
    assert not report.removed
    assert report.protected_bytes > 0
    assert (tree[2] / "Build/Products/output.o").exists()


@pytest.mark.parametrize("mode", ["open", "cwd", "argv"])
def test_real_process_use_protects_the_derived_data_anchor(tree: Tree, mode: str) -> None:
    _root, _session, derived, _q, _s = tree
    script = "import sys,time; f=open(sys.argv[1],'rb') if sys.argv[2]=='open' else None; print('ready',flush=True);time.sleep(300)"
    reference = derived / "Build/Products/output.o" if mode == "open" else derived
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(reference), mode],
        cwd=derived if mode == "cwd" else None,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        report = sweep(tree, policy(), apply=True)
        assert not report.removed
        assert (derived / "Build/Products/output.o").exists()
        assert report.protected
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def test_internal_symlink_is_unlinked_without_touching_its_external_target(
    tree: Tree, tmp_path: Path
) -> None:
    external = tmp_path / "outside-source.txt"
    external.write_text("must survive")
    (tree[2] / "Build/Products/link").symlink_to(external)
    report = sweep(tree, policy(), apply=True)
    assert report.removed
    assert external.read_text() == "must survive"


def test_symlink_component_is_not_followed(tree: Tree, tmp_path: Path) -> None:
    derived = tree[2]
    external = tmp_path / "external-build"
    (derived / "Build").rename(external)
    (derived / "Build").symlink_to(external, target_is_directory=True)
    report = sweep(tree, policy(), apply=True)
    assert derived / "Build" not in paths(report.removed)
    assert (external / "Products/output.o").exists()


def test_invalid_policy_is_rejected_before_mutation(tree: Tree) -> None:
    with pytest.raises(ValueError):
        sweep(tree, replace(policy(), min_free_bytes=-1), apply=True)
    assert (tree[2] / "Build/Products/output.o").exists()


def test_unreadable_registry_fails_closed(tree: Tree) -> None:
    gate = tree[4].parent / "build-gates"
    gate.mkdir()
    (gate / "build-registry.db").write_bytes(b"not a sqlite database")
    report = sweep(tree, policy(), apply=True)
    assert report.status is RunStatus.ERROR
    assert report.errors
    assert not report.removed
    assert (tree[2] / "Build/Products/output.o").exists()


def _claim_fixture(tree: Tree) -> tuple[Candidate, Path]:
    import json

    from herdr_engineering.scratch_reaper import _discover

    candidate = next(c for c in _discover(tree[0]) if c.path.name == "Build")
    retiring = tree[4] / "retiring"
    retiring.mkdir(parents=True)
    claim = retiring / ("a" * 32)
    candidate.path.rename(claim)
    claim.with_suffix(".json").write_text(
        json.dumps(
            {
                "root": str(candidate.root),
                "original": str(candidate.path),
                "device": candidate.device,
                "inode": candidate.inode,
            }
        )
    )
    return candidate, claim


def test_dry_run_never_restores_or_deletes_pending_claim(tree: Tree) -> None:
    candidate, claim = _claim_fixture(tree)
    report = sweep(tree, policy())
    assert report.status is RunStatus.ERROR
    assert claim.exists() and not candidate.path.exists()


def test_apply_recovers_interrupted_claim_before_new_cleanup(tree: Tree) -> None:
    candidate, claim = _claim_fixture(tree)
    report = sweep(tree, replace(policy(), ttl_seconds=86400), apply=True)
    assert report.status is RunStatus.OK
    assert candidate.path.is_dir() and not claim.exists()
    assert not claim.with_suffix(".json").exists()
    assert (tree[2] / "Logs/build.log").exists()


def test_recovery_never_overwrites_recreated_source(tree: Tree) -> None:
    candidate, claim = _claim_fixture(tree)
    candidate.path.mkdir()
    (candidate.path / "new-work.txt").write_text("unique replacement")
    report = sweep(tree, policy(), apply=True)
    assert report.status is RunStatus.ERROR
    assert claim.exists()
    assert (candidate.path / "new-work.txt").read_text() == "unique replacement"


def test_replaced_inode_is_not_deleted(tree: Tree) -> None:
    from herdr_engineering.scratch_reaper import _discover, _retire

    candidate = next(c for c in _discover(tree[0]) if c.path.name == "Build")
    saved = candidate.path.with_name("original-cache")
    candidate.path.rename(saved)
    candidate.path.mkdir()
    (candidate.path / "new-source.txt").write_text("must survive")
    (tree[4] / "retiring").mkdir(parents=True)
    result = _retire(candidate, tree[4], tree[4].parent / "build-gates", __import__("time").time())
    assert result == "changed_identity"
    assert (candidate.path / "new-source.txt").exists()
    assert saved.exists()


def test_state_and_quarantine_symlinks_are_rejected(tree: Tree, tmp_path: Path) -> None:
    root, _session, _derived, _q, state = tree
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "quarantine-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        run_once(root, link, state, policy(), apply=True, gate_state=tmp_path / "gate")
    assert (tree[2] / "Build/Products/output.o").exists()


def test_missing_process_tools_fails_closed_in_real_cli(tree: Tree, tmp_path: Path) -> None:
    import json

    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.scratch_reaper",
            "--root",
            str(tree[0]),
            "--quarantine",
            str(tree[3]),
            "--state-dir",
            str(tree[4]),
            "--gate-state-dir",
            str(tmp_path / "gate"),
            "--ttl-minutes",
            "0",
            "--apply",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, PATH=str(empty)),
        timeout=30,
    )
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "error" and report["errors"]
    assert (tree[2] / "Build/Products/output.o").exists()


def test_edited_dependency_metadata_is_retained(tree: Tree) -> None:
    package = tree[2] / "SourcePackages"
    package.mkdir()
    (package / "workspace-state.json").write_text(
        '{"version":6,"object":{"dependencies":[{"state":{"name":"edited"}}]}}'
    )
    report = sweep(tree, policy(), apply=True)
    assert package not in paths(report.removed)
    assert (package / "workspace-state.json").exists()
    assert any(p.reason == "edited_dependency" for p in report.protected)


def test_general_quarantine_admission_is_bounded_in_dry_run(tmp_path: Path) -> None:
    from herdr_engineering.scratch_safety import inspect_tree

    root = tmp_path / f"claude-{os.getuid()}"
    q = tmp_path / "quarantine"
    q.mkdir()
    for number in [1, 2]:
        session = root / "-project" / f"01234567-89ab-4cde-8fab-{number:012d}"
        session.mkdir(parents=True)
        with (session / "scratch.bin").open("wb") as stream:
            stream.write(os.urandom(128 * 1024))
            stream.flush()
            os.fsync(stream.fileno())
    one = inspect_tree(next((root / "-project").iterdir()))
    assert one.reserved_bytes >= 128 * 1024
    settings = replace(policy(), session_ttl_seconds=0, quarantine_max_bytes=200 * 1024)
    report = run_once(
        root, q, tmp_path / "state", settings, gate_state=tmp_path / "gate", live_roots=[]
    )
    assert len(report.quarantined) == 1
    assert len(list((root / "-project").iterdir())) == 2
    assert not list(q.iterdir())


def _git_run(path: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(path),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "condition", ["clean", "dirty", "unpushed", "stash", "no_remote", "ignored", "assumed", "skip"]
)
def test_nested_dependency_repository_guards_use_real_git(
    tree: Tree, tmp_path: Path, condition: str
) -> None:
    remote = tmp_path / "remote.git"
    _git_run(tmp_path, "init", "--bare", str(remote))
    checkout = tree[2] / "SourcePackages/checkouts/package"
    checkout.mkdir(parents=True)
    _git_run(checkout, "init")
    _git_run(checkout, "config", "user.name", "Retention Test")
    _git_run(checkout, "config", "user.email", "retention@localhost")
    source = checkout / "source.txt"
    source.write_text("published source")
    if condition == "ignored":
        (checkout / ".gitignore").write_text("private-notes.bin\n")
    _git_run(checkout, "add", ".")
    _git_run(checkout, "commit", "-m", "published")
    _git_run(checkout, "remote", "add", "origin", str(remote))
    _git_run(checkout, "push", "-u", "origin", "HEAD")
    if condition == "ignored":
        (checkout / "private-notes.bin").write_text("unique ignored work")
    if condition in {"assumed", "skip"}:
        _git_run(
            checkout,
            "update-index",
            "--assume-unchanged" if condition == "assumed" else "--skip-worktree",
            "source.txt",
        )
        source.write_text("unique hidden source edit")
    if condition in {"dirty", "unpushed", "stash"}:
        source.write_text("unique unpublished work")
    if condition == "unpushed":
        _git_run(checkout, "commit", "-am", "local only")
    if condition == "stash":
        _git_run(checkout, "stash", "push")
    if condition == "no_remote":
        _git_run(checkout, "remote", "remove", "origin")
    report = sweep(tree, policy(), apply=True)
    if condition == "clean":
        assert not checkout.exists()
        assert tree[2] / "SourcePackages" in paths(report.removed)
    else:
        assert checkout.exists()
        assert tree[2] / "SourcePackages" not in paths(report.removed)
    assert (tree[2] / "Logs/build.log").exists()


def test_general_session_quarantine_retains_recovery_data_then_expires(tree: Tree) -> None:
    settings = replace(policy(), ttl_seconds=86400, session_ttl_seconds=0)
    report = sweep(tree, settings, apply=True)
    assert len(report.quarantined) == 1
    assert not tree[1].exists()
    recovered = list(tree[3].glob("*/*/notes.md"))
    assert len(recovered) == 1 and recovered[0].read_text() == "unique session work"
    expired = sweep(tree, replace(settings, quarantine_ttl_seconds=0), apply=True)
    assert expired.removed
    assert not recovered[0].exists()


def test_quarantine_day_symlink_never_moves_data_outside_managed_roots(
    tree: Tree, tmp_path: Path
) -> None:
    import time

    outside = tmp_path / "outside-quarantine"
    outside.mkdir()
    (tree[3] / time.strftime("%Y-%m-%d")).symlink_to(outside, target_is_directory=True)
    report = sweep(tree, replace(policy(), ttl_seconds=86400, session_ttl_seconds=0), apply=True)
    assert report.status is RunStatus.ERROR
    assert tree[1].is_dir()
    assert not list(outside.iterdir())


def test_retiring_directory_symlink_fails_closed(tree: Tree, tmp_path: Path) -> None:
    outside = tmp_path / "outside-retiring"
    outside.mkdir()
    tree[4].mkdir()
    (tree[4] / "retiring").symlink_to(outside, target_is_directory=True)
    report = sweep(tree, policy(), apply=True)
    assert report.status is RunStatus.ERROR
    assert not report.removed
    assert (tree[2] / "Build/Products/output.o").exists()
    assert not list(outside.iterdir())


def test_tracked_build_output_is_not_discarded(tree: Tree) -> None:
    anchor = tree[2]
    _git_run(anchor, "init")
    _git_run(anchor, "add", "Build/Products/output.o")
    report = sweep(tree, policy(), apply=True)
    assert anchor / "Build" not in paths(report.removed)
    assert (anchor / "Build/Products/output.o").exists()
    assert any(p.reason == "tracked_or_unverifiable_output" for p in report.protected)


def test_process_reference_boundary_does_not_pin_similarly_named_sibling(tree: Tree) -> None:
    sibling = tree[2].with_name(tree[2].name + "-sibling")
    sibling.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            'import time;print("ready",flush=True);time.sleep(300)',
            str(sibling),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        report = sweep(tree, policy(), apply=True)
        assert tree[2] / "Build" in paths(report.removed)
        assert sibling.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


@pytest.mark.parametrize("apply", [True, False])
def test_real_shared_build_lock_blocks_cleanup(tree: Tree, tmp_path: Path, apply: bool) -> None:
    gate = tmp_path / "build-gates"
    gate.mkdir()
    lock = gate / "build-maintenance.lock"
    script = 'import fcntl,sys,time;f=open(sys.argv[1],"a");fcntl.flock(f,fcntl.LOCK_SH);print("ready",flush=True);time.sleep(300)'
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        report = sweep(tree, policy(), apply=apply)
        assert report.status is (RunStatus.BUSY if apply else RunStatus.DRY_RUN)
        assert not report.removed
        assert (tree[2] / "Build/Products/output.o").exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def test_live_lease_protects_its_source_project(tree: Tree, tmp_path: Path) -> None:
    import socket

    from herdr_engineering.build_registry import BuildRun

    source = tree[1] / "source"
    source.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(300)"], cwd=source)
    run = BuildRun.open(
        tmp_path / "build-gates",
        cwd=str(source),
        cmd_kind="other",
        host=socket.gethostname(),
        cmd=["python"],
        ttl_s=60,
    )
    try:
        run.on_spawn(child.pid)
        report = sweep(tree, policy(), apply=True)
        assert not report.removed
        assert any(p.reason == "active_lease" for p in report.protected)
    finally:
        run.halt()
        child.terminate()
        result = child.wait(timeout=5)
        run.on_exit(128 + abs(result), signaled=True)


def test_pending_live_session_receipt_is_rechecked_before_retirement(
    tree: Tree, tmp_path: Path
) -> None:
    import time

    from herdr_engineering.scratch_reaper import Kind, _retire
    from herdr_engineering.scratch_safety import inspect_tree

    info = inspect_tree(tree[1])
    candidate = Candidate(
        tree[1],
        tree[1],
        tree[0],
        Kind.SESSION,
        info.newest,
        info.allocated_bytes,
        info.device,
        info.inode,
    )
    live = tmp_path / "live"
    live.mkdir()
    (live / (SESSION + ".jsonl")).write_text('{"type":"user"}\n')
    (tree[4] / "retiring").mkdir(parents=True)
    reason = _retire(candidate, tree[4], tmp_path / "gate", time.time(), live_roots=[live])
    assert reason == "live_session"
    assert (tree[1] / "notes.md").exists()


def test_swiftpm_output_is_reclaimed_without_dependency_state(tree: Tree) -> None:
    package = tree[1] / "swift-package"
    package.mkdir()
    (package / "Package.swift").write_text("// swift-tools-version:5.9\n")
    scratch = package / ".build"
    (scratch / "arm64-apple-macosx/debug").mkdir(parents=True)
    (scratch / "arm64-apple-macosx/debug/output.o").write_bytes(b"compiled")
    (scratch / "workspace-state.json").write_text(
        '{"version":6,"object":{"dependencies":[],"artifacts":[]}}'
    )
    (scratch / "checkouts").mkdir()
    (scratch / "checkouts/unique-edit.txt").write_text("keep dependency data")
    report = sweep(tree, policy(), apply=True)
    assert scratch / "arm64-apple-macosx" in paths(report.removed)
    assert (scratch / "workspace-state.json").exists()
    assert (scratch / "checkouts/unique-edit.txt").exists()


def test_cargo_recognition_is_revalidated_before_removal(tree: Tree) -> None:
    import time

    from herdr_engineering.scratch_reaper import _discover, _retire

    package = tree[1] / "rust-package"
    (package / "target").mkdir(parents=True)
    (package / "Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
    marker = package / "target/.rustc_info.json"
    marker.write_text("{}")
    candidate = next(c for c in _discover(tree[0]) if c.path == package / "target")
    marker.unlink()
    (candidate.path / "unique.txt").write_text("no longer generated output")
    (tree[4] / "retiring").mkdir(parents=True)
    reason = _retire(candidate, tree[4], tree[4].parent / "gate", time.time())
    assert reason == "changed_format"
    assert (candidate.path / "unique.txt").exists()


@pytest.mark.integration
def test_native_cargo_output_is_removed_while_dirty_source_and_cli_evidence_survive(
    tmp_path: Path,
) -> None:
    import json
    import shutil

    from herdr_engineering.scratch_safety import inspect_tree

    rustup = shutil.which("rustup")
    assert rustup is not None, "native compiler acceptance requires rustup"
    cargo = subprocess.check_output([rustup, "which", "cargo"], text=True).strip()
    root = tmp_path / f"claude-{os.getuid()}"
    repo = root / "-fixture" / SESSION / "scratchpad/project"
    (repo / "src").mkdir(parents=True)
    manifest = repo / "Cargo.toml"
    manifest.write_text(
        '[package]\nname="scratch_retention_proof"\nversion="0.1.0"\nedition="2021"\n'
    )
    source = repo / "src/lib.rs"
    source.write_text("pub fn answer() -> u32 { 42 }\n")
    _git_run(repo, "init")
    _git_run(repo, "config", "user.name", "Retention Test")
    _git_run(repo, "config", "user.email", "retention@localhost")
    _git_run(repo, "add", ".")
    _git_run(repo, "commit", "-m", "source baseline")
    compiler = subprocess.run(
        [
            cargo,
            "check",
            "--offline",
            "--manifest-path",
            str(manifest),
            "--target-dir",
            str(repo / "target"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ, RUSTC_WRAPPER="", RUSTC_WORKSPACE_WRAPPER=""),
    )
    assert compiler.returncode == 0, compiler.stderr
    assert list((repo / "target").rglob("libscratch_retention_proof*.rmeta"))
    assert inspect_tree(repo / "target").allocated_bytes > 0
    source.write_text(source.read_text() + "pub fn unique_edit() -> bool { true }\n")
    expected = source.read_bytes()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.scratch_reaper",
            "--root",
            str(root),
            "--quarantine",
            str(tmp_path / "quarantine"),
            "--state-dir",
            str(tmp_path / "state"),
            "--gate-state-dir",
            str(tmp_path / "gate"),
            "--session-root",
            str(sessions),
            "--ttl-minutes",
            "0",
            "--min-free-gib",
            "0",
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["removed_count"] == 1
    assert report["removed_bytes"] > 0
    assert not (repo / "target").exists()
    assert source.read_bytes() == expected
    assert _git_run(repo, "status", "--porcelain").strip()
    assert (tmp_path / "state/status.json").exists()


@pytest.mark.parametrize(
    "marker", ["missing", "garbage", "non_object", "relative_workspace", "non_string"]
)
def test_ambiguous_derived_data_metadata_is_retained(tree: Tree, marker: str) -> None:
    info = tree[2] / "info.plist"
    if marker == "missing":
        info.unlink()
    elif marker == "garbage":
        info.write_bytes(b"not a plist")
    elif marker == "non_object":
        info.write_bytes(plistlib.dumps(["not", "metadata"]))
    else:
        info.write_bytes(
            plistlib.dumps(
                {"WorkspacePath": "relative/project" if marker == "relative_workspace" else 4}
            )
        )
    report = sweep(tree, policy(), apply=True)
    assert not report.removed
    assert (tree[2] / "Build/Products/output.o").exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, True])
def test_nonfinite_negative_and_boolean_policy_values_are_rejected(
    tree: Tree, value: float
) -> None:
    with pytest.raises(ValueError):
        sweep(tree, replace(policy(), ttl_seconds=value), apply=True)
    assert (tree[2] / "Build/Products/output.o").exists()


def test_missing_scratch_root_is_an_explicit_noop(tmp_path: Path) -> None:
    report = run_once(
        tmp_path / f"claude-{os.getuid()}",
        tmp_path / "quarantine",
        tmp_path / "state",
        policy(),
        apply=True,
        gate_state=tmp_path / "gate",
    )
    assert report.status is RunStatus.OK
    assert report.removed_bytes == 0
    assert report.to_dict()["observed_reclaimed_bytes"] is None


def test_state_cannot_be_inside_scratch(tree: Tree) -> None:
    with pytest.raises(ValueError):
        run_once(
            tree[0],
            tree[3],
            tree[1] / "state",
            policy(),
            apply=True,
            gate_state=tree[4].parent / "gate",
        )
    assert (tree[2] / "Build/Products/output.o").exists()


def test_floor_larger_than_filesystem_is_rejected_before_data_removal(tree: Tree) -> None:
    import shutil

    report = sweep(
        tree, replace(policy(), min_free_bytes=shutil.disk_usage(tree[0]).total + 1), apply=True
    )
    assert report.status is RunStatus.ERROR and report.errors
    assert not report.removed
    assert (tree[2] / "Build/Products/output.o").exists()


def test_real_free_space_pressure_removes_idle_output_and_reports_unmet_floor(tree: Tree) -> None:
    import shutil

    floor = shutil.disk_usage(tree[0]).free + 2**30
    report = sweep(tree, replace(policy(), ttl_seconds=86400, min_free_bytes=floor), apply=True)
    assert report.removed
    assert report.status is RunStatus.PRESSURE_BLOCKED
    assert report.free_before is not None and report.free_after is not None
    assert (tree[1] / "notes.md").exists()


@pytest.mark.parametrize("change", ["keep", "activity"])
def test_actual_post_claim_writer_causes_restoration(tree: Tree, change: str) -> None:
    # A real competing process waits for the rename, then changes the claimed
    # directory during process reinspection. No injected callback or fake clock.
    script = """import pathlib,sys,time
state=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+30
print("ready",flush=True)
while time.monotonic()<deadline:
 if state.exists():
  dirs=[p for p in state.iterdir() if p.is_dir()]
  if dirs:
   (dirs[0]/sys.argv[2]).write_text("new work")
   raise SystemExit(0)
 time.sleep(.01)
raise SystemExit(2)
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tree[4] / "retiring"),
            ".keep" if change == "keep" else "new-output.o",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        report = sweep(tree, policy(), apply=True)
        assert child.wait(timeout=5) == 0
        assert (tree[2] / "Build/Products/output.o").exists()
        assert (tree[2] / "Build" / (".keep" if change == "keep" else "new-output.o")).exists()
        assert any(
            p.reason == ("keep_marker" if change == "keep" else "recent_activity")
            for p in report.protected
        )
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def test_completed_receipt_is_cleaned_without_touching_application_data(tree: Tree) -> None:
    import json

    retiring = tree[4] / "retiring"
    retiring.mkdir(parents=True)
    receipt = retiring / ("b" * 32 + ".json")
    receipt.write_text(
        json.dumps(
            {"root": str(tree[0]), "original": str(tree[2] / "Build"), "device": 1, "inode": 1}
        )
    )
    report = sweep(tree, replace(policy(), ttl_seconds=86400), apply=True)
    assert report.status is RunStatus.OK
    assert not receipt.exists()
    assert (tree[2] / "Build/Products/output.o").exists()


def test_report_details_are_bounded_without_losing_aggregate_counts() -> None:
    import json

    from herdr_engineering.scratch_reaper import Kind

    path = Path("/tmp/" + ("nested/" * 500))
    candidate = Candidate(path, path, path, Kind.CARGO, 0, 4096, 1, 2)
    report = Report(RunStatus.OK, False, removed=[candidate] * 1000)
    data = report.to_dict()
    assert len(json.dumps(data, default=str).encode()) <= 1024**2
    assert data["removed_count"] == 1000
    assert isinstance(data["removed"], list)
    assert len(data["removed"]) < 1000


def test_unreadable_session_metadata_does_not_quarantine_unique_work(
    tree: Tree, tmp_path: Path
) -> None:
    live = tmp_path / "unreadable-session-logs"
    live.mkdir()
    (live / (SESSION + ".jsonl")).write_text('{"type":"user"}\n')
    live.chmod(0)
    try:
        report = run_once(
            tree[0],
            tree[3],
            tree[4],
            replace(policy(), ttl_seconds=86400, session_ttl_seconds=0),
            apply=True,
            gate_state=tmp_path / "gate",
            live_roots=[live],
        )
        assert report.status is RunStatus.ERROR
        assert not report.quarantined
        assert (tree[1] / "notes.md").exists()
    finally:
        live.chmod(0o700)


def test_coordination_state_cannot_be_retired_with_output(tree: Tree) -> None:
    with pytest.raises(ValueError):
        run_once(
            tree[0],
            tree[3],
            tree[4],
            policy(),
            apply=True,
            gate_state=tree[2] / "Build/gate",
            live_roots=[],
        )
    assert (tree[2] / "Build/Products/output.o").exists()


@pytest.mark.parametrize("option", ["--max-disposable-gib", "--min-free-gib"])
def test_cli_rejects_nonfinite_limits_without_traceback(tree: Tree, option: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.scratch_reaper",
            "--root",
            str(tree[0]),
            "--state-dir",
            str(tree[4]),
            option,
            "nan",
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert (tree[2] / "Build/Products/output.o").exists()


def test_scheduled_template_uses_isolated_runtime_and_bounded_output() -> None:
    template = Path(__file__).resolve().parents[1] / "deploy/agent-scratch-reaper.plist"
    data = plistlib.loads(template.read_bytes())
    arguments = data["ProgramArguments"]
    assert arguments[:4] == ["__PYTHON__", "-I", "-m", "herdr_engineering.scratch_reaper"]
    assert "--apply" in arguments
    assert "--gate-state-dir" in arguments
    assert data["StartInterval"] == 900
    assert data["StandardOutPath"] == "/dev/null"
    assert data["StandardErrorPath"] == "/dev/null"


def test_status_cli_reads_last_report_without_sweeping(tree: Tree) -> None:
    import json

    before = sweep(tree, policy())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.scratch_reaper",
            "--status",
            "--state-dir",
            str(tree[4]),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == before.status
    assert (tree[2] / "Build/Products/output.o").exists()


@pytest.mark.integration
def test_installed_wheel_is_independent_of_checkout_and_dry_run_preserves_data(
    tree: Tree, tmp_path: Path
) -> None:
    import json

    wheel_name = os.environ.get("HERDR_SCRATCH_TEST_WHEEL")
    assert wheel_name, "build the real wheel before installed-wheel acceptance"
    wheel = Path(wheel_name).resolve()
    assert wheel.is_file()
    environment = tmp_path / "installed"
    clean_environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE"}
    }
    creation = subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--symlinks", str(environment)],
        env=clean_environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert creation.returncode == 0, creation.stderr
    python = environment / "bin/python"
    installation = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert installation.returncode == 0, installation.stderr
    location = subprocess.check_output(
        [
            str(python),
            "-I",
            "-c",
            "import herdr_engineering.scratch_reaper as m; print(m.__file__)",
        ],
        cwd=tmp_path,
        text=True,
    ).strip()
    assert Path(location).is_relative_to(environment)
    import hashlib

    for name in ["scratch_reaper", "scratch_safety"]:
        installed_hash = subprocess.check_output(
            [
                str(python),
                "-I",
                "-c",
                "import hashlib,importlib,pathlib,sys; m=importlib.import_module('herdr_engineering.'+sys.argv[1]); print(hashlib.sha256(pathlib.Path(m.__file__).read_bytes()).hexdigest())",
                name,
            ],
            cwd=tmp_path,
            text=True,
        ).strip()
        source = Path(__file__).resolve().parents[1] / "herdr_engineering" / (name + ".py")
        assert installed_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    entry = environment / "bin/agent-scratch-reaper"
    assert entry.is_file()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "herdr_engineering.scratch_reaper",
            "--root",
            str(tree[0]),
            "--quarantine",
            str(tree[3]),
            "--state-dir",
            str(tree[4]),
            "--gate-state-dir",
            str(tmp_path / "gate"),
            "--session-root",
            str(sessions),
            "--min-free-gib",
            "0",
            "--ttl-minutes",
            "0",
            "--apply",
            "--dry-run",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["dry_run"] and data["would_remove_count"] >= 2
    assert not data["removed"]
    assert (tree[2] / "Build/Products/output.o").exists()


def test_even_published_clean_source_checkout_is_not_general_session_garbage(
    tree: Tree, tmp_path: Path
) -> None:
    checkout = tree[1] / "source-checkout"
    checkout.mkdir()
    remote = tmp_path / "source-remote.git"
    _git_run(tmp_path, "init", "--bare", str(remote))
    _git_run(checkout, "init")
    _git_run(checkout, "config", "user.name", "Retention Test")
    _git_run(checkout, "config", "user.email", "retention@localhost")
    (checkout / "source.swift").write_text("important source")
    _git_run(checkout, "add", ".")
    _git_run(checkout, "commit", "-m", "published source")
    _git_run(checkout, "remote", "add", "origin", str(remote))
    _git_run(checkout, "push", "-u", "origin", "HEAD")
    report = sweep(tree, replace(policy(), ttl_seconds=86400, session_ttl_seconds=0), apply=True)
    assert not report.quarantined
    assert (checkout / "source.swift").exists()
    assert any(p.reason == "source_repository" for p in report.protected)


def test_status_before_first_sweep_is_explicit_and_read_only(tmp_path: Path) -> None:
    import json

    state = tmp_path / "not-created"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.scratch_reaper",
            "--status",
            "--state-dir",
            str(state),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "never_run"
    assert not state.exists()


@pytest.mark.parametrize("name", ["dd with spaces", "dd-café"])
def test_real_argv_reference_with_nontrivial_path_is_protected(tree: Tree, name: str) -> None:
    anchor = tree[2].with_name(name)
    tree[2].rename(anchor)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            'import time; print("ready",flush=True); time.sleep(300)',
            str(anchor),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        report = sweep(tree, policy(), apply=True)
        assert not report.removed
        assert (anchor / "Build/Products/output.o").exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def test_repository_configured_filter_is_not_executed_by_cleanup(
    tree: Tree, tmp_path: Path
) -> None:
    import shlex

    checkout = tree[2] / "SourcePackages/checkouts/package"
    checkout.mkdir(parents=True)
    remote = tmp_path / "filter-remote.git"
    _git_run(tmp_path, "init", "--bare", str(remote))
    _git_run(checkout, "init")
    _git_run(checkout, "config", "user.name", "Retention Test")
    _git_run(checkout, "config", "user.email", "retention@localhost")
    source = checkout / "source.txt"
    source.write_text("published source")
    _git_run(checkout, "add", ".")
    _git_run(checkout, "commit", "-m", "published")
    _git_run(checkout, "remote", "add", "origin", str(remote))
    _git_run(checkout, "push", "-u", "origin", "HEAD")
    marker = tmp_path / "filter-executed"
    program = tmp_path / "real-identity-filter.sh"
    program.write_text("#!/bin/sh\nprintf executed > " + shlex.quote(str(marker)) + "\ncat\n")
    _git_run(checkout, "config", "filter.audit.clean", "/bin/sh " + shlex.quote(str(program)))
    (checkout / ".gitattributes").write_text("*.txt filter=audit\n")
    source.write_text("modified source!")
    _git_run(checkout, "status", "--porcelain")
    assert marker.exists(), "positive control: real Git invokes this configured filter"
    marker.unlink()
    report = sweep(tree, policy(), apply=True)
    assert not marker.exists()
    assert checkout.exists()
    assert any(p.reason == "repository_filter" for p in report.protected)


def test_non_session_memory_alias_does_not_block_or_pin_session_retention(
    tree: Tree, tmp_path: Path
) -> None:
    logs = tmp_path / "claude-projects"
    logs.mkdir()
    (logs / (SESSION + ".jsonl")).write_text('{"type":"user"}\n')
    memory = tmp_path / "durable-memory"
    memory.mkdir()
    other_id = "11234567-89ab-4cde-8fab-0123456789ab"
    (memory / "old-task.md").write_text("Historical reference: " + other_id)
    (logs / "memory").symlink_to(memory, target_is_directory=True)
    inactive = tree[1].parent / other_id
    inactive.mkdir()
    (inactive / "trace.log").write_text("old scratch")
    report = run_once(
        tree[0],
        tree[3],
        tree[4],
        replace(policy(), session_ttl_seconds=0),
        apply=True,
        gate_state=tmp_path / "gate",
        live_roots=[logs],
    )
    assert report.status is RunStatus.OK, report.errors
    assert tree[1].exists()
    assert not inactive.exists()
    assert (memory / "old-task.md").exists()


def test_relative_argv_reference_is_resolved_from_real_process_cwd(tree: Tree) -> None:
    anchor = tree[2]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            'import time;print("ready",flush=True);time.sleep(300)',
            anchor.name,
        ],
        cwd=anchor.parent,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        report = sweep(tree, policy(), apply=True)
        assert not report.removed
        assert (anchor / "Build/Products/output.o").exists()
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


@pytest.mark.parametrize("layout", ["classic", "swiftbuild"])
def test_custom_swiftpm_cache_layout_is_structurally_recognized(tree: Tree, layout: str) -> None:
    scratch = tree[1] / "scratchpad/custom-sp.2"
    output = scratch / ("x86_64-unknown-linux-gnu" if layout == "classic" else "out")
    output.mkdir(parents=True)
    (output / "object.o").write_bytes(b"generated")
    (scratch / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (scratch / "workspace-state.json").write_text(
        '{"version":6,"object":{"dependencies":[],"artifacts":[]}}'
    )
    (scratch / "checkouts").mkdir()
    (scratch / "checkouts/notes.txt").write_text("retained dependency state")
    (scratch / "Logs").mkdir()
    (scratch / "Logs/build.log").write_text("retained diagnostics")
    report = sweep(tree, policy(), apply=True)
    assert output in paths(report.removed)
    assert not output.exists()
    assert (scratch / "workspace-state.json").exists()
    assert (scratch / "checkouts/notes.txt").exists()
    assert (scratch / "Logs/build.log").exists()


def test_recovery_receipt_parent_traversal_is_rejected(tree: Tree) -> None:
    import json

    candidate, claim = _claim_fixture(tree)
    receipt = claim.with_suffix(".json")
    data = json.loads(receipt.read_text())
    outside = tree[0].parent / "outside-recovery"
    data["original"] = str(tree[0] / ".." / "outside-recovery")
    receipt.write_text(json.dumps(data))
    report = sweep(tree, policy(), apply=True)
    assert report.status is RunStatus.ERROR
    assert claim.exists() and not outside.exists()
    assert not candidate.path.exists()


@pytest.mark.parametrize("requested_compiler", ["native", "gcc"])
def test_real_compiler_cwd_protects_cache_without_cache_path_in_argv(
    tree: Tree, requested_compiler: str
) -> None:
    import shutil

    compiler = (
        shutil.which("clang") or shutil.which("gcc")
        if requested_compiler == "native"
        else shutil.which("gcc")
    )
    assert compiler is not None, "native compiler safety acceptance requires Clang or GCC"
    source = tree[1] / "source"
    source.mkdir()
    child = subprocess.Popen(
        [compiler, "-x", "c", "-fsyntax-only", "-"],
        cwd=source,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        report = sweep(tree, policy(), apply=True)
        assert not report.removed
        assert (tree[2] / "Build/Products/output.o").exists()
        assert any(p.reason == "process_in_use" for p in report.protected)
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=10)


@pytest.mark.parametrize("declaration", ["sqlite", "invalid_tag", "symlink_tag"])
def test_custom_swift_cache_declaration_requires_real_proof(
    tree: Tree, tmp_path: Path, declaration: str
) -> None:
    import sqlite3

    scratch = tree[1] / "scratchpad/custom-swift-state"
    output = scratch / "x86_64-unknown-linux-gnu"
    output.mkdir(parents=True)
    (output / "object.o").write_bytes(b"output")
    (scratch / "workspace-state.json").write_text(
        '{"version":6,"object":{"dependencies":[],"artifacts":[]}}'
    )
    if declaration == "sqlite":
        connection = sqlite3.connect(scratch / "build.db")
        try:
            connection.execute("CREATE TABLE build_state (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
    elif declaration == "invalid_tag":
        (scratch / "CACHEDIR.TAG").write_text("not a cache declaration")
    else:
        outside = tmp_path / "outside-tag"
        outside.write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        (scratch / "CACHEDIR.TAG").symlink_to(outside)
    report = sweep(tree, policy(), apply=True)
    assert (output in paths(report.removed)) == (declaration == "sqlite")
    if declaration != "sqlite":
        assert (output / "object.o").exists()


@pytest.mark.parametrize("fault", ["fifo", "readonly"])
def test_one_uninspectable_component_does_not_stop_unrelated_cleanup(
    tree: Tree, fault: str
) -> None:
    build = tree[2] / "Build"
    if fault == "fifo":
        os.mkfifo(build / "Products/control.pipe")
    else:
        (build / "Products").chmod(0o500)
    try:
        report = sweep(tree, policy(), apply=True)
        assert tree[2] / "ModuleCache.noindex" in paths(report.removed)
        assert (build / "Products/output.o").exists()
        assert any("inspection" in p.reason for p in report.protected)
    finally:
        if fault == "readonly":
            (build / "Products").chmod(0o700)


def test_conflicting_recovery_is_retained_but_other_output_can_be_reclaimed(tree: Tree) -> None:
    candidate, claim = _claim_fixture(tree)
    candidate.path.mkdir()
    (candidate.path / "new-work.txt").write_text("unique recreated work")
    report = sweep(tree, policy(), apply=True)
    assert tree[2] / "ModuleCache.noindex" in paths(report.removed)
    assert claim.exists() and (candidate.path / "new-work.txt").exists()
    again = sweep(tree, policy(), apply=True)
    assert claim.exists() and (candidate.path / "new-work.txt").exists()
    assert again.protected


def test_branch_discovery_limit_does_not_hide_independent_cache(tree: Tree) -> None:
    import shutil

    from herdr_engineering.scratch_reaper import _discover

    independent = tree[0] / "independent-dd"
    shutil.copytree(tree[2], independent)
    report = Report(RunStatus.DRY_RUN, True)
    candidates = _discover(tree[0], report=report, max_dirs=2)
    assert independent / "Build" in paths(candidates)
    assert report.protected and report.errors


@pytest.mark.parametrize("fault", ["fifo", "unreadable"])
def test_bad_quarantine_entry_does_not_stop_healthy_cache_cleanup(tree: Tree, fault: str) -> None:
    import shutil
    import time

    day = tree[3] / time.strftime("%Y-%m-%d")
    day.mkdir()
    bad = day / "bad-entry"
    bad.mkdir()
    if fault == "fifo":
        os.mkfifo(bad / "control.pipe")
    else:
        bad.chmod(0)
    good = day / "good-derived-data"
    shutil.copytree(tree[2], good)
    try:
        report = sweep(tree, policy(), apply=True)
        assert tree[2] / "Build" in paths(report.removed)
        assert good / "Build" in paths(report.removed)
        assert bad.exists()
    finally:
        if fault == "unreadable":
            bad.chmod(0o700)


def test_unreadable_quarantine_day_does_not_stop_healthy_expiry(tree: Tree) -> None:
    day = tree[3] / "2000-01-01"
    day.mkdir()
    healthy = tree[3] / "2000-01-02" / "expired-session"
    healthy.mkdir(parents=True)
    (healthy / "old.txt").write_text("abandoned fixture")
    day.chmod(0)
    try:
        report = sweep(tree, replace(policy(), quarantine_ttl_seconds=0), apply=True)
        assert not healthy.exists()
        assert not report.quarantined
        assert report.errors
        assert report.quarantine_bytes is None
    finally:
        day.chmod(0o700)
