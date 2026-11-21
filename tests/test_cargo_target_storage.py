"""Managed target placement preserves explicit Cargo storage choices."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from herdr_engineering.build_cache_config import validate
from herdr_engineering.cargo_target_migration import migrate_target, tree_manifest
from herdr_engineering.cargo_target_storage import (
    alias_owned,
    compatibility_alias,
    prepare_target,
    seed_existing_aliases,
    target_directory,
    target_override,
    workspace_root,
)

requires_lsof = pytest.mark.skipif(
    shutil.which("lsof") is None,
    reason="open-file inspection needs lsof; migration fails closed without it",
)


def test_managed_target_storage_is_an_explicit_valid_option():
    assert validate({"enabled": True, "local": {"manage_targets": True}}).local.manage_targets
    assert validate(None).local.manage_targets  # on by default
    assert not validate({"enabled": True, "local": {"manage_targets": False}}).local.manage_targets


def test_unique_workspaces_and_ownership_are_preserved(tmp_path: Path):
    cache = tmp_path / "pool/compiler-cache"
    cache.mkdir(parents=True)
    first, second = tmp_path / "first", tmp_path / "second"
    assert target_directory(cache, first) != target_directory(cache, second)
    target = prepare_target(cache, first)
    assert prepare_target(cache, first) == target
    marker = target / ".herdr-cargo-target.json"
    marker.write_text(json.dumps({"version": 1, "workspace": str(second), "owner_uid": os.getuid()}))
    with pytest.raises(ValueError, match="different workspace"):
        prepare_target(cache, first)


def test_explicit_target_overrides_remain_authoritative(tmp_path: Path):
    assert target_override(["build", "--target-dir", "custom"], {}, tmp_path)
    assert target_override(["build", "--target-dir=custom"], {}, tmp_path)
    assert target_override([], {"CARGO_BUILD_TARGET_DIR": "custom"}, tmp_path)
    assert target_override(["--config", 'build.target-dir="custom"', "build"], {}, tmp_path)
    assert not target_override([], {"CARGO_HOME": str(tmp_path / "empty")}, tmp_path)
    (tmp_path / ".cargo").mkdir()
    (tmp_path / ".cargo/config.toml").write_text('[build]\ntarget-dir="custom"\n')
    assert target_override([], {}, tmp_path)


def test_target_storage_refuses_unknown_or_unsafe_directories(tmp_path: Path):
    cache = tmp_path / "pool/cache"
    with pytest.raises(ValueError, match="disappeared"):
        prepare_target(cache, tmp_path / "one")
    cache.mkdir(parents=True)
    first = prepare_target(cache, tmp_path / "one")
    second = target_directory(cache, tmp_path / "two")
    second.mkdir()
    (second / "keep.txt").write_text("operator data")
    with pytest.raises(ValueError, match="no ownership"):
        prepare_target(cache, tmp_path / "two")
    assert (second / "keep.txt").read_text() == "operator data"
    (first.parent / ".herdr-target-root.json").write_text('{"version":0,"owner_uid":0}')
    with pytest.raises(ValueError, match="ownership"):
        prepare_target(cache, tmp_path / "one")


def test_more_explicit_configuration_and_workspace_failures(tmp_path: Path):
    assert target_override([], {"CARGO_TARGET_DIR": "somewhere"}, tmp_path)
    assert target_override(["--config=build.target-dir='custom'"], {}, tmp_path)
    assert target_override(["--config", "extra.toml"], {}, tmp_path)
    assert target_override(["--config", "bad==toml"], {}, tmp_path)
    cargo = shutil.which("cargo")
    if cargo:
        with pytest.raises(ValueError, match="Cannot determine"):
            workspace_root(Path(cargo), [], tmp_path)


def test_migration_requires_explicit_quiet_window(tmp_path: Path):
    with pytest.raises(ValueError, match="Pause"):
        migrate_target(tmp_path / "target", tmp_path / "cache", tmp_path, tmp_path / "state")


def build_real_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("real Cargo required")
    workspace = tmp_path / "project"
    (workspace / "src").mkdir(parents=True)
    (workspace / "Cargo.toml").write_text('[package]\nname="storage-probe"\nversion="0.1.0"\nedition="2021"\n')
    (workspace / "src/lib.rs").write_text('pub fn answer() -> u32 { 42 }\n')
    (workspace / ".gitignore").write_text('target/\n')
    subprocess.run(["git", "init", "-q", "-b", "fixture", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "Cargo.toml", "src/lib.rs", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Cache test", "-c",
                    "user.email=cache@example.invalid", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"], check=True)
    disabled = tmp_path / "disabled.yaml"
    disabled.write_text("fabric_build_cache:\n  enabled: false\n")
    result = subprocess.run([cargo, "build", "--offline", "--target-dir", str(workspace / "target")],
                            cwd=workspace, env={**os.environ, "HERDR_ENGINEERING_CONFIG": str(disabled)},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    cache = tmp_path / "external/compiler-cache"
    cache.mkdir(parents=True)
    return workspace, cache, tmp_path / "state"


@requires_lsof
def test_real_output_migration_preserves_bytes_links_and_original_paths(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    target = workspace / "target"
    library = target / "debug/libstorage_probe.rlib"
    os.link(library, target / "debug/extra-hardlink.rlib")
    (target / "debug/current.rlib").symlink_to("libstorage_probe.rlib")
    original = tree_manifest(target)
    result = migrate_target(target, cache, workspace, state, quiescent=True)
    assert result["status"] == "moved_verified"
    assert target.is_symlink() and library.is_file()
    destination = Path(str(result["destination"]))
    copied = tree_manifest(destination)
    assert all(copied[name] == entry for name, entry in original.items())
    assert (destination / "debug/libstorage_probe.rlib").stat().st_ino == (destination / "debug/extra-hardlink.rlib").stat().st_ino
    destination.rename(destination.with_name(destination.name + "-unmounted"))
    assert alias_owned(state, workspace), "Migrated aliases must survive an unavailable destination"


@requires_lsof
def test_open_output_is_never_migrated(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    target = workspace / "target"
    with (target / ".rustc_info.json").open("rb"):
        with pytest.raises(ValueError, match="open files"):
            migrate_target(target, cache, workspace, state, quiescent=True)
    assert target.is_dir() and not target.is_symlink()


@requires_lsof
def test_escaping_link_preserves_original_output(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    (workspace / "target/source-link").symlink_to(workspace / "src/lib.rs")
    with pytest.raises(ValueError, match="escaping symlink"):
        migrate_target(workspace / "target", cache, workspace, state, quiescent=True)
    assert (workspace / "src/lib.rs").is_file()


def test_tracked_output_and_special_files_are_preserved(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    target = workspace / "target"
    subprocess.run(["git", "-C", str(workspace), "add", "-f", "target/.rustc_info.json"], check=True)
    with pytest.raises(ValueError, match="untracked"):
        migrate_target(target, cache, workspace, state, quiescent=True)
    subprocess.run(["git", "-C", str(workspace), "reset", "-q", "HEAD", "--", "target/.rustc_info.json"], check=True)
    os.mkfifo(target / "special")
    with pytest.raises(ValueError, match="Special file"):
        tree_manifest(target)


@requires_lsof
def test_newer_managed_output_is_not_overwritten(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    current = prepare_target(cache, workspace)
    (current / "newer-output").write_text("keep this")
    result = migrate_target(workspace / "target", cache, workspace, state, quiescent=True)
    assert result["status"] == "moved_verified"
    assert Path(str(result["destination"])) != current
    assert (current / "newer-output").read_text() == "keep this"


def test_owned_alias_can_fall_back_after_external_storage_disappears(tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    state = tmp_path / "state"
    cache = tmp_path / "external/compiler-cache"
    cache.mkdir(parents=True)
    external = prepare_target(cache, workspace)
    assert compatibility_alias(state, workspace, external)
    assert compatibility_alias(state, workspace, external)
    assert seed_existing_aliases(cache, state) == 1
    # Rename the actual filesystem tree to model an unavailable mount path.
    cache.parent.rename(tmp_path / "disconnected")
    assert not (workspace / "target").exists()
    assert alias_owned(state, workspace)
    internal_cache = tmp_path / "internal/compiler-cache"
    internal_cache.mkdir(parents=True)
    internal = prepare_target(internal_cache, workspace)
    assert compatibility_alias(state, workspace, internal)
    assert (workspace / "target").resolve() == internal
    assert compatibility_alias(state, workspace, external)
    assert seed_existing_aliases(tmp_path / "absent", state) == 0


def test_aliases_never_replace_operator_paths_or_accept_bad_records(tmp_path: Path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    state = tmp_path / "state"
    alias = workspace / "target"
    alias.mkdir()
    assert not compatibility_alias(state, workspace, tmp_path / "new")
    alias.rmdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    alias.symlink_to(foreign)
    assert not alias_owned(state, workspace)
    assert not compatibility_alias(state, workspace, tmp_path / "new")
    marker = foreign / ".herdr-cargo-target.json"
    marker.write_text("invalid json")
    assert not alias_owned(state, workspace)
    marker.write_text(json.dumps({"workspace": str(workspace), "owner_uid": os.getuid(), "version": 1}))
    assert seed_existing_aliases(tmp_path / "cache", state) == 0
    assert alias_owned(state, workspace)


def test_unknown_config_and_symlink_destinations_are_preserved(tmp_path: Path):
    (tmp_path / ".cargo").mkdir()
    config = tmp_path / ".cargo/config.toml"
    config.write_text("not valid TOML!")
    assert target_override([], {}, tmp_path)
    cache = tmp_path / "pool/compiler-cache"
    cache.mkdir(parents=True)
    root = cache.parent / "targets"
    root.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="root cannot"):
        prepare_target(cache, tmp_path / "workspace")
    root.unlink()
    target = prepare_target(cache, tmp_path / "workspace")
    shutil.rmtree(target)
    target.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="directory cannot"):
        prepare_target(cache, tmp_path / "workspace")


def test_source_markers_and_unrecognized_output_are_preserved(tmp_path: Path):
    from herdr_engineering.cargo_target_migration import recognized_source
    with pytest.raises(ValueError, match="recognized"):
        recognized_source(tmp_path / "target", tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "Cargo.toml").write_text("source data")
    with pytest.raises(ValueError, match="Source/repository"):
        tree_manifest(target)


@requires_lsof
def test_failed_real_copy_preserves_original_output(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    target = workspace / "target"
    before = tree_manifest(target)
    # A real OS failure: rsync cannot create files in an unwritable destination.
    # Use a missing executable search path, retaining real git/lsof inspection.
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("git", "lsof"):
        executable = shutil.which(name)
        assert executable
        (tools / name).symlink_to(executable)
    previous = os.environ["PATH"]
    try:
        os.environ["PATH"] = str(tools)
        with pytest.raises(FileNotFoundError):
            migrate_target(target, cache, workspace, state, quiescent=True)
    finally:
        os.environ["PATH"] = previous
    assert tree_manifest(target) == before
    assert not list((cache.parent / "targets").glob("*.copying-*"))


def test_application_arguments_do_not_change_workspace_or_target(tmp_path: Path):
    first, _cache, _state = build_real_fixture(tmp_path / "first")
    second, _cache, _state = build_real_fixture(tmp_path / "second")
    cargo = shutil.which("cargo")
    assert cargo
    arguments = ["run", "--", "--manifest-path", str(second / "Cargo.toml"), "--target-dir", "app-data"]
    assert workspace_root(Path(cargo), arguments, first) == first
    assert not target_override(arguments, {"CARGO_HOME": str(tmp_path / "cargo-home")}, first)


@requires_lsof
def test_target_management_cli_enable_and_migration_safety(tmp_path: Path):
    from herdr_engineering.cargo_host import install
    workspace, cache, state = build_real_fixture(tmp_path)
    home = tmp_path / "home"
    cargo = shutil.which("cargo")
    assert cargo
    install(home, Path(cargo), Path(sys.executable))
    config = tmp_path / "config.yaml"
    settings = {"fabric_build_cache": {"enabled": True, "local": {
        "min_free_disk_percent": 0, "storage_tiers": [{"name": "internal", "directory": str(cache)}]}}}
    config.write_text(yaml.safe_dump(settings))
    # Hermetic: an ambient target-dir override (common on CI agents) is "authoritative"
    # to the migrator and would hide the same-filesystem verdict this test asserts.
    ambient = {key: value for key, value in os.environ.items()
               if key not in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR")}
    env = {**ambient, "HERDR_DISK_GUARD_OVERRIDE": "1", "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "HERDR_ENGINEERING_CONFIG": str(config), "HERDR_ENGINEERING_STATE": str(state),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "herdr_engineering.cargo_host", *arguments],
                              env=env, text=True, capture_output=True, timeout=30)

    enabled = invoke("targets-enable", "--config", str(config))
    assert enabled.returncode == 0, enabled.stderr
    assert json.loads(enabled.stdout)["manage_targets"]
    assert config.with_name(config.name + ".before-herdr-target-storage").exists()
    assert invoke("targets-enable", "--config", str(config)).returncode == 0
    for coordinated in ("0", "1"):
        build = subprocess.run([str(home / ".local/bin/cargo"), "build", "--offline"],
                               cwd=workspace, env={**env, "HERDR_CARGO_HOST_COORDINATED": coordinated},
                               text=True, capture_output=True, timeout=60)
        assert build.returncode == 0, build.stderr
        assert (workspace / "target").is_dir() and not (workspace / "target").is_symlink()
    missing_roots = invoke("targets-migrate")
    assert missing_roots.returncode != 0 and "explicit project roots" in missing_roots.stderr
    unsafe = invoke("targets-migrate", "--root", str(workspace), "--apply")
    assert unsafe.returncode != 0 and "quiescent" in unsafe.stderr
    plan = invoke("targets-migrate", "--root", str(workspace))
    assert plan.returncode == 0, plan.stderr
    assert json.loads(plan.stdout)["targets"][0]["status"] == "preserved"
    assert "same filesystem" in json.loads(plan.stdout)["targets"][0]["reason"]
    explicit = subprocess.run([sys.executable, "-m", "herdr_engineering.cargo_host", "targets-migrate",
                               "--root", str(workspace)], env={**env, "CARGO_TARGET_DIR": str(tmp_path / "elsewhere")},
                              text=True, capture_output=True, timeout=30)
    assert explicit.returncode == 0, explicit.stderr
    assert "Explicit target setting" in json.loads(explicit.stdout)["targets"][0]["reason"]
    settings["fabric_build_cache"]["local"]["min_free_disk_percent"] = 101
    config.write_text(yaml.safe_dump(settings))
    assert invoke("targets-migrate", "--root", str(workspace)).returncode != 0
    config.write_text("fabric_build_cache:\n  enabled: false\n")
    assert "Enable fabric_build_cache" in invoke("targets-enable", "--config", str(config)).stderr
    assert invoke("targets-migrate", "--root", str(workspace)).returncode != 0


@pytest.mark.integration
def test_migration_cli_uses_real_external_volume(tmp_path: Path):
    from herdr_engineering.cargo_host import install
    from herdr_engineering.cargo_target_migration import migration_run
    volume_text = os.environ.get("HERDR_CACHE_TEST_VOLUME")
    if not volume_text or sys.platform != "darwin":
        pytest.skip("real writable macOS test volume required")
    import tempfile

    from herdr_engineering.cargo_host import enroll_storage
    workspace, _cache, state = build_real_fixture(tmp_path)
    volume = Path(volume_text)
    if workspace.stat().st_dev == volume.stat().st_dev:
        pytest.skip("migration requires two actual filesystems")
    with tempfile.TemporaryDirectory(prefix="herdr-target-migration-", dir=volume) as external:
        home = tmp_path / "home"
        cargo = shutil.which("cargo")
        assert cargo
        install(home, Path(cargo), Path(sys.executable))
        config = tmp_path / "config.yaml"
        config.write_text("fabric_build_cache:\n  enabled: true\n  local:\n    min_free_disk_percent: 0\n")
        enroll_storage(volume, "test", str(Path(external).relative_to(volume) / "compiler-cache"), config)
        rustup = os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup"))
        previous = {key: os.environ.get(key) for key in ("HOME", "RUSTUP_HOME", "HERDR_ENGINEERING_CONFIG", "HERDR_ENGINEERING_STATE")}
        try:
            os.environ.update(HOME=str(home), RUSTUP_HOME=rustup, HERDR_ENGINEERING_CONFIG=str(config), HERDR_ENGINEERING_STATE=str(state))
            plan = migration_run([workspace])
            assert plan["targets"][0]["status"] == "eligible"
            result = migration_run([workspace], apply=True, quiescent=True)
            assert result["targets"][0]["status"] == "moved_verified"
            assert (workspace / "target").is_symlink()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _hold_open(path: Path, *, cwd: Path | None = None, name: str | None = None) -> subprocess.Popen[bytes]:
    """A real process that keeps ``path`` open (or, named like a compiler, sits in ``cwd``)."""
    sleep = shutil.which("sleep")
    assert sleep
    if name is not None:
        program = path.parent / name
        shutil.copy(sleep, program)
        process = subprocess.Popen([str(program), "30"], cwd=cwd)
    else:
        process = subprocess.Popen(["tail", "-f", str(path)], stdout=subprocess.DEVNULL)
    return process


def _wait_until_listed(predicate) -> None:
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("process never appeared in the native open-file listing")


@requires_lsof
def test_idle_check_reports_open_files_and_active_compilers(tmp_path: Path):
    from herdr_engineering.cargo_target_migration import assert_idle, process_paths
    workspace = tmp_path / "ws"
    target = workspace / "target"
    target.mkdir(parents=True)
    held = target / "held.log"
    held.write_text("x")
    assert_idle(target, workspace)
    reader = _hold_open(held)
    try:
        _wait_until_listed(lambda: held.resolve() in process_paths()[0])
        with pytest.raises(ValueError, match="open files"):
            assert_idle(target, workspace)
    finally:
        reader.kill()
        reader.wait()
    tools = tmp_path / "tools"
    tools.mkdir()
    compiler = _hold_open(tools / "x", cwd=workspace, name="rustc")
    try:
        _wait_until_listed(lambda: workspace.resolve() in process_paths()[1])
        with pytest.raises(ValueError, match="active compiler"):
            assert_idle(tmp_path / "elsewhere", workspace.resolve())
    finally:
        compiler.kill()
        compiler.wait()


@requires_lsof
def test_migration_replaces_a_stale_marker_and_a_prepared_destination(tmp_path: Path):
    from herdr_engineering.cargo_target_storage import TARGET_MARKER
    workspace, cache, state = build_real_fixture(tmp_path)
    target = workspace / "target"
    # A stray marker inside the old output must not survive: the copy gets a fresh, correct one.
    (target / TARGET_MARKER).write_text('{"stale": true}\n')
    result = migrate_target(target, cache, workspace, state, quiescent=True)
    assert result["status"] == "moved_verified"
    marker = json.loads((Path(str(result["destination"])) / TARGET_MARKER).read_text())
    assert marker["workspace"] == str(workspace.resolve()) and "stale" not in marker


@requires_lsof
def test_old_output_is_never_merged_into_a_newer_managed_target(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    target = workspace / "target"
    first = prepare_target(cache, workspace.resolve())
    (first / "newer-fingerprint").write_text("keep")
    result = migrate_target(target, cache, workspace, state, quiescent=True)
    destination = Path(str(result["destination"]))
    assert destination != first and "-legacy-" in destination.name
    assert (first / "newer-fingerprint").read_text() == "keep"


def _migration_environment(tmp_path: Path, workspace: Path, cache: Path, state: Path,
                           local: dict[str, object] | None = None) -> dict[str, str]:
    from herdr_engineering.cargo_host import install
    cargo = shutil.which("cargo")
    assert cargo
    home = tmp_path / "home"
    install(home, Path(cargo), Path(sys.executable))
    config = tmp_path / "migration.yaml"
    section: dict[str, object] = {"enabled": True, "local": {
        "min_free_disk_percent": 0, "storage_tiers": [{"name": "internal", "directory": str(cache)}],
        **(local or {})}}
    config.write_text(yaml.safe_dump({"fabric_build_cache": section}))
    ambient = {key: value for key, value in os.environ.items()
               if key not in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR")}
    return {**ambient, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
            "HERDR_ENGINEERING_CONFIG": str(config), "HERDR_ENGINEERING_STATE": str(state),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1])}


@requires_lsof
def test_migration_dry_run_and_apply_across_two_filesystems(tmp_path: Path):
    shm = Path("/dev/shm")
    if not shm.is_dir() or not os.access(shm, os.W_OK):
        pytest.skip("a second writable filesystem (/dev/shm) is required")
    workspace, _cache, state = build_real_fixture(tmp_path)
    if workspace.stat().st_dev == shm.stat().st_dev:
        pytest.skip("/dev/shm shares the project's filesystem")
    external = Path(tempfile.mkdtemp(prefix="herdr-migration-", dir=shm))
    try:
        env = _migration_environment(tmp_path, workspace, external, state)
        run = lambda *arguments: subprocess.run(  # noqa: E731
            [sys.executable, "-m", "herdr_engineering.cargo_host", "targets-migrate", "--root", str(workspace),
             *arguments], env=env, text=True, capture_output=True, timeout=120)
        plan = run()
        assert plan.returncode == 0, plan.stderr
        report = json.loads(plan.stdout)
        assert report["dry_run"] is True and report["targets"][0]["status"] == "eligible"
        assert not (workspace / "target").is_symlink()
        applied = run("--apply", "--quiescent")
        assert applied.returncode == 0, applied.stderr
        item = json.loads(applied.stdout)["targets"][0]
        assert item["status"] == "moved_verified" and (workspace / "target").is_symlink()
    finally:
        shutil.rmtree(external, ignore_errors=True)


@requires_lsof
def test_migration_run_rejects_unusable_storage_and_unconfigured_hosts(tmp_path: Path):
    workspace, cache, state = build_real_fixture(tmp_path)
    env = _migration_environment(tmp_path, workspace, cache, state, local={
        "storage_tiers": [{"name": "gone", "directory": "herdr-build/compiler-cache",
                           "volume_uuid": "11111111-2222-4333-8444-555555555555"}]})
    base = [sys.executable, "-m", "herdr_engineering.cargo_host", "targets-migrate", "--root", str(workspace)]
    result = subprocess.run(base, env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 1 and "No eligible storage tier" in result.stderr
    disabled = tmp_path / "disabled.yaml"
    disabled.write_text("fabric_build_cache:\n  enabled: false\n")
    result = subprocess.run(base, env={**env, "HERDR_ENGINEERING_CONFIG": str(disabled)},
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 1 and "configuration is required" in result.stderr


def test_planned_destination_is_the_managed_target_directory(tmp_path: Path):
    from herdr_engineering.cargo_target_migration import planned_destination
    assert planned_destination(tmp_path, tmp_path / "ws") == target_directory(tmp_path, tmp_path / "ws")
