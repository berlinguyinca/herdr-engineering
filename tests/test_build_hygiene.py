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
from herdr_engineering import disk_guard, build_hygiene
from herdr_engineering.build_cache_config import validate
from herdr_engineering.cargo_target_storage import acquire_shared_target, repo_identity
from herdr_engineering.config import load_config
from herdr_engineering.doctor import DoctorReport, _add_build_hygiene

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
    assert first.parent.name == "tc-0" and second.parent.name == "tc-1"
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
            "refuse_builds_percent": refuse}


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
        settings["build_cleanup"] = {"refuse_builds_percent": refuse}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(settings))
    env = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
           "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
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
def test_default_install_shares_one_incremental_free_target_across_worktrees(tmp_path):
    home, env = cargo_env(tmp_path)
    repo, worktree = crate_with_worktree(tmp_path)
    for cwd in (repo, worktree):
        result = build(cwd, home, env)
        assert result.returncode == 0, result.stderr
    pool = tmp_path / "state/build-gates/shared-targets"
    slots = [p for p in pool.glob("*/*") if p.is_dir()]
    assert len(slots) == 1 and slots[0].name.endswith("-0")
    debug = slots[0] / "target/debug"
    assert (debug / "shared-demo").is_file()
    assert not (debug / "incremental").exists()  # CARGO_INCREMENTAL=0 applied by the wrapper
    assert not (repo / "target").exists() and not (worktree / "target").exists()


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
    assert (pool / repo_identity(repo) / f"{toolchain_identity(toolchain)}-1/target/debug/shared-demo").is_file()


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
    assert names["build_cleanup"].status == "ok" and names["cargo_host"].status == "ok"
    assert names["disk_pressure"].data["usage_percent"] >= 0
    json.dumps(after.to_dict())
