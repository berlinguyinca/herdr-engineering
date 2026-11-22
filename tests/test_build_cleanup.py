"""Safety and pressure behavior of the Rust build cache janitor."""
import fcntl
import json
import os
import subprocess
import time
from collections import namedtuple
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from tests.real_cargo import build

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(cleanup.shutil, "disk_usage", lambda _: Usage(1000, 900, 100))
    return root


def target(root, name="project", age=48):
    project = root / name
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (project / "src").mkdir()
    (project / "src/main.rs").write_text("fn main() {}\n")
    cache = build(project, project / "target")  # a real build: cargo writes the tag and the fingerprints
    stamp = time.time() - age * 3600
    for path in [*cache.rglob("*"), cache]:
        os.utime(path, (stamp, stamp))
    return cache


def run(root, **kwargs):
    return cleanup.run_once([root], root.parent / "state", **kwargs)


def test_removes_only_old_verified_outputs_and_writes_status(workspace):
    old = target(workspace)
    recent = target(workspace, "recent", age=1)
    unknown = workspace / "not-cargo" / "target"
    unknown.mkdir(parents=True)
    (unknown / "source.rs").write_text("keep")
    result = run(workspace)
    assert not old.exists()
    assert recent.exists() and unknown.exists()
    assert (old.parent / "Cargo.toml").exists()
    assert result["removed_count"] == 1
    assert result["removed_bytes"] > 0
    assert result["reclaimed_bytes"] == 0  # mocked filesystem has no free-space change
    assert json.loads((workspace.parent / "state/build-cleanup.json").read_text()) == result


def test_dry_run_does_not_delete(workspace):
    cache = target(workspace)
    result = run(workspace, dry_run=True)
    assert cache.exists()
    assert result["removed_count"] == 0
    assert result["would_remove"] == [str(cache)]


def test_no_pressure_no_removal(workspace, monkeypatch):
    cache = target(workspace)
    monkeypatch.setattr(cleanup.shutil, "disk_usage", lambda _: Usage(1000, 840, 160))
    assert run(workspace)["removed_count"] == 0
    assert cache.exists()


def test_oldest_first_stops_at_actual_low_water(workspace, monkeypatch):
    younger = target(workspace, "young", age=48)
    older = target(workspace, "old", age=72)
    monkeypatch.setattr(cleanup.shutil, "disk_usage", lambda _: Usage(1000, 900, 100)
                        if older.exists() else Usage(1000, 790, 210))
    assert run(workspace)["removed_count"] == 1
    assert younger.exists() and not older.exists()


def test_nested_recent_artifact_protects_old_directory(workspace):
    cache = target(workspace)
    (cache / "artifact").write_text("new output")
    assert run(workspace)["removed_count"] == 0
    assert cache.exists()


def _old_artifact(cache):
    (cache / "artifact").write_text("output")
    stamp = time.time() - 48 * 3600
    os.utime(cache / "artifact", (stamp, stamp))


def test_tracked_output_is_never_deleted(workspace):
    cache = target(workspace)
    _old_artifact(cache)
    subprocess.run(["git", "-C", str(cache.parent), "add", "target/artifact"], check=True)
    assert run(workspace)["removed_count"] == 0
    assert cache.exists()


def test_symlink_target_and_symlink_inside_target_are_preserved(workspace):
    cache = target(workspace)
    external = workspace.parent / "precious"
    external.write_text("keep")
    (cache / "link").symlink_to(external)
    other = workspace / "other"
    other.mkdir()
    (other / "target").symlink_to(cache, target_is_directory=True)
    assert run(workspace)["removed_count"] == 0
    assert external.read_text() == "keep"


def test_exclusive_maintenance_lock_blocks_cleanup_but_a_running_build_does_not(workspace):
    cache = target(workspace, "idle")
    state = workspace.parent / "state"
    state.mkdir()
    with (state / "build-maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run(workspace)
    assert result["status"] == "busy"
    assert cache.exists()
    with (state / "build-maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)  # a running build: the per-target checks protect it
        assert run(workspace)["status"] != "busy"


def test_scan_failure_is_closed(workspace, monkeypatch):
    cache = target(workspace)
    original = cleanup.os.scandir
    def denied(path):
        if Path(path) == cache:
            raise PermissionError("denied")
        return original(path)
    monkeypatch.setattr(cleanup.os, "scandir", denied)
    assert run(workspace)["errors"]
    assert cache.exists()


def test_invalid_policy_rejected(workspace):
    with pytest.raises(ValueError):
        run(workspace, low_water=90, high_water=85)


def test_unsafe_target_does_not_prevent_cleanup_of_other_projects(workspace):
    unsafe = target(workspace, "unsafe")
    safe = target(workspace, "safe")
    (unsafe / "link").symlink_to(workspace.parent / "outside")
    result = run(workspace)
    assert unsafe.exists() and not safe.exists()
    assert result["removed_count"] == 1
    assert any(item["path"] == str(unsafe) for item in result["skipped"])


def test_unverifiable_non_git_project_is_preserved(workspace):
    cache = target(workspace)
    cleanup.shutil.rmtree(cache.parent / ".git")
    assert run(workspace)["removed_count"] == 0
    assert cache.exists()


def test_discovery_limit_skips_the_root_with_a_reason_without_removing(workspace, monkeypatch):
    cache = target(workspace)
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 1)
    result = run(workspace)
    # discovery keeps what it found and warns; the target itself exceeds the inspection limit: reported, kept
    assert result["status"] != "ok" and any("limit" in warning for warning in result["warnings"])
    assert result["removed_count"] == 0 and any("limit" in error for error in result["errors"])
    assert cache.exists()


def test_nested_source_manifest_is_preserved(workspace):
    cache = target(workspace)
    (cache / "Cargo.toml").write_text("source, not disposable output")
    assert run(workspace)["removed_count"] == 0
    assert cache.exists()


def _paths(proc):
    """The files the scan says processes use (their cwd, executable, descriptors and mappings)."""
    return [hold[3] for hold in cleanup._scan(proc) if hold[2] == "uses"]


def test_proc_reads_cwd_executable_and_open_files(tmp_path):
    proc = tmp_path / "proc"
    pid = proc / "99999999"
    (pid / "fd").mkdir(parents=True)
    for link, value in [("cwd", "/project"), ("exe", "/binary"),
                        ("fd/0", "/project/target/output"), ("fd/1", "socket:[123]")]:
        (pid / link).symlink_to(value)
    paths = _paths(proc)
    assert set(paths) == {Path("/project"), Path("/binary"), Path("/project/target/output")}


def test_missing_proc_falls_back_to_lsof_or_fails_closed(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # a real host without lsof
    with pytest.raises(OSError):
        _paths(tmp_path / "no-proc")


def _other_filesystem() -> Path | None:
    """A writable directory on a real filesystem other than the temp directory's (``/dev/shm`` on Linux)."""
    shm = Path("/dev/shm")
    return shm if shm.is_dir() and os.access(shm, os.W_OK) else None


def test_a_target_on_another_filesystem_is_not_measured_by_the_survey(tmp_path):
    other = _other_filesystem()
    if other is None or other.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("needs a second real filesystem")
    home = other / f"herdr-test-{os.getpid()}"
    home.mkdir()
    try:
        foreign = target(home, "mounted")
        root = tmp_path / "root"
        root.mkdir()
        spec = {"root": str(root), "cap": 1, "full": False, "cap_cutoff": None, "exclude": [],
                "targets": [str(foreign)], "seconds": 60, "max_entries": 500_000, "max_depth": 6}
        rows = cleanup._parse_survey(cleanup._run_child(cleanup._SURVEY_CHILD, [], 60, "survey",
                                                        json.dumps(spec)))["rows"]
        assert rows == [{"path": str(foreign), "foreign_device": True}]
    finally:
        cleanup.shutil.rmtree(home, ignore_errors=True)


def test_an_entry_of_another_filesystem_inside_a_target_is_refused(workspace):
    """``/dev`` is a real directory of another filesystem (and owner): met inside a target it is a mount."""
    cache = target(workspace)
    with pytest.raises(OSError, match="mount"):
        cleanup._check_entry("dev", os.lstat("/dev"), cache.lstat().st_dev, cache)


def test_the_depth_limit_reaches_the_survey_and_a_deep_source_tree_does_not_block_a_shallow_cache(
        workspace, monkeypatch):
    shallow = target(workspace)
    (workspace / "source/one/two/three").mkdir(parents=True)
    (workspace / "a/b/c/d").mkdir(parents=True)
    deep = target(workspace / "a/b/c/d", "deep")
    monkeypatch.setattr(cleanup, "_MAX_DEPTH", 2)  # forwarded to the child that does the discovery
    assert run(workspace)["removed_count"] == 1
    assert not shallow.exists()
    assert deep.exists(), "a target deeper than the limit must not be found: the limit never reached the child"


@pytest.fixture
def hidden_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    child, parent = proc / "77777777", proc / "88888888"
    (child / "fd").mkdir(parents=True)
    (parent / "fd").mkdir(parents=True)
    binary = tmp_path / "herdr"
    binary.write_text("Herdr fixture")
    (child / "cmdline").write_bytes(b"cargo\0build\0")
    (child / "status").write_text("PPid:\t88888888\n")
    (parent / "cmdline").write_bytes(os.fsencode(binary) + b"\0server\0")
    (parent / "status").write_text("PPid:\t0\n")
    (parent / "exe").symlink_to(binary)
    original = cleanup.os.scandir

    def scandir(path):
        if path == child / "fd":
            raise PermissionError("nondumpable process")
        return original(path)

    monkeypatch.setattr(cleanup.os, "scandir", scandir)
    return proc, child, parent, binary


def test_inaccessible_managed_build_aborts_inspection(hidden_proc):
    proc, child, _, _ = hidden_proc
    assert cleanup._managed_process(child, proc)
    with pytest.raises(PermissionError):
        _paths(proc)


def test_inaccessible_unmanaged_daemon_does_not_block_inspection(hidden_proc):
    proc, child, parent, binary = hidden_proc
    (parent / "cmdline").write_bytes(b"systemd\0--user\0")
    assert not cleanup._managed_process(child, proc)
    assert _paths(proc) == [binary]


def test_managed_ancestry_walks_multiple_generations(hidden_proc):
    proc, child, parent, _ = hidden_proc
    intermediate = proc / "66666666"
    intermediate.mkdir()
    (intermediate / "status").write_text(f"PPid:\t{parent.name}\n")
    (intermediate / "cmdline").write_bytes(b"bash\0")
    (child / "status").write_text(f"PPid:\t{intermediate.name}\n")
    assert cleanup._managed_process(child, proc)


def test_unknown_ancestry_fails_closed(hidden_proc):
    proc, child, parent, _ = hidden_proc
    (parent / "cmdline").write_bytes(b"systemd\0--user\0")
    (parent / "status").unlink()
    with pytest.raises(OSError, match="ancestry changed"):
        _paths(proc)


def test_unverified_herdr_executable_fails_closed(hidden_proc):
    proc, _, parent, binary = hidden_proc
    other = binary.parent / "other"
    other.write_text("not herdr")
    (parent / "exe").unlink()
    (parent / "exe").symlink_to(other)
    with pytest.raises(OSError, match="Unverifiable Herdr"):
        _paths(proc)


def test_ancestry_cycle_fails_closed(hidden_proc):
    proc, child, parent, _ = hidden_proc
    (parent / "cmdline").write_bytes(b"bash\0")
    (parent / "status").write_text(f"PPid:\t{child.name}\n")
    with pytest.raises(OSError, match="Unverifiable process ancestry"):
        _paths(proc)


def test_any_verified_herdr_ancestor_is_conservatively_protected(hidden_proc):
    proc, child, parent, binary = hidden_proc
    (parent / "cmdline").write_bytes(os.fsencode(binary) + b"\0attach\0server\0")
    assert cleanup._managed_process(child, proc)


def test_herdr_global_options_do_not_hide_managed_ancestry(hidden_proc):
    proc, child, parent, binary = hidden_proc
    (parent / "cmdline").write_bytes(os.fsencode(binary) + b"\0--session\0abc\0server\0")
    assert cleanup._managed_process(child, proc)
    with pytest.raises(PermissionError):
        _paths(proc)
