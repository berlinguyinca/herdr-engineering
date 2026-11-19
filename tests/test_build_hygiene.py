"""Disk hygiene defaults: pressure cleanup, one managed target per worktree, build guard, install and doctor.

Real filesystems, real git repositories, real processes and a real Cargo; no mocks. Pressure is
produced by thresholds against the real measured usage (0% is always exceeded, 100% never is).
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_hygiene, disk_guard
from herdr_engineering.build_cache_config import validate
from herdr_engineering.config import load_config
from herdr_engineering.doctor import DoctorReport, _add_build_hygiene

REAL_HOME = Path.home()
CACHEDIR_TAG = "Signature: 8a477f597d28d172789f06886806bc55\n# Cargo target\n"


def make_target(root: Path, name: str = "project", age_hours: float = 48) -> Path:
    project = root / name
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
    cache = project / "target"
    (cache / "debug/.fingerprint").mkdir(parents=True)
    (cache / "debug/artifact").write_bytes(b"x" * 4096)
    stamp = time.time() - age_hours * 3600
    for path in [*cache.rglob("*"), cache]:
        os.utime(path, (stamp, stamp))
    return cache


def make_pool_target(slot: Path, age_hours: float = 48) -> Path:
    """A cache-only Cargo target (no manifest beside it), as the shared pool and admission dir are."""
    cache = slot / "target"
    (cache / "debug/.fingerprint").mkdir(parents=True)
    (cache / "CACHEDIR.TAG").write_text(CACHEDIR_TAG)
    (cache / "debug/artifact").write_bytes(b"x" * 4096)
    stamp = time.time() - age_hours * 3600
    for path in [*cache.rglob("*"), cache]:
        os.utime(path, (stamp, stamp))
    return cache


def sweep(root: Path, **kwargs):
    base = {"high_water": 1, "low_water": 0, "min_age_hours": 24}
    return cleanup.run_once([root], root.parent / "state", **{**base, **kwargs})


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Sweeps include the default tier's pools under the home directory: never the developer's own."""
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "projects"
    path.mkdir()
    return Path(os.path.realpath(path))


# --- A. defaults ---------------------------------------------------------------------------

def test_build_cleanup_and_cargo_host_are_on_by_default():
    config = load_config([])
    cleanup_cfg = config["build_cleanup"]
    assert cleanup_cfg["enabled"] is True
    assert cleanup_cfg["roots"] == ["~/IdeaProjects", "~/work", "~/.cache"]
    assert cleanup_cfg["pressure_percent"] == 90 and cleanup_cfg["pressure_min_age_hours"] == 2
    cache = validate(config["fabric_build_cache"])
    assert cache.enabled and cache.local.manage_targets
    assert cache.local.manage_targets


def test_explicit_opt_out_is_honoured(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"build_cleanup": {"enabled": False},
                                    "fabric_build_cache": {"enabled": False}}))
    config = load_config([path])
    assert config["build_cleanup"]["enabled"] is False
    assert config["build_cleanup"]["roots"] == ["~/IdeaProjects", "~/work", "~/.cache"]
    assert validate(config["fabric_build_cache"]).enabled is False


# --- B. pressure mode ----------------------------------------------------------------------

def test_pressure_allows_two_hour_idle_targets(root):
    idle = make_target(root, age_hours=3)
    result = sweep(root, pressure_water=0)
    assert result["pressure"] is True and result["removed_count"] == 1
    assert not idle.exists()


def test_without_pressure_the_normal_floor_applies(root):
    idle = make_target(root, age_hours=3)
    result = sweep(root, pressure_water=100)
    assert result["pressure"] is False and result["removed_count"] == 0
    assert idle.exists()


def test_pressure_still_protects_recent_targets(root):
    recent = make_target(root, age_hours=0.5)
    assert sweep(root, pressure_water=0)["removed_count"] == 0
    assert recent.exists()


def test_idle_floor_is_configurable_but_never_below_one_hour(root):
    make_target(root, age_hours=2)
    assert sweep(root, min_age_hours=1, pressure_water=100)["removed_count"] == 1
    with pytest.raises(ValueError):
        sweep(root, min_age_hours=0.5)
    with pytest.raises(ValueError):
        sweep(root, pressure_min_age_hours=0.5)


def test_pressure_never_touches_a_target_a_live_process_uses(root):
    cache = make_target(root, age_hours=5)
    process = subprocess.Popen(["sleep", "30"], cwd=cache.parent)
    try:
        time.sleep(0.3)
        result = sweep(root, pressure_water=0)
    finally:
        process.kill()
        process.wait()
    assert result["removed_count"] == 0 and cache.exists()
    assert any(item["reason"] == "active project" for item in result["skipped"])


def test_idle_retired_pool_target_is_swept(root):
    cache = make_pool_target(root / "repo-1" / "tc-0")
    assert sweep(root, pressure_water=0)["removed_count"] == 1
    assert not cache.exists()


def test_cache_only_directory_without_cachedir_tag_is_not_recognised(root):
    cache = make_pool_target(root / "repo-2" / "tc-0")
    (cache / "CACHEDIR.TAG").unlink()
    assert sweep(root, pressure_water=0)["removed_count"] == 0
    assert cache.exists()


# --- C. helpers -----------------------------------------------------------------------------

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


# --- D. disk-budget guard ------------------------------------------------------------------

def guard_policy(root: Path, refuse: float) -> dict:
    return {"enabled": True, "roots": [str(root)], "high_water_percent": 1, "low_water_percent": 0,
            "min_age_hours": 24, "pressure_percent": 0, "pressure_min_age_hours": 2,
            "refuse_builds_percent": refuse, "refuse_builds_min_free_gib": 1e12}


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "hstate"))
    return tmp_path / "gstate"


def test_guard_allows_builds_below_the_limit_without_cleaning(root, state_env):
    cache = make_target(root, age_hours=5)
    decision = disk_guard.check_build_allowed(root, guard_policy(root, 100), state_env, env={})
    assert decision.allowed and not decision.cleanup_ran and cache.exists()


def test_guard_refuses_clearly_after_running_one_pressure_cleanup(root, state_env):
    cache = make_target(root, age_hours=5)
    decision = disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={})
    assert not decision.allowed and decision.cleanup_ran
    assert "refusing to start a new build" in decision.message
    assert disk_guard.OVERRIDE_ENV in decision.message
    assert not cache.exists()  # the pressure cleanup reclaimed the idle target
    later = make_target(root, "later", age_hours=5)
    again = disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={})
    assert not again.allowed and not again.cleanup_ran and later.exists()  # triggered once


def test_guard_override_env_lets_the_build_through_untouched(root, state_env):
    cache = make_target(root, age_hours=5)
    decision = disk_guard.check_build_allowed(
        root, guard_policy(root, 0), state_env, env={disk_guard.OVERRIDE_ENV: "1"})
    assert decision.allowed and not decision.cleanup_ran and cache.exists()
    assert disk_guard.OVERRIDE_ENV in decision.message


# --- E. cargo wrapper, real Cargo ----------------------------------------------------------

def cargo_env(tmp_path: Path, extra: dict | None = None, refuse: float | None = None):
    cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
    if not (cargo and rustc):
        pytest.skip("real cargo and rustc are required")
    from herdr_engineering.cargo_host import install
    home = tmp_path / "home"
    install(home, Path(cargo), Path(sys.executable))
    settings: dict = {"fabric_build_cache": {"local": {"capacity_gib": 1, "min_free_disk_percent": 0}}}
    if refuse is not None:
        settings["build_cleanup"] = {"refuse_builds_percent": refuse, "refuse_builds_min_free_gib": 1e12}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(settings))
    env = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(REAL_HOME / ".rustup")),
           "RUSTC": rustc, "HERDR_ENGINEERING_CONFIG": str(config_path),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "PATH": str(home / ".local/bin") + os.pathsep + os.environ["PATH"], **(extra or {})}
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "SCCACHE_DIR", "SCCACHE_SERVER_PORT",
                "CARGO_TARGET_DIR", "CARGO_INCREMENTAL", "HERDR_CARGO_HOST_COORDINATED",
                disk_guard.OVERRIDE_ENV):
        env.pop(key, None)
    env.update(extra or {})
    return home, env


def crate_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "Cargo.toml").write_text('[package]\nname="shared-demo"\nversion="0.1.0"\nedition="2021"\n')
    (repo / "src/main.rs").write_text("fn main() {}\n")
    git("init", "-q", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature", cwd=repo)
    return repo, tmp_path / "wt"


def build(cwd: Path, home: Path, env: dict):
    return subprocess.run([str(home / ".local/bin/cargo"), "build", "--offline"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=180)


def managed_targets(tmp_path: Path) -> list[Path]:
    return sorted(p for p in (tmp_path / "state/build-gates/targets").glob("*-*") if p.is_dir())


@pytest.mark.integration
def test_default_install_gives_each_worktree_its_own_incremental_free_target(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, worktree = crate_with_worktree(tmp_path)
    for cwd in (repo, worktree):
        result = build(cwd, home, env)
        assert result.returncode == 0, result.stderr
    targets = managed_targets(tmp_path)
    assert len(targets) == 2
    for target in targets:
        debug = target / "debug"
        assert (debug / "shared-demo").is_file()
        # CARGO_INCREMENTAL=0 applied by the wrapper: no incremental artifacts (cargo may keep an empty dir)
        assert not any((debug / "incremental").rglob("*"))
    assert not (tmp_path / "state/build-gates/shared-targets").exists()  # no slot pool any more
    for checkout in (repo, worktree):  # ./target stays usable: a managed alias to the worktree's own target
        assert (checkout / "target").is_symlink()
        assert (checkout / "target/debug/shared-demo").is_file()
        assert (checkout / "target").resolve() in {target.resolve() for target in targets}
    assert (repo / "target").resolve() != (worktree / "target").resolve()


@pytest.mark.integration
def test_concurrent_builds_of_one_worktree_share_its_single_target(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, _ = crate_with_worktree(tmp_path)
    for _ in range(2):
        assert build(repo, home, env).returncode == 0
    assert len(managed_targets(tmp_path)) == 1


@pytest.mark.integration
def test_explicit_cargo_target_dir_is_respected(tmp_path):
    explicit = tmp_path / "mine"
    home, env = cargo_env(tmp_path, {"CARGO_TARGET_DIR": str(explicit)})
    repo, _ = crate_with_worktree(tmp_path)
    assert build(repo, home, env).returncode == 0
    assert (explicit / "debug/shared-demo").is_file()
    assert not managed_targets(tmp_path)


@pytest.mark.integration
def test_wrapper_refuses_builds_on_a_full_disk_and_override_allows_them(tmp_path):
    home, env = cargo_env(tmp_path, refuse=0)
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED
    assert "refusing to start a new build" in refused.stderr
    allowed = build(repo, home, {**env, disk_guard.OVERRIDE_ENV: "1"})
    assert allowed.returncode == 0, allowed.stderr


# --- F. install and G. doctor --------------------------------------------------------------

def test_launchd_job_is_a_periodic_one_shot_sweep(tmp_path):
    result = build_hygiene.install_cleanup_job(tmp_path, Path("/opt/herdr-eng"), platform="darwin",
                                              interval_seconds=600, activate=False)
    plist = plistlib.loads(Path(result["job"]).read_bytes())
    assert plist["ProgramArguments"] == ["/opt/herdr-eng", "build-cleanup", "run"]
    assert plist["StartInterval"] == 600 and plist["Nice"] == 19


def test_systemd_unit_runs_the_watcher(tmp_path):
    result = build_hygiene.install_cleanup_job(tmp_path, Path("/opt/herdr-eng"), platform="linux",
                                              activate=False)
    text = Path(result["job"]).read_text()
    assert 'ExecStart="/opt/herdr-eng" build-cleanup watch' in text and "WantedBy=default.target" in text


def test_install_honours_opt_out(tmp_path):
    config = {"build_cleanup": {"enabled": False}, "fabric_build_cache": {"enabled": False}}
    result = build_hygiene.install(tmp_path, Path("/opt/herdr-eng"), activate=False, config=config)
    assert "skipped" in result["build_cleanup"] and "skipped" in result["cargo_host"]
    assert not any(tmp_path.rglob("*.plist")) and not (tmp_path / ".local").exists()


def test_install_by_default_installs_job_and_cargo_launcher_and_doctor_sees_them(tmp_path, monkeypatch):
    if not shutil.which("cargo"):
        pytest.skip("real cargo is required")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    before = DoctorReport()
    _add_build_hygiene(before, load_config([]))
    names = {c.name: c for c in before.checks}
    assert names["build_cleanup"].status == "warn" and names["cargo_host"].status == "warn"
    result = build_hygiene.install(tmp_path, Path("/opt/herdr-eng"), activate=False,
                                  config=load_config([]))
    assert Path(result["build_cleanup"]["job"]).is_file()
    assert Path(result["cargo_host"]["cargo"]).is_file()
    after = DoctorReport()
    _add_build_hygiene(after, load_config([]))
    names = {c.name: c for c in after.checks}
    # Installed but never loaded (activate=False) is reported as such where launchd can be asked.
    unloaded = sys.platform == "darwin" and build_hygiene._launchd_loaded() is False
    assert names["build_cleanup"].status == ("warn" if unloaded else "ok")
    assert names["cargo_host"].status == "ok"
    assert names["disk_pressure"].data["usage_percent"] >= 0
    json.dumps(after.to_dict())


@pytest.mark.integration
def test_unmanaged_target_directory_is_left_alone(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, _ = crate_with_worktree(tmp_path)
    (repo / "target").mkdir()
    (repo / "target/keep.txt").write_text("mine")
    result = build(repo, home, env)
    assert result.returncode == 0, result.stderr
    assert (repo / "target/keep.txt").read_text() == "mine" and not (repo / "target").is_symlink()
    assert (repo / "target/debug/shared-demo").is_file()
    assert not managed_targets(tmp_path)


@pytest.mark.integration
def test_cargo_clean_cleans_only_its_own_worktree_target(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, worktree = crate_with_worktree(tmp_path)
    for cwd in (repo, worktree):
        assert build(cwd, home, env).returncode == 0
    mine, theirs = ((cwd / "target").resolve() for cwd in (repo, worktree))
    cleaned = subprocess.run([str(home / ".local/bin/cargo"), "clean"], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=120)
    assert cleaned.returncode == 0, cleaned.stderr
    assert not (mine / "debug").exists() and (theirs / "debug/shared-demo").is_file()


# --- review round 1 fixes -------------------------------------------------------------------

def run_cli(tmp_path: Path, *args: str, settings: dict | None = None, extra_env: dict | None = None):
    config_path = tmp_path / "cli-config.yaml"
    config_path.write_text(yaml.safe_dump(settings or {}))
    env = {**os.environ, "HERDR_ENGINEERING_CONFIG": str(config_path),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "cli-state"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]), **(extra_env or {})}
    env.pop(disk_guard.OVERRIDE_ENV, None)
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", *args], env=env,
                          capture_output=True, text=True, timeout=120)


def test_fleet_cleanup_installer_failure_is_recorded_not_fatal(tmp_path):
    from herdr_engineering import fleet
    installer = tmp_path / "build-cleanup.sh"
    installer.write_text("#!/bin/sh\nexit 3\n")
    report: dict = {"steps": {}}

    def step(name, cmd, timeout):
        rc, _, err = fleet._run(cmd, timeout=timeout)
        report["steps"][name] = {"ok": rc == 0, "rc": rc, "stderr": err.strip() or None}
        return rc == 0, ""

    assert fleet._install_controller_cleanup(installer, step, report, have_bus=True) is False  # must not raise
    assert report["steps"]["controller_cleanup"]["ok"] is False
    assert report["steps"]["controller_cleanup"]["rc"] == 3


def test_fleet_cleanup_installer_is_skipped_without_a_systemd_user_bus(tmp_path):
    from herdr_engineering import fleet
    installer = tmp_path / "build-cleanup.sh"
    ran = tmp_path / "ran"
    installer.write_text(f"#!/bin/sh\ntouch {ran}\n")
    report: dict = {"steps": {}}

    def step(name, cmd, timeout):  # the real runner: it would run the installer if asked to
        rc, _, err = fleet._run(cmd, timeout=timeout)
        report["steps"][name] = {"ok": rc == 0, "rc": rc, "stderr": err.strip() or None}
        return rc == 0, ""

    assert fleet._install_controller_cleanup(installer, step, report, have_bus=False) is False
    assert report["steps"]["controller_cleanup"]["skipped"] == "no systemd user bus"
    assert not ran.exists()


def test_job_command_is_the_venv_binary_or_python_dash_m_never_a_module_path():
    command = build_hygiene.default_command()
    assert command[0] == sys.executable or (Path(command[0]).name == "herdr-eng"
                                            and Path(command[0]).parent == Path(sys.executable).parent)
    if command[0] == sys.executable:
        assert command[1:] == ["-m", "herdr_engineering.cli"]
    assert not any(part.endswith(".py") for part in command)


def test_install_writes_a_runnable_job_command_and_keeps_a_working_job(tmp_path):
    fallback = [sys.executable, "-m", "herdr_engineering.cli"]
    config = {"build_cleanup": {"enabled": True}, "fabric_build_cache": {"enabled": False}}
    result = build_hygiene.install(tmp_path, fallback, platform="darwin", activate=False, config=config)
    plist = plistlib.loads(Path(result["build_cleanup"]["job"]).read_bytes())
    assert plist["ProgramArguments"] == [*fallback, "build-cleanup", "run"]
    # A working job with a real binary is not replaced by the `-m` fallback.
    working = tmp_path / "second"
    build_hygiene.install_cleanup_job(working, Path(sys.executable), platform="darwin", activate=False)
    job = working / "Library/LaunchAgents" / f"{build_hygiene.LAUNCHD_LABEL}.plist"
    before = job.read_bytes()
    build_hygiene.install(working, fallback, platform="darwin", activate=False, config=config)
    assert plistlib.loads(job.read_bytes())["ProgramArguments"][:1] == [sys.executable]
    assert job.read_bytes() == before  # same command and interval: byte-identical rewrite
    # A changed interval is applied even though the working command is kept.
    faster = {**config, "build_cleanup": {"enabled": True, "interval_seconds": 120}}
    build_hygiene.install(working, fallback, platform="darwin", activate=False, config=faster)
    refreshed = plistlib.loads(job.read_bytes())
    assert refreshed["StartInterval"] == 120 and refreshed["ProgramArguments"][:1] == [sys.executable]
    assert "-m" not in refreshed["ProgramArguments"]


def test_systemd_unit_quotes_every_argument_of_the_command(tmp_path):
    result = build_hygiene.install_cleanup_job(
        tmp_path, [sys.executable, "-m", "herdr_engineering.cli"], platform="linux", activate=False)
    text = Path(result["job"]).read_text()
    assert f'ExecStart="{sys.executable}" "-m" "herdr_engineering.cli" build-cleanup watch' in text


def test_jobs_pin_the_state_and_config_the_builds_use(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "custom-state"))
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(tmp_path / "custom.yaml"))
    (tmp_path / "custom-state").mkdir()
    mac = build_hygiene.install_cleanup_job(tmp_path / "mac", Path("/opt/herdr-eng"), platform="darwin",
                                           activate=False)
    environment = plistlib.loads(Path(mac["job"]).read_bytes())["EnvironmentVariables"]
    assert environment["HERDR_ENGINEERING_STATE"] == str((tmp_path / "custom-state").resolve())
    assert environment["HERDR_ENGINEERING_CONFIG"] == str(tmp_path / "custom.yaml")
    linux = build_hygiene.install_cleanup_job(tmp_path / "lin", Path("/opt/herdr-eng"), platform="linux",
                                             activate=False)
    text = Path(linux["job"]).read_text()
    assert f'Environment="HERDR_ENGINEERING_STATE={(tmp_path / "custom-state").resolve()}"' in text
    assert f'Environment="HERDR_ENGINEERING_CONFIG={tmp_path / "custom.yaml"}"' in text


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_opt_out_removes_an_installed_job(tmp_path, platform):
    installed = build_hygiene.install_cleanup_job(tmp_path, Path("/opt/herdr-eng"), platform=platform,
                                                 activate=False)
    assert Path(installed["job"]).is_file()
    config = {"build_cleanup": {"enabled": False}, "fabric_build_cache": {"enabled": False}}
    result = build_hygiene.install(tmp_path, Path("/opt/herdr-eng"), platform=platform, activate=False,
                                  config=config)
    assert result["build_cleanup"]["removed"] == installed["job"]
    assert not Path(installed["job"]).exists()


def test_build_cleanup_run_exits_early_when_disabled(tmp_path, root):
    cache = make_target(root)
    refused = run_cli(tmp_path, "build-cleanup", "run", "--root", str(root),
                      settings={"build_cleanup": {"enabled": False, "high_water_percent": 1,
                                                  "low_water_percent": 0}})
    assert refused.returncode == 0 and json.loads(refused.stdout)["status"] == "disabled"
    assert cache.exists()
    swept = run_cli(tmp_path, "build-cleanup", "run", "--root", str(root),
                    settings={"build_cleanup": {"high_water_percent": 1, "low_water_percent": 0}})
    assert swept.returncode == 0 and not cache.exists()


def test_guard_busy_sweep_does_not_use_up_the_cooldown(root, state_env):
    cache = make_target(root, age_hours=5)
    state_env.mkdir(parents=True)
    fd = os.open(state_env / "build-cleanup.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another sweep is running
    try:
        busy = disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={})
    finally:
        os.close(fd)
    assert not busy.allowed and not busy.cleanup_ran and cache.exists()
    assert not (state_env / "disk-guard.json").exists()
    later = disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={})
    assert not later.allowed and later.cleanup_ran and not cache.exists()


def test_guard_still_refuses_when_the_cooldown_stamp_cannot_be_written(root, state_env):
    cache = make_target(root, age_hours=5)
    state_env.mkdir(parents=True)
    (state_env / "disk-guard.json").mkdir()  # an unwritable stamp, as on a full disk
    decision = disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={})
    assert not decision.allowed and decision.cleanup_ran and not cache.exists()
    assert disk_guard.OVERRIDE_ENV in decision.message


def test_guard_does_not_refuse_a_disk_with_ample_free_space(root, state_env):
    policy = {**guard_policy(root, 0), "refuse_builds_min_free_gib": 0}
    decision = disk_guard.check_build_allowed(root, policy, state_env, env={})
    assert decision.allowed and not decision.cleanup_ran


def test_guard_is_off_when_build_cleanup_is_disabled(root, state_env):
    policy = {**guard_policy(root, 0), "enabled": False}
    assert disk_guard.check_build_allowed(root, policy, state_env, env={}).allowed


def test_guard_measures_the_nearest_existing_ancestor_of_the_build_output(root, state_env):
    decision = disk_guard.check_build_allowed(root / "not/yet/created", guard_policy(root, 101),
                                              state_env, env={})
    assert decision.allowed and 0 <= decision.usage_percent <= 100
    percent, free_gib = disk_guard.measure(root)
    assert 0 <= percent <= 100 and free_gib > 0


def test_real_cargo_prefers_the_rustup_proxy_over_path(tmp_path):
    proxy_dir = tmp_path / "home/.cargo/bin"
    distro = tmp_path / "usr/bin"
    proxy_dir.mkdir(parents=True)
    distro.mkdir(parents=True)
    binary = Path(shutil.which("true"))
    shutil.copy(binary, proxy_dir / "cargo")
    shutil.copy(binary, distro / "cargo")
    resolved = build_hygiene.resolve_real_cargo(tmp_path / "home", path_env=str(distro))
    assert resolved == proxy_dir / "cargo"


def test_real_cargo_respects_a_valid_recorded_install(tmp_path):
    home = tmp_path / "home"
    (home / ".cargo/bin").mkdir(parents=True)
    recorded = tmp_path / "recorded/cargo"
    recorded.parent.mkdir()
    shutil.copy(shutil.which("true"), recorded)
    shutil.copy(shutil.which("true"), home / ".cargo/bin/cargo")
    from herdr_engineering.cargo_host import host_root
    (host_root(home)).mkdir(parents=True)
    (host_root(home) / "install.json").write_text(json.dumps({"real_cargo": str(recorded)}))
    assert build_hygiene.resolve_real_cargo(home, path_env="") == recorded


# --- review round 2 fixes -------------------------------------------------------------------

@pytest.mark.integration
def test_two_worktrees_build_into_separate_targets_and_keep_their_own_binaries(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, worktree = crate_with_worktree(tmp_path)
    assert build(repo, home, env).returncode == 0
    binary = (repo / "target/debug/shared-demo").resolve()
    before = binary.stat().st_mtime_ns
    (worktree / "src/main.rs").write_text('fn main() { println!("other branch"); }\n')
    assert build(worktree, home, env).returncode == 0
    assert (repo / "target").resolve() != (worktree / "target").resolve()
    assert (repo / "target/debug/shared-demo").resolve() == binary
    assert binary.stat().st_mtime_ns == before  # the other branch's build did not overwrite it
    assert subprocess.run([str(binary)], capture_output=True, text=True).stdout == ""


def test_guard_and_sweep_measure_usage_the_same_way(root):
    assert cleanup._usage(root) == pytest.approx(disk_guard.measure(root)[0], abs=0.5)


def test_each_cleanup_cycle_picks_up_a_pool_created_after_the_watcher_started(tmp_path, root, monkeypatch):
    from herdr_engineering.build_maintenance import cleanup_cycle
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "wstate"))
    cfg = {"roots": [str(root)], "high_water_percent": 100.0, "low_water_percent": 0}
    state = tmp_path / "wstate/build-gates"
    state.mkdir(parents=True)
    pool = state / "shared-targets"
    assert cleanup_cycle(cfg, None, state)["roots"] == [str(root)]
    pool.mkdir()
    assert os.path.realpath(pool) in cleanup_cycle(cfg, None, state)["roots"]


def test_pools_and_legacy_targets_on_an_enrolled_tier_are_swept_roots(tmp_path, monkeypatch):
    from herdr_engineering.build_maintenance import effective_roots
    volume = tmp_path / "vol/herdr-build"
    (volume / "shared-targets").mkdir(parents=True)
    (volume / "targets").mkdir()
    config = tmp_path / "tiers.yaml"
    config.write_text(yaml.safe_dump({"fabric_build_cache": {"local": {"storage_tiers": [
        {"name": "ext", "directory": str(volume / "compiler-cache")}]}}}))
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(config))
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "estate"))
    roots = {str(path) for path in effective_roots({"roots": []})}
    assert os.path.realpath(volume / "shared-targets") in roots
    assert os.path.realpath(volume / "targets") in roots


def make_managed_target(targets: Path, name: str = "app-1234", age_hours: float = 48) -> Path:
    """A per-worktree managed target (``targets/<label>-<id>``) with the markers that prove it is ours."""
    targets.mkdir(parents=True, exist_ok=True)
    (targets / ".herdr-target-root.json").write_text(json.dumps({"version": 1, "owner_uid": os.getuid()}))
    target = targets / name
    (target / "debug/.fingerprint").mkdir(parents=True)
    (target / "debug/artifact").write_bytes(b"x" * 4096)
    (target / ".herdr-cargo-target.json").write_text(json.dumps(
        {"version": 1, "workspace": str(targets.parent / "ws"), "owner_uid": os.getuid()}))
    stamp = time.time() - age_hours * 3600
    for path in [*target.rglob("*"), target]:
        os.utime(path, (stamp, stamp))
    return target


def test_orphaned_per_workspace_targets_are_reclaimed_when_idle(tmp_path):
    targets = tmp_path / "state/targets"
    target = make_managed_target(targets)
    recent = make_managed_target(targets, "fresh-5678", age_hours=0)
    result = cleanup.run_once([Path(os.path.realpath(targets))], tmp_path / "state2", high_water=1,
                              low_water=0, min_age_hours=24)
    assert str(Path(os.path.realpath(target))) in result["removed"] and not target.exists()
    assert recent.exists()


def test_unmarked_directories_under_a_targets_folder_are_not_removed(tmp_path):
    targets = tmp_path / "state/targets"
    target = make_managed_target(targets)
    (target / ".herdr-cargo-target.json").unlink()
    cleanup.run_once([Path(os.path.realpath(targets))], tmp_path / "state2", high_water=1,
                     low_water=0, min_age_hours=24)
    assert target.exists()


def test_tracked_cache_only_target_in_a_repository_is_never_removed(root):
    repo = root / "tools"
    fixture = repo / "tests/fixtures/ws/target"
    (fixture / "debug/.fingerprint").mkdir(parents=True)
    (fixture / "CACHEDIR.TAG").write_text(CACHEDIR_TAG)
    (fixture / ".rustc_info.json").write_text("{}")
    git("init", "-q", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "fixture", cwd=repo)
    stamp = time.time() - 48 * 3600
    for path in [*fixture.rglob("*"), fixture]:
        os.utime(path, (stamp, stamp))
    result = sweep(root)
    assert fixture.exists() and not result["removed"]
    assert any(item["reason"] == "tracked or unverifiable output" for item in result["skipped"])


def test_dry_run_scans_processes_once_not_per_candidate(root):
    for name in ("one", "two", "three"):
        make_target(root, name)
    result = sweep(root, dry_run=True)
    assert len(result["would_remove"]) == 3 and result["process_scans"] == 1


def test_explicit_roots_are_not_resolved_and_a_non_canonical_one_is_skipped_with_a_reason(tmp_path, root):
    from herdr_engineering.build_maintenance import effective_roots
    link = tmp_path / "link"
    link.symlink_to(root)
    assert effective_roots({}, [str(link)]) == [link]
    cache = make_target(root, "victim")
    result = cleanup.run_once([link, root], tmp_path / "state", high_water=1, low_water=0, min_age_hours=24)
    assert result["status"] == "warning" and not result["errors"]
    assert any(f"skipped root {link}" in warning and "canonical" in warning for warning in result["warnings"])
    assert str(cache) in result["removed"] and not cache.exists()  # the good root is still swept


def test_a_missing_default_root_does_not_abort_the_sweep(tmp_path, root):
    cache = make_target(root, "victim")
    missing = tmp_path / "gone" / "targets"
    result = cleanup.run_once([missing, root], tmp_path / "state", high_water=1, low_water=0, min_age_hours=24)
    assert not result["errors"] and any(f"skipped root {missing}" in w for w in result["warnings"])
    assert not cache.exists()


def lock_state(tmp_path: Path, name: str, mode: int) -> int:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    fd = os.open(state / name, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, mode)
    return fd


def test_a_sweep_runs_while_a_build_holds_the_maintenance_lock_shared(tmp_path, root):
    """The guard runs inside builds (build-coordinate holds LOCK_SH for the whole build)."""
    cache = make_target(root, "victim")
    fd = lock_state(tmp_path, "build-maintenance.lock", fcntl.LOCK_SH)
    try:
        result = cleanup.run_once([root], tmp_path / "state", high_water=1, low_water=0, min_age_hours=24)
    finally:
        os.close(fd)
    assert result["status"] != "busy" and not cache.exists()


def test_a_sweep_yields_to_exclusive_maintenance_and_to_another_sweep(tmp_path, root):
    cache = make_target(root, "victim")
    for name in ("build-maintenance.lock", "build-cleanup.lock"):
        fd = lock_state(tmp_path, name, fcntl.LOCK_EX)
        try:
            result = cleanup.run_once([root], tmp_path / "state", high_water=1, low_water=0, min_age_hours=24)
        finally:
            os.close(fd)
        assert result["status"] == "busy" and cache.exists(), name


def test_a_volume_tier_is_resolved_through_its_mount_never_against_the_cwd(tmp_path, monkeypatch):
    from herdr_engineering.build_maintenance import effective_roots
    decoy = tmp_path / "cwd"
    (decoy / "herdr-build/targets").mkdir(parents=True)
    monkeypatch.chdir(decoy)
    config = tmp_path / "tiers.yaml"
    config.write_text(yaml.safe_dump({"fabric_build_cache": {"local": {"storage_tiers": [
        {"name": "ext", "directory": "herdr-build/compiler-cache",
         "volume_uuid": "11111111-2222-3333-4444-555555555555"}]}}}))
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(config))
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "estate"))
    assert os.path.realpath(decoy / "herdr-build/targets") not in {str(p) for p in effective_roots({"roots": []})}


def test_a_legacy_shared_pool_is_still_swept_when_idle_and_migration_ignores_it(tmp_path):
    from herdr_engineering.build_cleanup import _discover
    pool = tmp_path / "state/build-gates/shared-targets"
    cache = make_pool_target(pool / "repo-1" / "tc-0")
    root = Path(os.path.realpath(pool))
    assert cache in _discover(root) and _discover(root, managed=False) == []
    assert cleanup.run_once([root], tmp_path / "s2", high_water=1, low_water=0,
                            min_age_hours=24, pressure_water=0)["removed_count"] == 1


def test_build_hygiene_install_exits_nonzero_when_a_step_fails(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump({}))
    env = {"HOME": str(home), "PATH": str(tmp_path / "empty"), "HERDR_ENGINEERING_CONFIG": str(config),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "s"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "build-hygiene", "install",
                             "--no-activate"], env=env, capture_output=True, text=True, timeout=120)
    assert json.loads(result.stdout)["cargo_host"]["error"]
    assert result.returncode != 0


def test_systemd_install_without_a_user_bus_writes_the_unit_and_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-runtime-dir"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    result = build_hygiene.install_cleanup_job(tmp_path, [sys.executable, "-m", "herdr_engineering.cli"],
                                               platform="linux", activate=True)
    assert result["activated"] is False and "user bus" in result["skipped"]
    assert Path(result["job"]).is_file()


def test_systemd_unit_lives_under_xdg_config_home_for_the_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert build_hygiene._unit_path(Path.home()) == tmp_path / "xdg/systemd/user" / build_hygiene.SYSTEMD_UNIT
    assert build_hygiene._unit_path(tmp_path / "other") == (
        tmp_path / "other/.config/systemd/user" / build_hygiene.SYSTEMD_UNIT)


def test_install_can_leave_the_cleanup_job_to_the_controller_step(tmp_path):
    result = build_hygiene.install(tmp_path, [sys.executable, "-m", "x"], platform="linux", activate=False,
                                   config={"fabric_build_cache": {"enabled": False}}, skip_cleanup_job=True)
    assert "installed by the controller" in result["build_cleanup"]["skipped"]
    assert not build_hygiene._unit_path(tmp_path).exists()


def test_fleet_runs_a_single_cleanup_installer_per_platform():
    from herdr_engineering import fleet
    assert "--skip-cleanup-job" in fleet._build_hygiene_command(legacy_installer_ran=True)
    assert "--skip-cleanup-job" not in fleet._build_hygiene_command(legacy_installer_ran=False)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd")
def test_launchd_reload_survives_the_asynchronous_bootout(tmp_path):
    import plistlib
    import uuid
    label = f"com.herdr-engineering.test-{uuid.uuid4().hex[:8]}"
    plist = tmp_path / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/true"],
                                      "RunAtLoad": False}))
    domain = f"gui/{os.getuid()}"
    if subprocess.run(["launchctl", "print", domain], capture_output=True).returncode != 0:
        pytest.skip("no launchd GUI domain")
    try:
        build_hygiene.reload_launchd_job(domain, label, plist)
        build_hygiene.reload_launchd_job(domain, label, plist)  # job is loaded: bootout races bootstrap
        assert subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                              capture_output=True).returncode == 0
    finally:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)


def test_build_coordinate_guards_by_command_and_leaves_cargo_to_the_wrapper(tmp_path):
    settings = {"build_cleanup": {"enabled": True, "roots": [], "refuse_builds_percent": 0,
                                  "refuse_builds_min_free_gib": 1e12}}
    assert run_cli(tmp_path, "build-coordinate", "--", "ninja", "--version",
                   settings=settings).returncode == disk_guard.EXIT_REFUSED
    for command in (["go", "test", "./..."], ["go", "vet"], ["cargo", "--version"], ["true"]):
        result = run_cli(tmp_path, "build-coordinate", "--", *command, settings=settings)
        assert result.returncode != disk_guard.EXIT_REFUSED, command
    assert run_cli(tmp_path, "build-coordinate", "--", "go", "build", settings=settings
                   ).returncode == disk_guard.EXIT_REFUSED


@pytest.mark.integration
def test_wrapper_still_guards_when_it_runs_under_build_coordinate(tmp_path):
    home, env = cargo_env(tmp_path, refuse=0)
    repo, _ = crate_with_worktree(tmp_path)
    result = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "build-coordinate", "--",
                             str(home / ".local/bin/cargo"), "build", "--offline"],
                            cwd=repo, env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == disk_guard.EXIT_REFUSED, result.stderr


# --- round 4: build-held target lock, guard ordering, busy wait, scan resilience ----------------

def test_a_target_a_build_holds_is_never_swept_and_is_swept_once_the_build_ends(tmp_path):
    from herdr_engineering.cargo_target_storage import prepare_target
    cache = tmp_path / "state/sccache"
    cache.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = prepare_target(cache, workspace, build_lock=True)  # a build's shared lock, still open
    (target / "debug/.fingerprint").mkdir(parents=True)
    (target / "debug/artifact").write_bytes(b"x" * 4096)
    stamp = time.time() - 48 * 3600
    for path in [*target.rglob("*"), target]:
        os.utime(path, (stamp, stamp))
    root = Path(os.path.realpath(target.parent))
    busy = cleanup.run_once([root], tmp_path / "s2", high_water=1, low_water=0, min_age_hours=24)
    assert busy["removed_count"] == 0 and target.exists()
    assert any(item["reason"] == "build in progress" for item in busy["skipped"])


def test_a_build_waits_for_a_sweep_that_is_deleting_its_target_then_starts_afresh(tmp_path):
    import threading

    from herdr_engineering.cargo_target_storage import prepare_target
    cache = tmp_path / "state/sccache"
    cache.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = prepare_target(cache, workspace)
    stamp = time.time() - 48 * 3600
    os.utime(target, (stamp, stamp))
    fd = cleanup._lock_for_removal(target)  # the sweep, midway through its delete
    assert isinstance(fd, int) and fd >= 0
    done = threading.Event()
    threading.Thread(target=lambda: (prepare_target(cache, workspace, build_lock=True), done.set()),
                     daemon=True).start()
    assert not done.wait(0.5), "the build must wait while the sweep holds the target"
    shutil.rmtree(target)
    cleanup._release_removal_lock(target, fd)
    assert done.wait(10), "the build starts as soon as the sweep is done"
    assert target.is_dir() and (target / ".herdr-cargo-target.json").is_file()


def test_an_unreadable_subdirectory_is_skipped_not_the_whole_root(root):
    blocked = root / "docker-data" / "pg"
    blocked.mkdir(parents=True)
    cache = make_target(root)
    blocked.chmod(0)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        result = sweep(root, pressure_water=0)
    finally:
        blocked.chmod(0o700)
    assert result["removed_count"] == 1 and not cache.exists()
    assert any("unreadable" in warning for warning in result["warnings"])


def test_guard_waits_for_a_sweep_already_freeing_space_before_deciding(root, state_env):
    import threading
    state_env.mkdir(parents=True)
    held = os.open(state_env / "build-cleanup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    threading.Timer(1.0, lambda: (fcntl.flock(held, fcntl.LOCK_UN), os.close(held))).start()
    started = time.monotonic()
    decision = disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={}, busy_wait=15)
    assert time.monotonic() - started >= 0.8  # it waited for the other sweep, then re-measured
    assert not decision.allowed


def test_guard_does_not_wait_when_the_busy_wait_is_zero(root, state_env):
    state_env.mkdir(parents=True)
    held = os.open(state_env / "build-cleanup.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        disk_guard.check_build_allowed(root, guard_policy(root, 0), state_env, env={}, busy_wait=0)
        assert time.monotonic() - started < 5
    finally:
        os.close(held)


@pytest.mark.integration
def test_guard_runs_before_the_reserve_check_so_pressure_cleanup_is_reached(tmp_path):
    home, env = cargo_env(tmp_path, refuse=0)
    settings = yaml.safe_load(Path(env["HERDR_ENGINEERING_CONFIG"]).read_text())
    settings["fabric_build_cache"]["local"]["min_free_disk_percent"] = 100  # no tier can ever meet it
    Path(env["HERDR_ENGINEERING_CONFIG"]).write_text(yaml.safe_dump(settings))
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert "refusing to start a new build" in refused.stderr


@pytest.mark.integration
def test_wrapper_guards_an_uncached_build_when_the_build_cache_is_disabled(tmp_path):
    home, env = cargo_env(tmp_path, refuse=0)
    settings = yaml.safe_load(Path(env["HERDR_ENGINEERING_CONFIG"]).read_text())
    settings["fabric_build_cache"] = {"enabled": False}
    Path(env["HERDR_ENGINEERING_CONFIG"]).write_text(yaml.safe_dump(settings))
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr


def test_target_override_names_the_directory_the_caller_chose(tmp_path):
    from herdr_engineering.cargo_target_storage import target_override
    cwd = tmp_path / "repo"
    (cwd / ".cargo").mkdir(parents=True)
    (cwd / ".cargo/config.toml").write_text('[build]\ntarget-dir = "out"\n')
    assert target_override([], {}, cwd) == tmp_path / "repo/out"
    assert target_override(["--target-dir", "/elsewhere/t"], {}, cwd) == Path("/elsewhere/t")
    assert target_override([], {"CARGO_TARGET_DIR": "/ext/t"}, cwd) == Path("/ext/t")
    assert target_override(["--config", "extra.toml"], {}, cwd) is True
    clean = tmp_path / "clean"
    clean.mkdir()
    assert target_override([], {"CARGO_HOME": str(clean)}, clean) is False


def test_a_job_pointing_into_another_venv_is_replaced_not_kept(tmp_path):
    stale = tmp_path / "old-venv"
    (stale / "bin").mkdir(parents=True)
    (stale / "pyvenv.cfg").write_text("home = /usr/bin\n")
    exe = stale / "bin/herdr-eng"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    plist = tmp_path / "job.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(exe), "build-cleanup", "run"]}))
    assert build_hygiene._job_works(plist) is False
    kept = tmp_path / "bin/herdr-eng"
    kept.parent.mkdir()
    kept.write_text("#!/bin/sh\n")
    kept.chmod(0o755)
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(kept), "build-cleanup", "run"]}))
    assert build_hygiene._job_works(plist) is True
