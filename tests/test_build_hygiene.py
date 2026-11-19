"""Disk hygiene defaults: pressure cleanup, one managed target per worktree, build guard, install and doctor.

Real filesystems, real git repositories, real processes and a real Cargo; no mocks. Pressure is
produced by thresholds against the real measured usage (0% is always exceeded, 100% never is).
"""
from __future__ import annotations

import fcntl
import inspect
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_hygiene, cargo_host, disk_guard
from herdr_engineering.build_cache_config import validate
from herdr_engineering.config import DEFAULTS, load_config
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
    usage = cleanup._usage(root)  # relative to what the filesystem shows, not an assumed 1%
    if usage <= 0:
        pytest.skip("the filesystem reports no usage at all")
    base = {"high_water": usage / 2, "low_water": 0, "min_age_hours": 24}
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


def test_systemd_unit_lives_under_xdg_config_home_for_the_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert build_hygiene._unit_path(Path.home()) == tmp_path / "xdg/systemd/user" / build_hygiene.SYSTEMD_UNIT
    assert build_hygiene._unit_path(tmp_path / "other") == (
        tmp_path / "other/.config/systemd/user" / build_hygiene.SYSTEMD_UNIT)


def test_fleet_update_leaves_the_cleanup_job_to_build_hygiene_alone():
    """One installer writes the job (the old systemd script resolved herdr-eng blindly, stale venv included)."""
    from herdr_engineering import fleet
    assert not hasattr(fleet, "_install_controller_cleanup")
    assert "--skip-cleanup-job" not in inspect.getsource(fleet) and "build-cleanup.sh" not in inspect.getsource(fleet)


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


def test_build_coordinate_guards_cargo_itself_when_the_wrapper_is_not_on_path(tmp_path):
    settings = {"build_cleanup": {"enabled": True, "roots": [], "refuse_builds_percent": 0,
                                  "refuse_builds_min_free_gib": 1e12}}
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname="c"\nversion="0.1.0"\nedition="2021"\n')
    (crate / "src/main.rs").write_text("fn main() {}\n")
    env = {**os.environ, "HERDR_ENGINEERING_CONFIG": str(tmp_path / "c.yaml"),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "st"), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(settings))
    env.pop(disk_guard.OVERRIDE_ENV, None)
    real = _real_cargo()
    env["PATH"] = f"{real.parent}{os.pathsep}/usr/bin{os.pathsep}/bin"
    refused = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "build-coordinate", "--",
                              "cargo", "build", "--offline"], cwd=crate, env=env, capture_output=True,
                             text=True, timeout=120)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert not (crate / "target").exists()


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
    settings["fabric_build_cache"]["local"]["min_free_disk_percent"] = 99  # no tier can ever meet it
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


def test_target_override_follows_cargos_precedence(tmp_path):
    from herdr_engineering.cargo_target_storage import target_override
    cwd = tmp_path / "repo"
    cwd.mkdir()
    env = {"CARGO_TARGET_DIR": "/from/env"}
    # --target-dir beats --config beats the environment
    assert target_override(["--target-dir", "/cli", "--config", "build.target-dir='/cfg'"], env, cwd) == Path("/cli")
    assert target_override(["--config", "build.target-dir='/cfg'"], env, cwd) == Path("/cfg")
    assert target_override(["--config=build.target-dir='/cfg'", "--target-dir=/cli"], env, cwd) == Path("/cli")
    assert target_override(["--config", "extra.toml"], env, cwd) is True
    # repeated --config values merge left to right: the last one that sets the directory wins
    assert target_override(["--config", "build.target-dir='/a'", "--config", "build.target-dir='/b'"],
                           env, cwd) == Path("/b")
    assert target_override([], env, cwd) == Path("/from/env")


def test_fleet_keeps_the_cleanup_job_when_roots_are_empty():
    from herdr_engineering import fleet
    # Empty roots still sweep the managed targets: fleet must not disable a job build-hygiene re-enables.
    assert fleet._cleanup_job_wanted({"enabled": True, "roots": []}) is True
    assert fleet._cleanup_job_wanted({}) is True
    assert fleet._cleanup_job_wanted({"enabled": False, "roots": ["/x"]}) is False
    assert not hasattr(fleet, "_has_systemd_user_bus")  # the one bus probe lives in build_hygiene


# --- review round 5 -------------------------------------------------------------------------

def make_managed(root: Path, name: str, age_hours: float = 48) -> Path:
    """A real managed target (``<root>/targets/<label>-<id>``) with Cargo output, idle for ``age_hours``."""
    from herdr_engineering.cargo_target_storage import prepare_target
    workspace = root.parent / "workspaces" / name
    workspace.mkdir(parents=True, exist_ok=True)
    (root / "sccache").mkdir(exist_ok=True)
    target = prepare_target(root / "sccache", workspace)
    (target / "debug/.fingerprint").mkdir(parents=True)
    (target / "debug/artifact").write_bytes(b"x" * 4096)
    stamp = time.time() - age_hours * 3600
    for path in [*target.rglob("*"), target]:
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    return target


def _age(target: Path, hours: float = 48) -> None:
    """Make the target look idle again (a build touches its marker when it starts)."""
    stamp = time.time() - hours * 3600
    for path in [*target.rglob("*"), target]:
        os.utime(path, (stamp, stamp), follow_symlinks=False)


def test_only_a_targets_directory_with_the_root_marker_yields_managed_targets(root):
    """A root marker proves ownership of the ``targets`` directory it sits in, nowhere else."""
    target = make_managed(root, "app")
    assert cleanup._discover(root) == [target]
    elsewhere = root / "somewhere-else"
    target.parent.rename(elsewhere)  # same marker, same child, but not a ``targets`` directory
    assert cleanup._discover(root) == []


def test_a_herdr_eng_symlink_into_another_venv_is_not_a_working_job(tmp_path):
    other = tmp_path / "old-venv"
    (other / "bin").mkdir(parents=True)
    (other / "pyvenv.cfg").write_text("home = /usr/bin\n")
    real = other / "bin/herdr-eng"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    link_dir = tmp_path / "local-bin"
    link_dir.mkdir()
    link = link_dir / "herdr-eng"
    link.symlink_to(real)
    assert build_hygiene._in_other_venv(link) is True


def set_reserve(env: dict, percent: float, **cleanup_settings) -> None:
    path = Path(env["HERDR_ENGINEERING_CONFIG"])
    settings = yaml.safe_load(path.read_text())
    local = settings["fabric_build_cache"]["local"]
    local["min_free_disk_percent"] = percent
    local["storage_tiers"] = [{"name": "primary", "directory": str(path.parent / "tier/compiler-cache")}]
    if cleanup_settings:
        settings.setdefault("build_cleanup", {}).update(cleanup_settings)
    path.write_text(yaml.safe_dump(settings))


@pytest.mark.integration
def test_a_reserve_refusal_cleans_up_then_exits_75_never_a_crash(tmp_path):
    home, env = cargo_env(tmp_path)  # the guard itself (90% / 50 GiB) would allow this build
    projects = tmp_path / "projects"
    projects.mkdir()
    stale = make_target(projects, age_hours=48)
    set_reserve(env, 99, roots=[str(projects)])  # no tier can ever keep 99% free
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert "refusing to start a new build" in refused.stderr and "Traceback" not in refused.stderr
    assert not stale.exists(), "the pressure cleanup runs before the refusal"


@pytest.mark.integration
def test_an_operator_owned_target_is_decided_before_and_without_the_managed_tier(tmp_path):
    home, env = cargo_env(tmp_path)
    set_reserve(env, 99)  # the managed tier could never be used, but this build never writes there
    repo, _ = crate_with_worktree(tmp_path)
    (repo / "target").mkdir()
    built = build(repo, home, env)
    assert built.returncode == 0, built.stderr
    assert (repo / "target/debug/shared-demo").is_file() and not managed_targets(tmp_path)


# --- review round 6 -------------------------------------------------------------------------

def _policy(root: Path, **extra) -> dict:
    return {"enabled": True, "roots": [str(root)], "pressure_percent": 100, **extra}


def test_the_reserve_check_and_the_sweep_use_the_one_df_figure(root):
    """select_storage's reserve rule and the cleanup's usage are the same measurement."""
    from herdr_engineering.build_cache_location import StorageTier, select_storage
    used, _ = disk_guard.measure(root)
    assert cleanup._usage(root) == pytest.approx(used, abs=0.5)
    tier = (StorageTier("t", str(root / "cache")),)
    keeps = select_storage(tier, root / "x", (100 - used) - 1)
    misses = select_storage(tier, root / "x", (100 - used) + 1)
    assert keeps.directory is not None and misses.directory is None


def test_a_reserve_cleanup_is_not_pressure_mode(root, state_env):
    """The reserve line (here 1%) must not make the short idle floor apply: only ``pressure_percent`` does."""
    recent = make_target(root, "recent", age_hours=3)
    old = make_target(root, "old", age_hours=48)
    ran = disk_guard.relieve(_policy(root), state_env, 1, pressure=False)
    assert ran is True and recent.exists() and not old.exists()
    shutil.rmtree(state_env)
    ran = disk_guard.relieve(_policy(root), state_env, 1, pressure=True)  # the guard's refusal line
    assert ran is True and not recent.exists()


def test_a_cleanup_only_touches_roots_on_the_filesystem_under_pressure(root, state_env):
    old = make_target(root, "old", age_hours=48)
    elsewhere = os.stat(root).st_dev + 1  # a different filesystem than the root's
    result = cleanup.run_once([root], state_env, high_water=1, low_water=0, only_device=elsewhere)
    assert result["roots"] == [] and old.exists()
    result = cleanup.run_once([root], state_env, high_water=1, low_water=0, only_device=os.stat(root).st_dev)
    assert result["removed"] == [str(old)]


def test_a_failed_cleanup_does_not_start_the_cooldown(tmp_path, root):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    assert disk_guard.relieve(_policy(root), blocker / "state", 1) is None  # the sweep itself cannot start
    good = tmp_path / "state"
    assert disk_guard.relieve(_policy(root), good, 1) is not None  # not suppressed by the failure


def test_a_reserve_miss_with_an_empty_build_cleanup_key_exits_75_not_a_crash(tmp_path):
    home, env = cargo_env(tmp_path)
    set_reserve(env, 99)
    path = Path(env["HERDR_ENGINEERING_CONFIG"])
    path.write_text(path.read_text() + "build_cleanup:\n")  # YAML null: no value
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert "Traceback" not in refused.stderr and "AttributeError" not in refused.stderr


@pytest.mark.integration
def test_a_reserve_refusal_with_cleanup_disabled_does_not_claim_a_cleanup(tmp_path):
    home, env = cargo_env(tmp_path)
    set_reserve(env, 99, enabled=False)
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert "refusing to start a new build" in refused.stderr and "after cleaning up" not in refused.stderr


def _other_filesystem_dir() -> Path | None:
    """A writable directory on a different filesystem than the temp directory (tmpfs on Linux)."""
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK) and os.stat(shm).st_dev != os.stat(Path.cwd()).st_dev:
        return shm
    return None


@pytest.mark.integration
def test_the_guard_measures_the_tier_the_reserve_chose_not_a_full_primary(tmp_path):
    """Tiers [full, roomy]: the build goes to the roomy tier, so a full primary must not refuse it."""
    other = _other_filesystem_dir()
    if other is None:
        pytest.skip("needs a second filesystem (tmpfs at /dev/shm)")
    home, env = cargo_env(tmp_path)
    base_used, _ = disk_guard.measure(tmp_path)
    shm_used, _ = disk_guard.measure(other)
    if shm_used >= base_used - 2:
        pytest.skip("the temp filesystem is not fuller than tmpfs here")
    path = Path(env["HERDR_ENGINEERING_CONFIG"])
    settings = yaml.safe_load(path.read_text())
    local = settings["fabric_build_cache"]["local"]
    shm_dir = Path(tempfile.mkdtemp(dir=other)) / "compiler-cache"
    local["storage_tiers"] = [{"name": "primary", "directory": str(tmp_path / "tier/compiler-cache")},
                              {"name": "roomy", "directory": str(shm_dir)}]
    local["min_free_disk_percent"] = (100 - base_used + 100 - shm_used) / 2  # the primary misses it, tmpfs meets it
    # a guard that refuses anything on the primary's filesystem but nothing on tmpfs
    settings["build_cleanup"] = {"refuse_builds_percent": (base_used + shm_used) / 2,
                                 "refuse_builds_min_free_gib": 1e12, "enabled": False}
    path.write_text(yaml.safe_dump(settings))
    repo, _ = crate_with_worktree(tmp_path)
    try:
        built = build(repo, home, env)
    finally:
        shutil.rmtree(shm_dir.parent, ignore_errors=True)
    assert built.returncode == 0, built.stderr


# --- review round 7: Cargo's own locks, tombstones, one guard story ----------------------------

def _crate_with_slow_build(path: Path, seconds: int = 12) -> Path:
    """A real crate whose build script sleeps, so a real ``cargo build`` stays in progress."""
    (path / "src").mkdir(parents=True)
    (path / "Cargo.toml").write_text('[package]\nname = "slow"\nversion = "0.1.0"\nedition = "2021"\n'
                                     'build = "build.rs"\n')
    (path / "build.rs").write_text(f"fn main() {{ std::thread::sleep(std::time::Duration::from_secs({seconds})); }}\n")
    (path / "src/main.rs").write_text("fn main() {}\n")
    git("init", "-q", cwd=path)
    return path


def _wait_until_cargo_holds(target: Path, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if target.is_dir():
            held = cleanup._lock_for_removal(target)
            if held == cleanup._BUSY:
                return
            cleanup._release_removal_lock(held)
        time.sleep(0.2)
    raise AssertionError(f"cargo never took its lock under {target}")


def _stop(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _spawn_cargo(argv: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _real_cargo() -> Path:
    """A cargo that is not the herdr wrapper (an IDE or a script would run this one)."""
    for candidate in (REAL_HOME / ".cargo/bin/cargo", "/opt/homebrew/bin/cargo", "/usr/local/bin/cargo",
                      shutil.which("cargo") or ""):
        path = Path(candidate) if candidate else None
        if path and path.is_file() and not cargo_host.path_cargo_is_wrapper(str(path)):
            return path
    pytest.skip("a real, non-wrapper cargo is required")


def _direct_env(**extra: str) -> dict:
    cargo = _real_cargo()
    env = {**os.environ, **extra}
    env["PATH"] = f"{cargo.parent}{os.pathsep}{env['PATH']}"
    env["CARGO_HOME"] = os.environ.get("CARGO_HOME", str(REAL_HOME / ".cargo"))
    env.setdefault("RUSTUP_HOME", os.environ.get("RUSTUP_HOME", str(REAL_HOME / ".rustup")))
    for key in ("RUSTC_WRAPPER", "CARGO_TARGET_DIR", "HERDR_CARGO_HOST_COORDINATED"):
        if key not in extra:
            env.pop(key, None)
    return env


@pytest.mark.integration
def test_a_direct_cargo_build_into_a_managed_target_blocks_the_sweep(root):
    """An IDE-style ``cargo build`` (no wrapper, no lock of ours) is protected by Cargo's own lock."""
    target = make_managed(root, "ide")
    crate = _crate_with_slow_build(root.parent / "workspaces/ide-crate")
    build = _spawn_cargo([str(_real_cargo()), "build", "--offline"], crate,
                         _direct_env(CARGO_TARGET_DIR=str(target)))
    try:
        _wait_until_cargo_holds(target)
        _age(target)
        busy = sweep(root)
        assert busy["removed"] == [] and target.is_dir(), busy
        assert any(x["path"] == str(target) and x["reason"] in {"build in progress", "active project"} for x in busy["skipped"]), busy
        assert cleanup._lock_for_removal(target) == cleanup._BUSY
    finally:
        _stop(build)
    _age(target)
    assert sweep(root)["removed"] == [str(target)]  # the build is gone: swept (and its locks with it)


@pytest.mark.integration
def test_a_direct_cargo_build_into_an_operators_target_blocks_the_sweep(root):
    crate = _crate_with_slow_build(root / "proj")
    build = _spawn_cargo([str(_real_cargo()), "build", "--offline"], crate, _direct_env())
    try:
        _wait_until_cargo_holds(crate / "target")
        _age(crate / "target")
        busy = sweep(root)
        assert busy["removed"] == [] and (crate / "target").is_dir(), busy
        assert any(x["reason"] in {"build in progress", "active project"} for x in busy["skipped"]), busy
        assert cleanup._lock_for_removal(crate / "target") == cleanup._BUSY
    finally:
        _stop(build)


@pytest.mark.integration
def test_a_wrapper_build_blocks_the_sweep_and_dies_with_its_pid(tmp_path):
    """The wrapper execs cargo (no waiting parent): killing the pid it was started as ends the build and
    its lock, and until then the sweep leaves the target alone."""
    home, env = cargo_env(tmp_path)
    env["HERDR_CARGO_HOST_COORDINATED"] = "1"  # as build-coordinate starts it: the exec path
    crate = _crate_with_slow_build(tmp_path / "crate")
    build = _spawn_cargo([str(home / ".local/bin/cargo"), "build", "--offline"], crate, env)
    try:
        deadline = time.monotonic() + 90
        while not managed_targets(tmp_path) and time.monotonic() < deadline:
            time.sleep(0.2)
        (target,) = managed_targets(tmp_path)
        _wait_until_cargo_holds(target)
        root = Path(os.path.realpath(target.parent))
        _age(target)
        busy = cleanup.run_once([root], tmp_path / "s2", high_water=1, low_water=0, min_age_hours=24)
        assert busy["removed"] == [] and target.is_dir()
        os.kill(build.pid, 9)  # not the process group: only the pid a watcher would have
        build.wait(timeout=30)
        free = cleanup._lock_for_removal(target)
        assert free != cleanup._BUSY, "cargo must not outlive the pid the wrapper was started as"
        cleanup._release_removal_lock(free)
    finally:
        _stop(build)


def _hold(paths: list[Path], seconds: int = 60) -> subprocess.Popen:
    code = ("import fcntl, sys, time, os\n"
            "fds = []\n"
            "for p in sys.stdin.read().split():\n"
            "    os.makedirs(os.path.dirname(p), exist_ok=True)\n"
            "    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(fd, fcntl.LOCK_EX); fds.append(fd)\n"
            f"print('held', flush=True); time.sleep({seconds})\n")
    # paths travel on stdin: a target named on the command line would make the sweep see an active project
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    proc.stdin.write("\n".join(map(str, paths)) + "\n")
    proc.stdin.close()
    assert proc.stdout.readline().strip() == "held"
    return proc


@pytest.mark.parametrize("layout", [
    [("debug", ".cargo-lock")],                                          # cargo 1.75
    [("debug", ".cargo-build-lock")],                                    # newer cargo: the build lock alone
    [("release", ".cargo-artifact-lock")],
    [("aarch64-apple-darwin/debug", ".cargo-lock")],                     # --target <triple>
    [("debug", ".cargo-lock"), ("debug", ".cargo-build-lock"), ("debug", ".cargo-artifact-lock")],
])
def test_any_cargo_lock_layout_keeps_the_sweep_away(root, layout):
    managed = make_managed(root, "layout")
    unmanaged = make_target(root, "plain")
    targets = (managed, unmanaged)
    holders = [_hold([target / profile / name for profile, name in layout]) for target in targets]
    try:
        for target in targets:
            _age(target)
        result = sweep(root)
        for target in targets:
            assert target.is_dir() and str(target) not in result["removed"], (layout, result)
            # the open descriptor may already show as an active project; the lock itself must also say busy
            assert any(x["path"] == str(target) and x["reason"] in {"build in progress", "active project"}
                       for x in result["skipped"]), json.dumps(result, default=str)
            assert cleanup._lock_for_removal(target) == cleanup._BUSY, layout
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()
    for target in targets:
        _age(target)
    assert sorted(sweep(root)["removed"]) == sorted(map(str, targets))  # the holders are gone


def test_a_dry_run_lists_only_targets_no_build_holds_and_takes_no_lock_for_good(root):
    held, free = make_managed(root, "held"), make_managed(root, "free")
    holder = _hold([held / "debug/.cargo-lock"])
    try:
        result = sweep(root, dry_run=True)
    finally:
        holder.kill()
        holder.wait()
    assert result["would_remove"] == [str(free)]
    again = cleanup._lock_for_removal(free)  # the dry run released what it took
    assert isinstance(again, list)
    cleanup._release_removal_lock(again)


def test_an_unopenable_cargo_lock_skips_that_target_not_the_sweep(root):
    broken, healthy = make_managed(root, "a-broken"), make_managed(root, "b-healthy")
    lock = broken / "debug/.cargo-lock"
    lock.write_text("")
    lock.chmod(0)  # present but unreadable: this target cannot be proven idle
    _age(broken)
    if os.access(lock, os.R_OK):
        pytest.skip("permissions are not enforced for this user")
    result = sweep(root)
    assert result["removed"] == [str(healthy)] and result["errors"] == []
    assert any(x["path"] == str(broken) and x["reason"].startswith("cannot lock") for x in result["skipped"])
    assert broken.is_dir()


def _unwritable_child(target: Path) -> Path:
    inner = target / "debug/deps/locked"
    inner.mkdir(parents=True)
    (inner / "x").write_text("x")
    inner.chmod(0o500)  # rmtree cannot empty it
    return inner


def test_an_interrupted_delete_keeps_the_target_directory_and_leaves_a_tombstone(root):
    from herdr_engineering.cargo_target_storage import TOMBSTONE, prepare_target
    target = make_managed(root, "half")
    inner = _unwritable_child(target)
    if os.access(inner, os.W_OK):
        pytest.skip("permissions are not enforced for this user")
    _age(target)
    try:
        with pytest.raises(OSError):
            cleanup._remove_target(target)
        assert os.listdir(target) == [".herdr-cargo-target.json"], "the directory and its marker stay"
        graves = [p for p in target.parent.iterdir() if TOMBSTONE.fullmatch(p.name)]
        assert len(graves) == 1
        assert prepare_target(root / "sccache", root.parent / "workspaces/half") == target  # no ownership error
    finally:
        for grave in target.parent.iterdir():
            for path in [grave, *grave.rglob("*")] if grave.is_dir() else []:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
    result = sweep(root)  # collects the tombstone
    assert result["tombstones_removed"] == 1
    assert not [p for p in target.parent.iterdir() if TOMBSTONE.fullmatch(p.name)]


def test_a_swept_managed_target_is_not_swept_again_and_its_alias_never_dangles(root):
    target = make_managed(root, "alias")
    alias = root.parent / "workspaces/alias/target"
    alias.symlink_to(target)
    assert sweep(root)["removed"] == [str(target)]
    assert alias.is_dir() and os.listdir(alias) == [".herdr-cargo-target.json"]
    again = sweep(root)
    assert again["removed"] == [] and again["tombstones_removed"] == 0 and not again["errors"]


@pytest.mark.integration
def test_a_build_blocked_on_the_sweeps_locks_resumes_into_a_valid_target(root):
    """Cargo waits on the locks the sweep holds; the sweep empties the target; cargo then builds into the
    same, still owned directory (it used to recreate ``debug`` with no marker)."""
    target = make_managed(root, "resume")
    crate = _crate_with_slow_build(root.parent / "workspaces/resume-crate", seconds=1)
    for name in cleanup._CARGO_LOCK_NAMES:  # the locks cargo will wait on (it opens them if present)
        (target / "debug" / name).write_text("")
    _age(target)
    held = cleanup._lock_for_removal(target)
    assert isinstance(held, list) and held
    inodes = {name: (target / "debug" / name).stat().st_ino for name in cleanup._CARGO_LOCK_NAMES}
    log = root.parent / "second-build.log"
    with log.open("w") as sink:
        build = subprocess.Popen([str(_real_cargo()), "build", "--offline"], cwd=crate,
                                 env=_direct_env(CARGO_TARGET_DIR=str(target)), stdout=sink, stderr=sink,
                                 start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while "waiting for file lock" not in log.read_text() and time.monotonic() < deadline:
            assert build.poll() is None, log.read_text()
            time.sleep(0.2)
        cleanup._remove_target(target, held)
        assert {name: (target / "debug" / name).stat().st_ino for name in inodes} == inodes  # never moved
        assert build.wait(timeout=120) == 0, log.read_text()
    finally:
        _stop(build)
    assert (target / ".herdr-cargo-target.json").is_file() and (target / "debug/slow").is_file()
    from herdr_engineering.cargo_target_storage import prepare_target
    assert prepare_target(root / "sccache", root.parent / "workspaces/resume") == target


@pytest.mark.integration
def test_a_plain_cargo_builds_through_the_alias_after_a_sweep(root):
    target = make_managed(root, "plain")
    crate = _crate_with_slow_build(root.parent / "workspaces/plain", seconds=0)
    (crate / "target").symlink_to(target)
    _age(target)
    assert sweep(root)["removed"] == [str(target)]
    built = subprocess.run([str(_real_cargo()), "build", "--offline"], cwd=crate,
                           env=_direct_env(), capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    assert (target / "debug/slow").is_file() and (target / ".herdr-cargo-target.json").is_file()


def test_retention_collects_tombstones_too(tmp_path):
    from herdr_engineering import cargo_target_retention as retention
    from herdr_engineering.cargo_target_storage import prepare_target, tombstone_path
    cache = tmp_path / "state/sccache"
    cache.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = prepare_target(cache, workspace)
    grave = tombstone_path(target)
    target.rename(grave)
    (grave / "leftover").write_text("x")
    report = retention.sweep(cache, tmp_path / "ret-state", budget_bytes=1, apply=True)
    assert not grave.exists(), report


def test_a_failed_sweep_backs_off_briefly_and_says_why(root, state_env):
    """A sweep that failed as a whole (here: its own state sits inside a target) stamps a short cooldown with
    the reason, so a permanent cause is not retried by every build start; it retries after two minutes."""
    cache = make_target(root, age_hours=48)
    state = cache / "state"
    assert disk_guard.relieve(_policy(root), state, 1) is False
    stamp = json.loads((state / "disk-guard.json").read_text())
    assert "Maintenance state is inside cleanup target" in stamp["aborted"]
    assert disk_guard.last_abort(state) == stamp["aborted"]
    assert disk_guard.relieve(_policy(root), state, 1) is None  # backing off
    stamp["triggered_at"] -= 130  # past the two minutes, well inside the ten of a normal sweep
    (state / "disk-guard.json").write_text(json.dumps(stamp))
    assert disk_guard.relieve(_policy(root), state, 1) is False  # tried again
    assert cache.exists()


def test_a_refusal_names_why_the_last_cleanup_could_not_run(root, monkeypatch):
    cache = make_target(root, age_hours=48)
    state = cache / "state"
    monkeypatch.setattr(disk_guard, "measure", lambda path: (99.0, 1.0))
    monkeypatch.setattr(cleanup, "_usage", lambda path: 99.0)  # a tmpfs root can really sit at 0%
    decision = disk_guard.check_build_allowed(root, _policy(root, high_water=1), state, env={})
    assert not decision.allowed and "could not run: Maintenance state is inside cleanup target" in decision.message


def test_a_target_that_always_errors_does_not_disable_the_cooldown(root, state_env):
    """A per-target fault (a symlink inside one target) is reported and skipped: the sweep still counts as
    completed, so the guard does not run a full sweep at every build start."""
    bad = make_target(root, "bad")
    (bad / "debug/link").symlink_to(bad / "debug/artifact")
    _age(bad)
    good = make_managed(root, "good")
    assert disk_guard.relieve(_policy(root), state_env, 1) is True  # the good target went
    assert not good.exists() or os.listdir(good) == [".herdr-cargo-target.json"]
    assert (state_env / "disk-guard.json").exists()
    assert disk_guard.relieve(_policy(root), state_env, 1) is None  # inside the cooldown


def test_a_boolean_build_cleanup_keeps_what_an_earlier_source_set(tmp_path):
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    first.write_text("build_cleanup:\n  roots: [/x]\n  min_age_hours: 72\n")
    second.write_text("build_cleanup: false\n")
    section = load_config([first, second])["build_cleanup"]
    assert section["enabled"] is False and section["roots"] == ["/x"] and section["min_age_hours"] == 72


def test_build_cleanup_false_means_disabled_everywhere(tmp_path):
    from herdr_engineering import fleet
    from herdr_engineering.config import InvalidError
    for text, enabled in (("build_cleanup: false\n", False), ("build_cleanup: true\n", True)):
        path = tmp_path / "c.yaml"
        path.write_text(text)
        section = load_config([path])["build_cleanup"]
        assert isinstance(section, dict) and section["enabled"] is enabled
        assert section["roots"] == DEFAULTS["build_cleanup"]["roots"]
        assert fleet._cleanup_job_wanted(section) is enabled
    path.write_text("build_cleanup: [1]\n")
    with pytest.raises(InvalidError):
        load_config([path])


@pytest.mark.integration
def test_a_boolean_build_cleanup_never_crashes_the_wrapper(tmp_path):
    home, env = cargo_env(tmp_path)
    set_reserve(env, 99)
    path = Path(env["HERDR_ENGINEERING_CONFIG"])
    settings = yaml.safe_load(path.read_text())
    settings["build_cleanup"] = False
    path.write_text(yaml.safe_dump(settings))
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert "Traceback" not in refused.stderr and "after cleaning up" not in refused.stderr


def test_an_explicit_root_is_swept_even_when_the_background_job_is_disabled(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    stale = make_target(Path(os.path.realpath(projects)), age_hours=48)
    settings = {"build_cleanup": {"enabled": False, "high_water_percent": 1, "low_water_percent": 0}}
    plain = run_cli(tmp_path, "build-cleanup", "run", settings=settings)
    assert json.loads(plain.stdout)["status"] == "disabled", plain.stdout + plain.stderr  # the job stays off
    swept = run_cli(tmp_path, "build-cleanup", "run", "--root", str(projects), settings=settings)
    assert swept.returncode == 0, swept.stderr
    assert json.loads(swept.stdout)["removed"] == [str(stale)] and not stale.exists()


@pytest.mark.integration
def test_a_refusal_inside_the_cooldown_does_not_claim_a_cleanup(tmp_path):
    home, env = cargo_env(tmp_path)
    projects = tmp_path / "projects"
    projects.mkdir()
    stale = make_target(projects, age_hours=48)
    set_reserve(env, 99, roots=[str(projects)])
    stamp_dir = tmp_path / "state/build-gates"
    stamp_dir.mkdir(parents=True)
    (stamp_dir / "disk-guard.json").write_text(json.dumps({"triggered_at": time.time()}))  # swept moments ago
    repo, _ = crate_with_worktree(tmp_path)
    refused = build(repo, home, env)
    assert refused.returncode == disk_guard.EXIT_REFUSED, refused.stderr
    assert "after cleaning up" not in refused.stderr and stale.exists()


def test_the_reserve_refusal_does_not_claim_a_cleanup_that_removed_nothing(root, state_env):
    make_target(root, "fresh", age_hours=1)  # too young to remove
    ran = disk_guard.relieve(_policy(root), state_env, 1)
    assert ran is False  # a sweep ran, nothing was removed: nothing to claim


def test_a_process_snapshot_is_reused_for_a_few_seconds(root, monkeypatch):
    make_target(root, "a")
    make_target(root, "b")
    assert sweep(root)["process_scans"] == 1  # the second removal reuses the first snapshot
    make_target(root, "c")
    make_target(root, "d")
    monkeypatch.setattr(cleanup, "_SNAPSHOT_REUSE_SECONDS", 0.0)
    assert sweep(root)["process_scans"] == 3  # stale snapshots are taken again before each removal


@pytest.mark.integration
def test_a_second_cargo_serializes_behind_the_one_that_waited_on_the_sweep(root):
    """The sweep never moves a lock file, so the cargo that waited holds the lock on the real path and a
    cargo that starts after it must wait for it (two cargos in one target would corrupt it)."""
    target = make_managed(root, "serial")
    crate = _crate_with_slow_build(root.parent / "workspaces/serial-crate", seconds=5)
    for name in cleanup._CARGO_LOCK_NAMES:
        (target / "debug" / name).write_text("")
    _age(target)
    held = cleanup._lock_for_removal(target)
    assert isinstance(held, list) and held
    logs = [root.parent / "first.log", root.parent / "second.log"]
    procs = []

    def start(log):
        with log.open("w") as sink:
            procs.append(subprocess.Popen([str(_real_cargo()), "build", "--offline"], cwd=crate,
                                          env=_direct_env(CARGO_TARGET_DIR=str(target)), stdout=sink,
                                          stderr=sink, start_new_session=True))

    def wait_for(log, text, proc):
        deadline = time.monotonic() + 90
        while text not in log.read_text() and time.monotonic() < deadline:
            assert proc.poll() is None, log.read_text()
            time.sleep(0.1)
        assert text in log.read_text(), log.read_text()

    try:
        start(logs[0])
        wait_for(logs[0], "waiting for file lock", procs[0])
        cleanup._remove_target(target, held)
        wait_for(logs[0], "Compiling slow", procs[0])  # the first cargo owns the lock and is building
        start(logs[1])
        wait_for(logs[1], "waiting for file lock", procs[1])
        assert procs[0].poll() is None  # still building: the second one had to wait
        assert procs[0].wait(timeout=120) == 0 and procs[1].wait(timeout=120) == 0
    finally:
        for proc in procs:
            _stop(proc)


def test_a_slow_process_scan_is_reused_as_long_as_it_took():
    assert cleanup._reuse_window(0.05) == cleanup._SNAPSHOT_REUSE_SECONDS
    assert cleanup._reuse_window(8.0) == 8.0  # an lsof that slow must not run again for every candidate


def test_a_tombstone_is_claimed_atomically_and_a_live_claim_is_respected(root):
    from herdr_engineering.cargo_target_storage import TOMBSTONE, claim_tombstone
    grave = root / ".w.deleting-0123456789ab"
    grave.mkdir()
    mine = claim_tombstone(grave)
    assert mine is not None and mine.name == f".w.deleting-0123456789ab.claimed-{os.getpid()}" and mine.is_dir()
    assert TOMBSTONE.fullmatch(mine.name) and claim_tombstone(grave) is None  # gone from its old name
    assert claim_tombstone(mine) == mine
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    other = root / f".x.deleting-0123456789ab.claimed-{sleeper.pid}"
    other.mkdir()
    try:
        assert claim_tombstone(other) is None and other.exists()  # its deleter is alive
    finally:
        sleeper.kill()
        sleeper.wait()
    taken = claim_tombstone(other)  # its deleter is dead now
    assert taken is not None and taken.name.endswith(f".claimed-{os.getpid()}") and not other.exists()


def test_a_delete_stops_at_its_time_bound_and_continues_later(root, tmp_path):
    from herdr_engineering.cargo_target_storage import remove_tree
    tree = root / "big"
    for index in range(20):
        (tree / f"d{index}").mkdir(parents=True)
        (tree / f"d{index}/f").write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (tree / "d0/link").symlink_to(outside)
    assert remove_tree(tree, time.monotonic() - 1) is False and tree.exists()
    assert remove_tree(tree) is True and not tree.exists()
    assert outside.read_text() == "keep"  # a symlink is removed, never followed


def test_retention_leaves_a_tombstone_that_a_live_sweep_has_claimed(root):
    from herdr_engineering.cargo_target_retention import TierReport, _collect_tombstone
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    grave = root / f".w.deleting-0123456789ab.claimed-{sleeper.pid}"
    grave.mkdir()
    (grave / "f").write_text("x")
    try:
        report = TierReport(root=str(root), budget_bytes=1)
        assert _collect_tombstone(grave, report, time.monotonic() + 60) is True
        assert (grave / "f").exists() and not report.skipped  # not deleted under its owner, no warning
    finally:
        sleeper.kill()
        sleeper.wait()
