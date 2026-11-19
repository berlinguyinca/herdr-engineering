"""Retention safety uses actual Cargo output, file locks and native processes."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.build_cache_config import validate
from herdr_engineering.cargo_target_storage import prepare_target
from herdr_engineering.errors import InvalidError


def fixture_output(tmp_path: Path, name: str = "project", *, days: int = 8) -> tuple[Path, Path, Path]:
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("real Cargo required")
    workspace = tmp_path / name
    (workspace / "src").mkdir(parents=True)
    (workspace / "Cargo.toml").write_text(f'[package]\nname="retention-{name}"\nversion="0.1.0"\nedition="2021"\n')
    (workspace / "src/lib.rs").write_text('pub fn answer()->u32 {42}\n')
    cache = tmp_path / "pool/compiler-cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = prepare_target(cache, workspace)
    disabled = tmp_path / "disabled.yaml"
    disabled.write_text("fabric_build_cache:\n  enabled: false\n")
    result = subprocess.run([cargo, "build", "--offline", "--target-dir", str(target)], cwd=workspace,
                            env={**os.environ, "HERDR_ENGINEERING_CONFIG": str(disabled)},
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    stamp = time.time() - days * 86400
    for path in [*target.rglob("*"), target]:
        if not path.is_symlink():
            os.utime(path, (stamp, stamp))
    return workspace, cache, target


requires_lsof = pytest.mark.skipif(
    shutil.which("lsof") is None,
    reason="open-file inspection needs lsof; eviction fails closed without it",
)


def test_retention_config_has_separate_validated_opt_in_budgets():
    config = validate(None)
    assert not config.local.target_retention_enabled
    assert config.local.target_internal_gib == 32
    assert config.local.target_external_gib == 256
    assert config.local.target_min_idle_hours == 168
    for setting, value in (("target_internal_gib", 0), ("target_external_gib", -1),
                           ("target_min_idle_hours", 23), ("target_internal_gib", float("nan")),
                           ("target_retention_enabled", "yes")):
        with pytest.raises(InvalidError):
            validate({"local": {setting: value}})


@requires_lsof
def test_dry_run_then_eviction_preserves_sources_markers_and_lock_inodes(tmp_path: Path):
    from herdr_engineering.cargo_target_retention import sweep
    workspace, cache, target = fixture_output(tmp_path)
    lock = target / "debug/.cargo-lock"
    inode = lock.stat().st_ino
    result = sweep(cache, tmp_path / "state", budget_bytes=1)
    assert result.used_bytes > 1 and result.would_reclaim_bytes > 0
    assert (target / "debug/libretention_project.rlib").is_file()
    result = sweep(cache, tmp_path / "state", budget_bytes=1, apply=True)
    assert result.reclaimed_bytes > 0
    assert not (target / "debug/libretention_project.rlib").exists()
    assert lock.stat().st_ino == inode
    assert (target / ".herdr-cargo-target.json").is_file()
    assert (workspace / "src/lib.rs").read_text() == 'pub fn answer()->u32 {42}\n'


def test_recent_invocations_and_real_open_files_are_preserved(tmp_path: Path):
    from herdr_engineering.cargo_target_retention import sweep
    workspace, cache, target = fixture_output(tmp_path)
    # Even an up-to-date Cargo invocation refreshes use, without artifact writes.
    prepare_target(cache, workspace)
    result = sweep(cache, tmp_path / "state", budget_bytes=1, apply=True)
    assert result.reclaimed_bytes == 0
    assert (target / "debug/libretention_project.rlib").is_file()
    stamp = time.time() - 8 * 86400
    os.utime(target / ".herdr-cargo-target.json", (stamp, stamp))
    with (target / "debug/libretention_project.rlib").open("rb"):
        result = sweep(cache, tmp_path / "state", budget_bytes=1, apply=True)
    assert result.reclaimed_bytes == 0


def test_native_cargo_and_maintenance_locks_prevent_eviction(tmp_path: Path):
    from herdr_engineering.cargo_target_retention import sweep
    _workspace, cache, target = fixture_output(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    with (state / "build-maintenance.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_SH)
        result = sweep(cache, state, budget_bytes=1, apply=True)
        assert result.status == "busy" and result.reclaimed_bytes == 0
    with (target / "debug/.cargo-lock").open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_SH)
        result = sweep(cache, state, budget_bytes=1, apply=True)
        assert result.reclaimed_bytes == 0
    assert (target / "debug/libretention_project.rlib").is_file()


@pytest.mark.parametrize("unsafe", ["source", "symlink", "foreign", "unmarked"])
def test_unknown_or_source_data_is_never_evicted(tmp_path: Path, unsafe: str):
    from herdr_engineering.cargo_target_retention import sweep
    _workspace, cache, target = fixture_output(tmp_path)
    if unsafe == "source":
        (target / "debug/Cargo.toml").write_text("operator source")
    elif unsafe == "symlink":
        (target / "debug/external").symlink_to(tmp_path)
    elif unsafe == "foreign":
        (target / ".herdr-cargo-target.json").write_text('{"owner_uid":-1,"version":1}')
    else:
        (target / ".herdr-cargo-target.json").unlink()
    result = sweep(cache, tmp_path / "state", budget_bytes=1, apply=True)
    assert result.reclaimed_bytes == 0
    assert (target / "debug/libretention_project.rlib").is_file()


def test_missing_storage_and_bounded_inventory_do_not_delete(tmp_path: Path):
    from herdr_engineering.cargo_target_retention import sweep
    result = sweep(tmp_path / "missing", tmp_path / "state", budget_bytes=1, apply=True)
    assert result.status == "unavailable"
    assert not (tmp_path / "missing").exists()
    _workspace, cache, target = fixture_output(tmp_path)
    result = sweep(cache, tmp_path / "state", budget_bytes=1, apply=True, max_entries=2)
    assert result.status == "partial" and result.reclaimed_bytes == 0
    assert (target / "debug/libretention_project.rlib").is_file()


def test_report_growth_and_scheduler_preserve_operator_files(tmp_path: Path):
    import json
    import plistlib
    import sys

    import yaml

    from herdr_engineering.cargo_host import enable_retention, install
    from herdr_engineering.cargo_target_retention import (
        install_job,
        maintain_targets,
        retention_report,
    )
    workspace, cache, target = fixture_output(tmp_path)
    state = tmp_path / "state"
    home = tmp_path / "home"
    cargo = shutil.which("cargo")
    assert cargo
    install(home, Path(cargo), Path(sys.executable))
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump({"fabric_build_cache": {"enabled": True, "local": {
        "manage_targets": True, "min_free_disk_percent": 0,
        "storage_tiers": [{"name": "internal", "directory": str(cache)},
                          {"name": "absent-external", "volume_uuid": "11111111-2222-4333-8444-555555555555",
                           "directory": "compiler-cache"}]}}}))
    keys = ("HOME", "HERDR_ENGINEERING_STATE", "HERDR_ENGINEERING_CONFIG")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update(HOME=str(home), HERDR_ENGINEERING_STATE=str(state), HERDR_ENGINEERING_CONFIG=str(config))
        assert "No target" in retention_report()
        first = maintain_targets()
        assert first["tiers"][0]["used_bytes"] > 0
        assert first["tiers"][1]["status"] == "unavailable"
        assert first["tiers"][1]["budget_bytes"] == 256 * 1024 ** 3
        (target / "debug/growth.bin").write_bytes(b"x" * 65536)
        second = maintain_targets()
        assert second["tiers"][0]["growth_bytes"] > 0
        assert "internal:" in retention_report()
        with pytest.raises(ValueError, match="Enable target retention"):
            maintain_targets(apply=True)
        enabled = enable_retention(config, schedule=False)
        assert enabled["target_retention_enabled"]
        assert enable_retention(config, schedule=False)["target_retention_enabled"]
        assert config.with_name(config.name + ".before-herdr-target-retention").exists()
        job = install_job(home, Path(sys.executable).parent / "herdr-eng", activate=False)
        data = plistlib.loads(job.read_bytes())
        assert data["StartInterval"] == 3600 and data["StandardOutPath"] == "/dev/null"
        assert "targets-maintain" in data["ProgramArguments"] and "--apply" in data["ProgramArguments"]
        assert install_job(home, Path(sys.executable).parent / "herdr-eng", activate=False) == job
        job.write_text("operator data")
        with pytest.raises(ValueError, match="existing|Existing"):
            install_job(home, Path(sys.executable).parent / "herdr-eng", activate=False)
        assert job.read_text() == "operator data"
        cli = subprocess.run([sys.executable, "-m", "herdr_engineering.cargo_host", "targets-report", "--json"],
                             text=True, capture_output=True, timeout=15)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["tiers"][0]["tier"] == "internal"
        for arguments in (("targets-maintain",), ("targets-report",),
                          ("targets-retention-enable", "--config", str(config), "--no-schedule")):
            smoke = subprocess.run([sys.executable, "-m", "herdr_engineering.cargo_host", *arguments],
                                   text=True, capture_output=True, timeout=15)
            assert smoke.returncode == 0, smoke.stderr
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_native_cargo_waits_on_preserved_profile_lock(tmp_path: Path):
    workspace, _cache, target = fixture_output(tmp_path)
    cargo = shutil.which("cargo")
    assert cargo
    disabled = tmp_path / "disabled.yaml"
    with (target / "debug/.cargo-lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = subprocess.Popen([cargo, "build", "--offline", "--target-dir", str(target)], cwd=workspace,
                                 env={**os.environ, "HERDR_ENGINEERING_CONFIG": str(disabled)},
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                child.communicate(timeout=2)
            fcntl.flock(lock, fcntl.LOCK_UN)
            _out, error = child.communicate(timeout=30)
            assert child.returncode == 0, error
            assert "Blocking" in error and "lock" in error
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_inventory_limit_bounds_wide_directory_memory(tmp_path: Path):
    import tracemalloc

    from herdr_engineering.cargo_target_retention import ScanLimit, _scan
    wide = tmp_path / "wide"
    wide.mkdir()
    for number in range(3000):
        (wide / str(number)).touch()
    tracemalloc.start()
    try:
        with pytest.raises(TimeoutError):
            _scan(wide, ScanLimit(2, time.monotonic() + 5))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024, f"bounded scan allocated {peak} bytes"


def test_schedule_keeps_selected_configuration_and_maintenance_state(tmp_path: Path):
    import plistlib
    import sys

    from herdr_engineering.cargo_target_retention import install_job
    config = tmp_path / "custom/settings.yaml"
    job = install_job(tmp_path / "home", Path(sys.executable).parent / "herdr-eng",
                      config_path=config, activate=False)
    payload = plistlib.loads(job.read_bytes())
    assert payload["EnvironmentVariables"]["HERDR_ENGINEERING_CONFIG"] == str(config.resolve())
    from herdr_engineering.build_coord import _default_state_dir
    assert payload["EnvironmentVariables"]["HERDR_ENGINEERING_STATE"] == str(_default_state_dir().parent.resolve())


@pytest.mark.integration
def test_mounted_target_directory_is_not_part_of_enrolled_root(tmp_path: Path):
    import sys

    from herdr_engineering.cargo_target_retention import sweep
    if sys.platform != "darwin":
        pytest.skip("real APFS mount boundary test")
    _workspace, cache, target = fixture_output(tmp_path)
    holding = target.with_name("saved-original")
    target.rename(holding)
    target.mkdir()
    image = tmp_path / "boundary.sparseimage"
    subprocess.run(["hdiutil", "create", "-quiet", "-size", "128m", "-fs", "APFS", "-type", "SPARSE",
                    "-volname", "HerdrBoundary", str(image)], check=True, timeout=30)
    subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", str(target), str(image)], check=True, timeout=30)
    try:
        shutil.copytree(holding, target, dirs_exist_ok=True)
        # The extra original is outside managed naming and must be preserved too.
        holding.rename(tmp_path / "original")
        result = sweep(cache, tmp_path / "state", budget_bytes=1, apply=True)
        assert result.reclaimed_bytes == 0
        assert (target / "debug/libretention_project.rlib").is_file()
        assert result.status == "partial"
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(target)], check=True, timeout=30)
        image.unlink(missing_ok=True)


@pytest.mark.e2e
def test_managed_cargo_build_evict_and_rebuild_via_public_cli(tmp_path: Path):
    import json
    import sys

    import yaml

    from herdr_engineering.cargo_host import install
    workspace, cache, target = fixture_output(tmp_path)
    home = tmp_path / "home"
    cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
    assert cargo and rustc
    install(home, Path(cargo), Path(sys.executable))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"fabric_build_cache": {"enabled": True, "local": {
        "manage_targets": True, "target_retention_enabled": True, "target_internal_gib": .000001,
        "min_free_disk_percent": 0, "storage_tiers": [{"name": "internal", "directory": str(cache)}]}}}))
    env = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"), "RUSTC": rustc,
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "state"), "HERDR_ENGINEERING_CONFIG": str(config),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    for key in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "RUSTC_WRAPPER", "HERDR_CARGO_HOST_COORDINATED"):
        env.pop(key, None)
    command = [str(home / ".local/bin/cargo"), "build", "--offline"]
    first = subprocess.run(command, cwd=workspace, env=env, text=True, capture_output=True, timeout=90)
    assert first.returncode == 0, first.stderr
    assert (workspace / "target").is_symlink()
    inode = (target / "debug/.cargo-lock").stat().st_ino
    stamp = time.time() - 8 * 86400
    for path in [*target.rglob("*"), target]:
        if not path.is_symlink():
            os.utime(path, (stamp, stamp))
    cleanup = subprocess.run([sys.executable, "-m", "herdr_engineering.cargo_host", "targets-maintain", "--apply"],
                             env=env, text=True, capture_output=True, timeout=30)
    assert cleanup.returncode == 0, cleanup.stderr
    assert json.loads(cleanup.stdout)["tiers"][0]["reclaimed_bytes"] > 0
    assert not (target / "debug/libretention_project.rlib").exists()
    assert (target / "debug/.cargo-lock").stat().st_ino == inode
    rebuilt = subprocess.run(command, cwd=workspace, env=env, text=True, capture_output=True, timeout=90)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert (target / "debug/libretention_project.rlib").is_file()


def test_invalid_policy_and_unowned_roots_fail_closed(tmp_path: Path):
    from herdr_engineering.cargo_target_retention import install_job, sweep
    with pytest.raises(ValueError):
        sweep(tmp_path / "absent", tmp_path / "state", budget_bytes=0)
    with pytest.raises(ValueError):
        install_job(tmp_path, tmp_path / "missing", activate=False)
    _workspace, cache, target = fixture_output(tmp_path)
    (target.parent / ".herdr-target-root.json").write_text('{"version":0}')
    assert sweep(cache, tmp_path / "state", budget_bytes=1, apply=True).status == "error"
    assert (target / "debug/libretention_project.rlib").is_file()


def test_corrupt_reports_and_disabled_cli_are_clear(tmp_path: Path):
    import json
    import sys

    from herdr_engineering.cargo_target_retention import read_report, retention_report
    state = tmp_path / "state"
    gates = state / "build-gates"
    gates.mkdir(parents=True)
    path = gates / "target-retention.json"
    previous = {key: os.environ.get(key) for key in ("HERDR_ENGINEERING_STATE", "HERDR_ENGINEERING_CONFIG")}
    config = tmp_path / "disabled.yaml"
    config.write_text("fabric_build_cache:\n  enabled: false\n")
    try:
        os.environ.update(HERDR_ENGINEERING_STATE=str(state), HERDR_ENGINEERING_CONFIG=str(config))
        for payload in ("not JSON", "[]", "x" * (256 * 1024 + 1)):
            path.write_text(payload)
            assert read_report(gates) is None
        path.write_text(json.dumps({"timestamp": "invalid"}))
        assert "Invalid" in retention_report()
        for action in ("targets-maintain", "targets-retention-enable"):
            command = [sys.executable, "-m", "herdr_engineering.cargo_host", action, "--config", str(config), "--no-schedule"]
            result = subprocess.run(command, text=True, capture_output=True, timeout=15)
            assert result.returncode != 0 and "Enable managed" in result.stderr
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fd_relative_removal_does_not_follow_links_or_delete_source(tmp_path: Path):
    from herdr_engineering.cargo_target_retention import ScanLimit, _remove_at
    root = tmp_path / "generated"
    root.mkdir()
    outside = tmp_path / "keep.txt"
    outside.write_text("operator data")
    (root / "link").symlink_to(outside)
    (root / "nested").mkdir()
    (root / "nested/output").write_bytes(b"generated")
    (root / "Cargo.toml").write_text("source")
    os.mkfifo(root / "special")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    limit = ScanLimit(100, time.monotonic() + 5)
    try:
        for name in ("Cargo.toml", "special"):
            with pytest.raises(ValueError):
                _remove_at(descriptor, name, root.stat().st_dev, limit)
        _remove_at(descriptor, "link", root.stat().st_dev, limit)
        _remove_at(descriptor, "nested", root.stat().st_dev, limit)
        assert outside.read_text() == "operator data"
        assert not (root / "nested").exists() and not (root / "link").is_symlink()
    finally:
        os.close(descriptor)
