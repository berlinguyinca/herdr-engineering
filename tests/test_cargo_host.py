"""Real-filesystem contracts for host-wide Cargo integration."""
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from herdr_engineering.build_cache_config import validate
from herdr_engineering.build_cache_env import CmdClass, _config_sets_wrapper, classify
from herdr_engineering.build_cache_location import (
    StorageCandidate,
    StorageTier,
    choose_storage,
    parse_storage_tiers,
    select_storage,
)
from herdr_engineering.cargo_host import (
    cargo_configuration,
    cargo_subcommand,
    enroll_storage,
    install,
    is_managed_wrapper,
)
from herdr_engineering.errors import InvalidError


def test_ordered_storage_tiers_are_valid_configuration():
    cfg = validate({"enabled": True, "local": {"storage_tiers": [
        {"name": "primary", "directory": "herdr-build/compiler-cache",
         "volume_uuid": "11111111-2222-4333-8444-555555555555"},
        {"name": "internal", "directory": "~/.cache/herdr-compiler"},
    ]}})
    assert [tier.name for tier in cfg.local.storage_tiers] == ["primary", "internal"]


def test_our_managed_compiler_adapter_does_not_disable_cache_policy(tmp_path: Path):
    home = tmp_path / "home"
    wrapper = home / ".local/share/herdr-engineering/cargo-host/rustc-cache"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n# herdr-managed-cargo-host-v1\n")
    cargo = home / ".cargo"
    cargo.mkdir()
    (cargo / "config.toml").write_text(f'[build]\nrustc-wrapper = "{wrapper}"\n')
    assert not _config_sets_wrapper(tmp_path, {"CARGO_HOME": str(cargo)})


def test_install_is_idempotent_preserves_settings_and_runs_real_cargo(tmp_path: Path):
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("real Cargo required")
    home = tmp_path / "home"
    (home / ".cargo").mkdir(parents=True)
    original = '[net]\nretry = 4\n'
    (home / ".cargo/config.toml").write_text(original)
    (home / ".zshrc").write_text("export EXISTING_SETTING=kept\n")
    install(home, Path(cargo), Path(sys.executable))
    install(home, Path(cargo), Path(sys.executable))
    config = tomllib.loads((home / ".cargo/config.toml").read_text())
    assert config["net"]["retry"] == 4
    assert config["build"]["incremental"] is False
    assert (home / ".cargo/config.toml.before-herdr-cargo-host").read_text() == original
    assert (home / ".zshrc").read_text().count("# >>> herdr-cargo-host >>>") == 1
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([str(home / ".local/bin/cargo"), "--version"], env=env,
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("cargo ")


def test_tier_order_and_unavailable_primary_fallback(tmp_path: Path):
    assert choose_storage((StorageCandidate("primary", tmp_path),
                           StorageCandidate("secondary", tmp_path / "second"))).name == "primary"
    selected = choose_storage((StorageCandidate("primary", None, "volume_unavailable"),
                               StorageCandidate("secondary", tmp_path)))
    assert selected.name == "secondary"
    assert selected.skipped == ("primary:volume_unavailable",)
    assert choose_storage((StorageCandidate("last", None, "low_free_disk"),)).directory is None


def test_real_storage_probe_respects_free_space_floor_and_creates_nothing(tmp_path: Path):
    target = tmp_path / "not-created"
    tiers = (StorageTier("internal", str(target)),)
    assert select_storage(tiers, tmp_path, 0).directory == target
    assert select_storage(tiers, tmp_path, 100).directory is None
    assert not target.exists()


@pytest.mark.parametrize("directory", ["/Volumes/missing/cache", "../cache"])
def test_external_directories_cannot_fall_back_to_stale_mount_paths(directory: str):
    with pytest.raises(InvalidError, match="internal directory"):
        parse_storage_tiers([{"name": "unsafe", "directory": directory}])


@pytest.mark.parametrize("value", [None, {}, ["bad"], [{"name": "x", "directory": "/tmp", "unknown": 1}],
                                   [{"name": "!", "directory": "/tmp"}],
                                   [{"name": "x", "directory": ""}],
                                   [{"name": "x", "directory": "cache", "volume_uuid": "bad"}],
                                   [{"name": "x", "directory": "../cache", "volume_uuid": "11111111-2222-4333-8444-555555555555"}]])
def test_invalid_storage_configuration_is_rejected(value: object):
    with pytest.raises(InvalidError):
        parse_storage_tiers(value)


def test_duplicate_tier_names_are_rejected():
    with pytest.raises(InvalidError, match="unique"):
        parse_storage_tiers([{"name": "same", "directory": "/tmp/a"},
                             {"name": "same", "directory": "/tmp/b"}])


def test_existing_operator_wrapper_is_not_replaced(tmp_path: Path):
    original = '[build]\nrustc-wrapper="/operator/custom-wrapper"\n'
    with pytest.raises(ValueError, match="operator"):
        cargo_configuration(original, tmp_path / "ours", tmp_path)


def test_own_config_update_handles_existing_build_table_and_missing_newline(tmp_path: Path):
    home = tmp_path / "home"
    wrapper = home / ".local/share/herdr-engineering/cargo-host/rustc-cache"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n# herdr-managed-cargo-host-v1\n")
    text = f'[build]\njobs=2\nincremental=true\nrustc-wrapper="{wrapper}"'
    updated = cargo_configuration(text, wrapper, home)
    assert tomllib.loads(updated)["build"] == {"jobs": 2, "incremental": False, "rustc-wrapper": str(wrapper)}
    assert "[build]\n" in cargo_configuration("[build]", wrapper, home)
    assert not is_managed_wrapper(str(tmp_path / "missing"), home)


def test_install_refuses_existing_foreign_launcher_and_symlink_configuration(tmp_path: Path):
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("real Cargo required")
    home = tmp_path / "home"
    launcher = home / ".local/bin/cargo"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("operator launcher")
    with pytest.raises(ValueError, match="not ours"):
        install(home, Path(cargo), Path(sys.executable))
    assert launcher.read_text() == "operator launcher"
    launcher.unlink()
    launcher.symlink_to(cargo)
    with pytest.raises(ValueError, match="symlink"):
        install(home, Path(cargo), Path(sys.executable))
    launcher.unlink()
    with pytest.raises(ValueError, match="recurse"):
        install(home, launcher, Path(sys.executable))
    with pytest.raises(ValueError, match="unavailable"):
        install(home, tmp_path / "missing", Path(sys.executable))
    config = home / ".cargo/config.toml"
    config.parent.mkdir()
    other = tmp_path / "other-config"
    other.write_text("[build]\njobs=2\n")
    config.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        install(home, Path(cargo), Path(sys.executable))
    assert other.read_text() == "[build]\njobs=2\n" and not launcher.exists()


@pytest.mark.parametrize("arguments", [["cargo", "--offline", "build"], ["cargo", "-q", "test"],
                                       ["cargo", "--color", "never", "c"], ["cargo", "+stable", "b"]])
def test_global_options_and_builtin_aliases_use_cache_policy(arguments: list[str]):
    assert classify(arguments) is CmdClass.CACHEABLE_CARGO


@pytest.mark.parametrize("arguments,expected", [([], None), (["--version"], None),
    (["-C", "/tmp", "build"], None), (["--color=never", "check"], "check"),
    (["--config", "build.jobs=2", "-vv", "t"], "test"), (["+stable", "--offline", "r"], "run"),
    (["--color", "never"], None), (["metadata"], "metadata")])
def test_cargo_global_option_classification(arguments: list[str], expected: str | None):
    assert cargo_subcommand(arguments) == expected


def test_install_preflights_shell_symlinks_before_publishing(tmp_path: Path):
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("real Cargo required")
    home = tmp_path / "home"
    home.mkdir()
    profile = tmp_path / "dotfile"
    profile.write_text("operator profile\n")
    (home / ".zshrc").symlink_to(profile)
    with pytest.raises(ValueError, match="symlink"):
        install(home, Path(cargo), Path(sys.executable))
    assert not (home / ".local/bin/cargo").exists()
    assert not (home / ".cargo/config.toml").exists()


def test_relative_cargo_path_is_stored_absolutely_and_alias_recursion_is_refused(tmp_path: Path):
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("real Cargo required")
    home = tmp_path / "home"
    install(home, Path(os.path.relpath(cargo)), Path(sys.executable))
    manifest = json.loads((home / ".local/share/herdr-engineering/cargo-host/install.json").read_text())
    assert Path(manifest["real_cargo"]).is_absolute()
    alias = tmp_path / "cargo-alias"
    alias.symlink_to(home / ".local/bin/cargo")
    with pytest.raises(ValueError, match="recurse"):
        install(home, alias, Path(sys.executable))


def test_real_compiler_still_runs_when_os_refuses_cache_probe_subprocess(tmp_path: Path):
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("requires non-root POSIX process limits")
    rustc = shutil.which("rustc")
    if rustc is None:
        pytest.skip("real Rust compiler required")
    config = tmp_path / "config.yaml"
    config.write_text("fabric_build_cache:\n  enabled: true\n  local:\n    min_free_disk_percent: 0\n")
    env = {**os.environ, "HERDR_ENGINEERING_CONFIG": str(config),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    code = ("import resource,sys; "
            "resource.setrlimit(resource.RLIMIT_NPROC,(0,resource.getrlimit(resource.RLIMIT_NPROC)[1])); "
            "from herdr_engineering.cargo_host import main; "
            "sys.exit(main(['rustc',sys.argv[1],'--version']))")
    result = subprocess.run([sys.executable, "-c", code, rustc], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    if "os error 11" in result.stderr:
        pytest.skip("runner cannot spawn rustc worker threads under RLIMIT_NPROC=0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rustc ")
    assert "cache unavailable" in result.stderr


def test_cli_install_status_and_errors_with_real_processes(tmp_path: Path):
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("real Cargo required")
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("fabric_build_cache:\n  enabled: false\n")
    env = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
           "HERDR_ENGINEERING_CONFIG": str(config), "XDG_CONFIG_HOME": str(home / ".config"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    base = [sys.executable, "-m", "herdr_engineering.cargo_host"]
    for arguments in (["install", "--real-cargo", cargo], ["install"]):
        result = subprocess.run(base + arguments, env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["cargo"] == str(home / ".local/bin/cargo")
    result = subprocess.run(base + ["status"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)["installed"]
    for arguments in (["rustc"], ["storage-add"]):
        result = subprocess.run(base + arguments, env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 1 and "cargo-host:" in result.stderr
    rustc = shutil.which("rustc")
    if rustc:
        result = subprocess.run(base + ["rustc", rustc, "--version"], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and result.stdout.startswith("rustc ")
        own = home / ".local/share/herdr-engineering/cargo-host/rustc-cache"
        result = subprocess.run(base + ["rustc", str(own)], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 1 and "Recursive" in result.stderr
    manifest = home / ".local/share/herdr-engineering/cargo-host/install.json"
    manifest.write_text(json.dumps({"real_cargo": str(tmp_path / "missing")}))
    result = subprocess.run(base + ["cargo", "--version"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and "repair" in result.stderr


@pytest.mark.integration
def test_enrollment_and_live_uuid_selection_on_real_volume(tmp_path: Path):
    import plistlib
    from uuid import uuid4
    if sys.platform != "darwin":
        pytest.skip("real macOS volume test")
    volume = os.environ.get("HERDR_CACHE_TEST_VOLUME")
    if not volume:
        pytest.skip("set HERDR_CACHE_TEST_VOLUME to an authorized test volume")
    name = "herdr-cache-test-" + uuid4().hex
    directory = ".agent-work/" + name
    config = tmp_path / "config.yaml"
    config.write_text("fabric_build_cache:\n  enabled: true\n")
    enrolled = enroll_storage(Path(volume), "primary", directory, config)
    cache = Path(enrolled["cache_directory"])
    try:
        import yaml
        result = subprocess.run([sys.executable, "-m", "herdr_engineering.cargo_host", "storage-add",
                                 "--volume", str(volume), "--directory", directory, "--config", str(config)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["cache_directory"] == str(cache)
        cfg = validate(yaml.safe_load(config.read_text())["fabric_build_cache"])
        selected = select_storage(cfg.local.storage_tiers, tmp_path / "default", 0)
        assert selected.name == "primary" and selected.directory == cache
        assert (cache / ".herdr-compiler-cache.json").is_file()
        with pytest.raises(InvalidError, match="escapes|relative"):
            enroll_storage(Path(volume), "unsafe", "../escape", config)
        assert json.loads((cache / ".herdr-compiler-cache.json").read_text())["volume_uuid"]
        info = plistlib.loads(subprocess.check_output(["diskutil", "info", "-plist", str(volume)]))
        assert enrolled["volume_uuid"].lower() == info["VolumeUUID"].lower()
        marker = cache / ".herdr-compiler-cache.json"
        marker.write_text(json.dumps({"volume_uuid": str(uuid4())}))
        assert select_storage(cfg.local.storage_tiers, tmp_path / "default", 0).name == "internal"
        marker.unlink()
        assert select_storage(cfg.local.storage_tiers, tmp_path / "default", 0).name == "internal"
    finally:
        shutil.rmtree(cache)


def _real_cargo_path() -> Path:
    cargo = shutil.which("cargo")
    if not cargo:
        pytest.skip("real Cargo required")
    return Path(cargo)


def test_install_into_new_home_captures_the_real_cargo_behind_an_existing_launcher(tmp_path: Path):
    cargo = _real_cargo_path()
    first, second = tmp_path / "first", tmp_path / "second"
    install(first, cargo, Path(sys.executable))
    result = install(second, first / ".local/bin/cargo", Path(sys.executable))
    manifest = json.loads((second / ".local/share/herdr-engineering/cargo-host/install.json").read_text())
    assert Path(manifest["real_cargo"]) == Path(os.path.abspath(cargo))
    assert result["cargo"] == str(second / ".local/bin/cargo")


def test_install_refuses_a_managed_chain_that_leads_back_to_the_new_launcher(tmp_path: Path):
    cargo = _real_cargo_path()
    first, second = tmp_path / "first", tmp_path / "second"
    install(first, cargo, Path(sys.executable))
    manifest = first / ".local/share/herdr-engineering/cargo-host/install.json"
    data = json.loads(manifest.read_text())
    data["real_cargo"] = str(second / ".local/bin/cargo")
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="recurse"):
        install(second, first / ".local/bin/cargo", Path(sys.executable))
    assert not (second / ".local/bin/cargo").exists()


def test_install_updates_a_legacy_cargo_config_in_place(tmp_path: Path):
    cargo = _real_cargo_path()
    home = tmp_path / "home"
    (home / ".cargo").mkdir(parents=True)
    (home / ".cargo/config").write_text("[net]\nretry = 2\n")
    result = install(home, cargo, Path(sys.executable))
    assert result["config"] == str(home / ".cargo/config")
    assert not (home / ".cargo/config.toml").exists()
    assert tomllib.loads((home / ".cargo/config").read_text())["net"]["retry"] == 2


def test_install_refuses_destinations_that_are_not_writable_regular_files(tmp_path: Path):
    cargo = _real_cargo_path()
    home = tmp_path / "home"
    (home / ".zshrc").mkdir(parents=True)
    with pytest.raises(ValueError, match="not a writable regular file"):
        install(home, cargo, Path(sys.executable))
    assert not (home / ".local/bin/cargo").exists()


def test_install_rolls_back_published_files_when_a_later_write_fails(tmp_path: Path):
    cargo = _real_cargo_path()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export KEPT=1\n")
    (home / ".cargo").mkdir()
    (home / ".cargo/config.toml").write_text("[net]\nretry = 3\n")
    # A stale temporary file makes the exclusive create of .bashrc's temporary fail with a real OSError
    # after the earlier files were already published.
    (home / ".bashrc.herdr-tmp").write_text("stale")
    with pytest.raises(FileExistsError):
        install(home, cargo, Path(sys.executable))
    assert (home / ".zshrc").read_text() == "export KEPT=1\n"
    assert (home / ".cargo/config.toml").read_text() == "[net]\nretry = 3\n"
    root = home / ".local/share/herdr-engineering/cargo-host"
    assert not (root / "rustc-cache").exists() and not (root / "install.json").exists()
    assert not (home / ".local/bin/cargo").exists() and not (home / ".zshenv").exists()


def _tier_config(tmp_path: Path, **local: object):
    return validate({"enabled": True, "local": {
        "storage_tiers": [{"name": "internal", "directory": str(tmp_path / "cache")}], **local}})


def test_disk_guard_prints_the_override_notice_and_allows_the_build(tmp_path: Path, monkeypatch, capsys):
    from herdr_engineering.cargo_host import _disk_guard_refusal
    from herdr_engineering.disk_guard import EXIT_REFUSED, OVERRIDE_ENV
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    full = {"enabled": True, "refuse_builds_percent": 0, "refuse_builds_min_free_gib": 10**9,
            "roots": [str(tmp_path)]}
    monkeypatch.setenv(OVERRIDE_ENV, "1")
    assert _disk_guard_refusal(tmp_path, full) is None
    assert f"proceeding because {OVERRIDE_ENV}=1" in capsys.readouterr().err
    monkeypatch.delenv(OVERRIDE_ENV)
    assert _disk_guard_refusal(tmp_path, full) == EXIT_REFUSED
    assert "refusing to start a new build" in capsys.readouterr().err
    assert _disk_guard_refusal(tmp_path, {"enabled": False}) is None
    assert capsys.readouterr().err == ""


def test_disk_guard_fault_never_blocks_a_build(tmp_path: Path, monkeypatch, capsys):
    from herdr_engineering.cargo_host import _disk_guard_refusal
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    assert _disk_guard_refusal(tmp_path, {"refuse_builds_percent": "not-a-number"}) is None
    assert "disk guard unavailable (ValueError)" in capsys.readouterr().err
    # Without a policy argument the configured one is loaded (hermetic: no config means defaults).
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(tmp_path / "absent.yaml"))
    assert _disk_guard_refusal(tmp_path) is None  # a fresh temp filesystem is nowhere near full


def test_reserve_selection_uses_first_tier_then_one_cleanup_then_reports(tmp_path: Path, monkeypatch):
    from herdr_engineering.cargo_host import _select_with_reserve
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    (tmp_path / "cache").mkdir()
    config = _tier_config(tmp_path)
    selected, cleaned = _select_with_reserve(config, {"enabled": True}, 0)
    assert selected.directory == tmp_path / "cache" and cleaned is False
    # A reserve no disk can meet, with cleanup switched off: nothing to try.
    selected, cleaned = _select_with_reserve(config, {"enabled": False}, 100)
    assert selected.directory is None and cleaned is False
    # Cleanup enabled over an empty root set removes nothing, so the refusal may not claim a cleanup.
    empty = tmp_path / "empty-root"
    empty.mkdir()
    selected, cleaned = _select_with_reserve(config, {"enabled": True, "roots": [str(empty)]}, 100)
    assert selected.directory is None and cleaned is False
    # A cleanup that cannot run (invalid thresholds) is reported and never crashes the build.
    selected, cleaned = _select_with_reserve(config, {"enabled": True, "roots": [str(empty)],
                                                      "high_water_percent": "x"}, 100)
    assert selected.directory is None and cleaned is False


def test_reserve_refusal_message_names_the_override(capsys):
    from herdr_engineering.cargo_host import _reserve_refusal
    from herdr_engineering.disk_guard import EXIT_REFUSED, OVERRIDE_ENV
    assert _reserve_refusal(12.5, False) == EXIT_REFUSED
    plain = capsys.readouterr().err
    assert "keeps 12.5% of its disk free: refusing" in plain and "even after" not in plain
    assert _reserve_refusal(12.5, True) == EXIT_REFUSED
    assert "even after cleaning up" in capsys.readouterr().err and OVERRIDE_ENV in plain


def _write_config(path: Path, **local: object) -> None:
    path.write_text(yaml.safe_dump({"fabric_build_cache": {"enabled": True, "local": local}}))


def test_enable_targets_validates_registers_and_is_repeatable(tmp_path: Path, monkeypatch):
    from herdr_engineering.cargo_host import enable_targets
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    config = tmp_path / "config.yaml"
    config.write_text("fabric_build_cache:\n  enabled: false\n")
    with pytest.raises(ValueError, match="Enable fabric_build_cache"):
        enable_targets(config)
    _write_config(config, min_free_disk_percent=0,
                  storage_tiers=[{"name": "internal", "directory": str(tmp_path / "cache")}])
    (tmp_path / "cache").mkdir()
    for _ in range(2):  # the second run keeps the first backup
        result = enable_targets(config)
        assert result["manage_targets"] is True and result["registered_existing_aliases"] == 0
    saved = yaml.safe_load(config.read_text())
    assert saved["fabric_build_cache"]["local"]["manage_targets"] is True
    backup = config.with_name(config.name + ".before-herdr-target-storage")
    assert "manage_targets" not in backup.read_text()


def test_enable_retention_requires_managed_targets_and_keeps_first_backup(tmp_path: Path, monkeypatch):
    from herdr_engineering.cargo_host import enable_retention
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    config = tmp_path / "config.yaml"
    _write_config(config, min_free_disk_percent=0, manage_targets=False)
    with pytest.raises(ValueError, match="managed Cargo targets"):
        enable_retention(config, schedule=False)
    _write_config(config, min_free_disk_percent=0, manage_targets=True)
    first = enable_retention(config, schedule=False)
    assert first["target_retention_enabled"] is True and first["job"] is None
    assert yaml.safe_load(config.read_text())["fabric_build_cache"]["local"]["target_retention_enabled"] is True
    backup = config.with_name(config.name + ".before-herdr-target-retention")
    assert "target_retention_enabled" not in backup.read_text()
    enable_retention(config, schedule=False)  # idempotent: backup is not overwritten
    assert "target_retention_enabled" not in backup.read_text()


def _launcher_env(home: Path, config: Path, state: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "RUSTC_WRAPPER",
                          "HERDR_DISK_GUARD_OVERRIDE", "HERDR_CARGO_HOST_COORDINATED")}
    return {**env, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
            "HERDR_ENGINEERING_CONFIG": str(config), "HERDR_ENGINEERING_STATE": str(state),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]), **extra}


def _tiny_workspace(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text('[package]\nname="reserve-probe"\nversion="0.1.0"\nedition="2021"\n')
    (root / "src/lib.rs").write_text("pub fn one() -> u32 { 1 }\n")
    return root


def test_managed_build_honours_free_space_reserve_override_and_cleanup_policy(tmp_path: Path):
    cargo = _real_cargo_path()
    home = tmp_path / "home"
    install(home, cargo, Path(sys.executable))
    workspace = _tiny_workspace(tmp_path / "project")
    config = tmp_path / "config.yaml"
    cache = tmp_path / "cache"
    cache.mkdir()
    # A reserve of 99% free can never be met; cleanup is off, so the tiers are not retried after a sweep.
    config.write_text(yaml.safe_dump({
        "build_cleanup": {"enabled": False},
        "fabric_build_cache": {"enabled": True, "local": {
            "manage_targets": True, "min_free_disk_percent": 99,
            "storage_tiers": [{"name": "internal", "directory": str(cache)}]}}}))
    launcher = str(home / ".local/bin/cargo")
    refused = subprocess.run([launcher, "build", "--offline"], cwd=workspace, text=True, capture_output=True,
                             timeout=60, env=_launcher_env(home, config, tmp_path / "state"))
    assert refused.returncode != 0 and "keeps 99% of its disk free" in refused.stderr
    assert not (workspace / "target").exists()
    allowed = subprocess.run([launcher, "build", "--offline"], cwd=workspace, text=True, capture_output=True,
                             timeout=90, env=_launcher_env(home, config, tmp_path / "state",
                                                           HERDR_DISK_GUARD_OVERRIDE="1"))
    assert allowed.returncode == 0, allowed.stderr
    assert "proceeding because HERDR_DISK_GUARD_OVERRIDE=1" in allowed.stderr
    assert (workspace / "target").is_symlink()


def test_cargo_clean_skips_the_disk_guard_and_other_subcommands_pass_through(tmp_path: Path):
    cargo = _real_cargo_path()
    home = tmp_path / "home"
    install(home, cargo, Path(sys.executable))
    workspace = _tiny_workspace(tmp_path / "project")
    cache = tmp_path / "cache"
    cache.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "build_cleanup": {"refuse_builds_percent": 0, "refuse_builds_min_free_gib": 1000000000},
        "fabric_build_cache": {"enabled": True, "local": {
            "manage_targets": True, "min_free_disk_percent": 0,
            "storage_tiers": [{"name": "internal", "directory": str(cache)}]}}}))
    env = _launcher_env(home, config, tmp_path / "state")
    launcher = str(home / ".local/bin/cargo")
    cleaned = subprocess.run([launcher, "clean", "--offline"], cwd=workspace, text=True, capture_output=True,
                             timeout=60, env=env)
    assert cleaned.returncode == 0, cleaned.stderr
    assert "refusing to start" not in cleaned.stderr
    built = subprocess.run([launcher, "build", "--offline"], cwd=workspace, text=True, capture_output=True,
                           timeout=60, env=env)
    assert built.returncode != 0 and "refusing to start a new build" in built.stderr
    version = subprocess.run([launcher, "--version"], cwd=workspace, text=True, capture_output=True,
                             timeout=30, env=env)
    assert version.returncode == 0 and version.stdout.startswith("cargo ")


def test_managed_build_without_any_writable_tier_is_not_started(tmp_path: Path):
    cargo = _real_cargo_path()
    home = tmp_path / "home"
    install(home, cargo, Path(sys.executable))
    workspace = _tiny_workspace(tmp_path / "project")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"fabric_build_cache": {"enabled": True, "local": {
        "manage_targets": True, "min_free_disk_percent": 0,
        "storage_tiers": [{"name": "gone", "directory": "herdr-build/compiler-cache",
                           "volume_uuid": "11111111-2222-4333-8444-555555555555"}]}}}))
    result = subprocess.run([str(home / ".local/bin/cargo"), "build", "--offline"], cwd=workspace, text=True,
                            capture_output=True, timeout=60, env=_launcher_env(home, config, tmp_path / "state"))
    assert result.returncode == 1 and "No writable storage tier" in result.stderr
    assert not (workspace / "target").exists()
