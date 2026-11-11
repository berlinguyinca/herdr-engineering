"""Real-filesystem contracts for host-wide Cargo integration."""
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

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
