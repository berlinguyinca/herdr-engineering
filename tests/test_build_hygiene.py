"""Disk hygiene defaults: pressure cleanup, shared target pool, build guard, install and doctor.

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
from herdr_engineering.cargo_target_storage import acquire_shared_target, repo_identity, slot_name
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
    assert cache.local.target_mode == "shared" and cache.local.target_pool_size == 2


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


def test_pool_target_is_swept_when_idle_and_kept_while_its_slot_lock_is_held(root):
    cache = make_pool_target(root / "repo-1" / "tc-0")
    lock = cache.parent / ".herdr-slot.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        busy = sweep(root, pressure_water=0)
        assert busy["removed_count"] == 0 and cache.exists()
        assert any(item["reason"] == "build in progress" for item in busy["skipped"])
    finally:
        os.close(fd)
    assert sweep(root, pressure_water=0)["removed_count"] == 1
    assert not cache.exists()


def test_cache_only_directory_without_cachedir_tag_is_not_recognised(root):
    cache = make_pool_target(root / "repo-2" / "tc-0")
    (cache / "CACHEDIR.TAG").unlink()
    assert sweep(root, pressure_water=0)["removed_count"] == 0
    assert cache.exists()


# --- C. shared target pool -----------------------------------------------------------------

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_worktrees_of_one_repo_share_an_identity_and_other_repos_do_not(tmp_path):
    repo, other = tmp_path / "repo", tmp_path / "other"
    for path in (repo, other):
        path.mkdir()
        git("init", "-q", cwd=path)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature", cwd=repo)
    assert repo_identity(repo) == repo_identity(tmp_path / "wt")
    assert repo_identity(repo) != repo_identity(other)


def test_pool_reuses_slot_zero_then_spills_when_it_is_busy(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first, fd0 = acquire_shared_target(tmp_path / "pool", "repo", "tc", 2, workspace)
    second, fd1 = acquire_shared_target(tmp_path / "pool", "repo", "tc", 2, workspace)
    assert first.parent.name.endswith("-0") and second.parent.name.endswith("-1")
    os.close(fd0)
    os.close(fd1)
    again, fd2 = acquire_shared_target(tmp_path / "pool", "repo", "tc", 2, workspace)
    os.close(fd2)
    assert again == first


def test_pool_falls_back_to_a_stable_slot_when_all_are_busy(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    held = [acquire_shared_target(tmp_path / "pool", "repo", "tc", 2, workspace) for _ in range(2)]
    third, fd = acquire_shared_target(tmp_path / "pool", "repo", "tc", 2, workspace)
    assert third in {path for path, _ in held}
    for _, handle in held:
        os.close(handle)
    os.close(fd)


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


@pytest.mark.integration
def test_default_install_gives_each_worktree_an_incremental_free_pool_target(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, worktree = crate_with_worktree(tmp_path)
    for cwd in (repo, worktree):
        result = build(cwd, home, env)
        assert result.returncode == 0, result.stderr
    pool = tmp_path / "state/build-gates/shared-targets"
    slots = [p for p in pool.glob("*/*") if p.is_dir()]
    assert len(slots) == 2 and all(slot.name.endswith("-0") for slot in slots)
    for slot in slots:
        debug = slot / "target/debug"
        assert (debug / "shared-demo").is_file()
        # CARGO_INCREMENTAL=0 applied by the wrapper: no incremental artifacts (cargo may keep an empty dir)
        assert not any((debug / "incremental").rglob("*"))
    # ./target stays usable: a managed alias to the slot the last build there used
    for checkout in (repo, worktree):
        assert (checkout / "target").is_symlink()
        assert (checkout / "target/debug/shared-demo").is_file()
        assert (checkout / "target").resolve() in {(slot / "target").resolve() for slot in slots}


@pytest.mark.integration
def test_concurrent_build_spills_to_the_second_pool_slot(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, _ = crate_with_worktree(tmp_path)
    pool = tmp_path / "state/build-gates/shared-targets"
    from herdr_engineering.cargo_target_storage import toolchain_identity
    toolchain = subprocess.run([shutil.which("cargo"), "-vV"], cwd=repo, env=env, capture_output=True,
                               text=True, check=True).stdout
    _, held = acquire_shared_target(pool, repo_identity(repo), toolchain_identity(toolchain), 2, repo)
    try:
        result = build(repo, home, env)
    finally:
        os.close(held)
    assert result.returncode == 0, result.stderr
    assert (pool / repo_identity(repo) / slot_name(toolchain_identity(toolchain), repo, 1) / "target/debug/shared-demo").is_file()


@pytest.mark.integration
def test_explicit_cargo_target_dir_is_respected(tmp_path):
    explicit = tmp_path / "mine"
    home, env = cargo_env(tmp_path, {"CARGO_TARGET_DIR": str(explicit)})
    repo, _ = crate_with_worktree(tmp_path)
    assert build(repo, home, env).returncode == 0
    assert (explicit / "debug/shared-demo").is_file()
    assert not (tmp_path / "state/build-gates/shared-targets").exists()


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
def test_shared_alias_follows_the_slot_of_the_last_build(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, _ = crate_with_worktree(tmp_path)
    assert build(repo, home, env).returncode == 0
    pool = tmp_path / "state/build-gates/shared-targets"
    from herdr_engineering.cargo_target_storage import toolchain_identity
    toolchain = subprocess.run([shutil.which("cargo"), "-vV"], cwd=repo, env=env, capture_output=True,
                               text=True, check=True).stdout
    _, held = acquire_shared_target(pool, repo_identity(repo), toolchain_identity(toolchain), 2, repo)
    try:
        assert build(repo, home, env).returncode == 0
    finally:
        os.close(held)
    slot_one = pool / repo_identity(repo) / slot_name(toolchain_identity(toolchain), repo, 1) / "target"
    assert (repo / "target").resolve() == slot_one.resolve()


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
    assert not (tmp_path / "state/build-gates/shared-targets").exists()


@pytest.mark.integration
def test_cargo_clean_cleans_the_shared_slot_and_creates_no_per_workspace_target(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, worktree = crate_with_worktree(tmp_path)
    assert build(repo, home, env).returncode == 0
    slot = next(p for p in (tmp_path / "state/build-gates/shared-targets").glob("*/*") if p.is_dir())
    assert (slot / "target/debug/shared-demo").is_file()
    other = subprocess.run([str(home / ".local/bin/cargo"), "clean"], cwd=worktree, env=env,
                           capture_output=True, text=True, timeout=120)
    assert other.returncode == 0, other.stderr
    assert (slot / "target/debug/shared-demo").is_file()  # another worktree's clean never touches it
    cleaned = subprocess.run([str(home / ".local/bin/cargo"), "clean"], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=120)
    assert cleaned.returncode == 0, cleaned.stderr
    assert not (slot / "target/debug").exists()
    assert not (tmp_path / "state/build-gates/targets").exists()  # no per-workspace managed target
    assert not (worktree / "target").exists()  # clean created no alias for a worktree that never built


@pytest.mark.integration
def test_cargo_clean_skips_a_slot_a_build_holds(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, _ = crate_with_worktree(tmp_path)
    assert build(repo, home, env).returncode == 0
    pool = tmp_path / "state/build-gates/shared-targets"
    slot = next(p for p in pool.glob("*/*") if p.is_dir())
    fd = os.open(slot / ".herdr-slot.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        cleaned = subprocess.run([str(home / ".local/bin/cargo"), "clean"], cwd=repo, env=env,
                                 capture_output=True, text=True, timeout=120)
    finally:
        os.close(fd)
    assert cleaned.returncode != 0 and "busy" in cleaned.stderr
    assert (slot / "target/debug/shared-demo").is_file()


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

    fleet._install_controller_cleanup(installer, step, report, have_bus=True)  # must not raise
    assert report["steps"]["controller_cleanup"]["ok"] is False
    assert report["steps"]["controller_cleanup"]["rc"] == 3


def test_fleet_cleanup_installer_is_skipped_without_a_systemd_user_bus(tmp_path):
    from herdr_engineering import fleet
    installer = tmp_path / "build-cleanup.sh"
    ran = tmp_path / "ran"
    installer.write_text(f"#!/bin/sh\ntouch {ran}\n")
    report: dict = {"steps": {}}
    fleet._install_controller_cleanup(installer, lambda *a: (False, ""), report, have_bus=False)
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
    again = build_hygiene.install(working, fallback, platform="darwin", activate=False, config=config)
    assert "kept" in again["build_cleanup"] and job.read_bytes() == before


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
    fd = os.open(state_env / "build-maintenance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)  # another gated build is running
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

def test_each_worktree_owns_its_pool_slots_and_never_shares_artifacts(tmp_path):
    first_ws, second_ws = tmp_path / "a", tmp_path / "b"
    first_ws.mkdir()
    second_ws.mkdir()
    pool = tmp_path / "pool"
    first, fd = acquire_shared_target(pool, "repo", "tc", 2, first_ws)
    os.close(fd)
    second, fd = acquire_shared_target(pool, "repo", "tc", 2, second_ws)
    os.close(fd)
    again, fd = acquire_shared_target(pool, "repo", "tc", 2, first_ws)
    os.close(fd)
    assert first.parent != second.parent  # idle slot of another worktree is never reused
    assert again == first  # a worktree gets its own last slot back


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


def make_legacy_target(targets: Path, name: str = "app-1234", age_hours: float = 48) -> Path:
    """A per-workspace managed target as written before the shared pool became the default."""
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
    target = make_legacy_target(targets)
    recent = make_legacy_target(targets, "fresh-5678", age_hours=0)
    result = cleanup.run_once([Path(os.path.realpath(targets))], tmp_path / "state2", high_water=1,
                              low_water=0, min_age_hours=24)
    assert str(Path(os.path.realpath(target))) in result["removed"] and not target.exists()
    assert recent.exists()


def test_unmarked_directories_under_a_targets_folder_are_not_removed(tmp_path):
    targets = tmp_path / "state/targets"
    target = make_legacy_target(targets)
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


def test_explicit_roots_are_not_resolved_so_the_canonical_root_guard_applies(tmp_path, root):
    from herdr_engineering.build_maintenance import effective_roots
    link = tmp_path / "link"
    link.symlink_to(root)
    assert effective_roots({}, [str(link)]) == [link]
    result = cleanup.run_once([link], tmp_path / "state", high_water=1, low_water=0)
    assert result["status"] == "error" and "canonical" in result["errors"][0]


def test_toolchain_identity_is_cached_between_builds(tmp_path, monkeypatch):
    from herdr_engineering.cargo_target_storage import cached_toolchain_identity
    cargo = REAL_HOME / ".cargo/bin/cargo"
    if not cargo.is_file():
        pytest.skip("rustup's cargo is required")
    monkeypatch.setenv("RUSTUP_HOME", os.environ.get("RUSTUP_HOME", str(REAL_HOME / ".rustup")))
    monkeypatch.setenv("CARGO_HOME", str(REAL_HOME / ".cargo"))
    state = tmp_path / "state"
    state.mkdir()
    first = cached_toolchain_identity(Path(cargo), [], tmp_path, state)
    entries = list(state.glob("toolchain-*.json"))
    assert len(entries) == 1
    entries[0].write_text(json.dumps({"identity": "cached-marker"}))
    assert cached_toolchain_identity(Path(cargo), [], tmp_path, state) == "cached-marker"
    entries[0].unlink()
    assert cached_toolchain_identity(Path(cargo), [], tmp_path, state) == first


def test_target_mode_is_an_enum_and_rejects_unknown_values():
    from herdr_engineering.build_cache_config import TargetMode
    from herdr_engineering.errors import InvalidError
    section = load_config([])["fabric_build_cache"]
    assert validate(section).local.target_mode is TargetMode.SHARED
    section["local"]["target_mode"] = "per_workspace"
    assert validate(section).local.target_mode is TargetMode.PER_WORKSPACE
    section["local"]["target_mode"] = "bogus"
    with pytest.raises(InvalidError):
        validate(section)


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
