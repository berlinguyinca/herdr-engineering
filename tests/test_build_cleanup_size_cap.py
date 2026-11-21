"""Per-target size cap: an idle Cargo target over ``max_target_gb`` is cleaned on every sweep, whatever the
disk pressure, through the same safety path as the pressure sweep. Real cargo builds, real files, real
processes and a real registry; thresholds go through the real policy parameters."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_maintenance as maintenance
from herdr_engineering import build_registry as br
from tests.real_cargo import build

NO_PRESSURE = {"high_water": 100, "low_water": 0.0}  # df can never read 100%: only the size cap acts
SMALL = 1  # byte: every real build is over it
HUGE = 1 << 50


def project(root: Path, name="project", age=48) -> Path:
    p = root / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (p / "src").mkdir()
    (p / "src/main.rs").write_text("fn main() {}\n")
    target = build(p, p / "target")
    stamp = time.time() - age * 3600
    for path in [*target.rglob("*"), target]:
        os.utime(path, (stamp, stamp))
    return target


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return root, tmp_path / "state"


def sweep(root, state, **kw):
    return cleanup.run_once([root], state, **{**NO_PRESSURE, **kw})


def test_idle_target_over_the_cap_is_cleaned_below_high_water(env):
    root, state = env
    target = project(root)
    result = sweep(root, state, max_target_bytes=SMALL)
    assert not target.exists() or not any(target.iterdir())
    assert result["removed"] == [str(target)]
    assert result["reasons"][str(target)] == "size_cap"
    assert result["pressure"] is False
    assert (target.parent / "Cargo.toml").exists()


def test_target_under_the_cap_stays(env):
    root, state = env
    target = project(root)
    result = sweep(root, state, max_target_bytes=HUGE)
    assert target.exists() and result["removed_count"] == 0


def test_no_cap_and_no_pressure_does_nothing(env):
    root, state = env
    target = project(root)
    assert sweep(root, state)["removed_count"] == 0
    assert target.exists()


def test_only_the_oversized_target_goes(env):
    root, state = env
    big = project(root, "big")
    small = project(root, "small")
    (big / "debug/filler.bin").write_bytes(b"x" * 4_000_000)
    for path in (big / "debug/filler.bin", big / "debug"):
        os.utime(path, (time.time() - 48 * 3600,) * 2)
    cap = cleanup._inspect(big)[1] - 1_000_000
    assert cleanup._inspect(small)[1] < cap
    result = sweep(root, state, max_target_bytes=cap)
    assert result["removed"] == [str(big)]
    assert (small / "debug").exists()


def test_recent_target_over_the_cap_waits_for_the_idle_floor(env):
    root, state = env
    target = project(root, age=1)
    result = sweep(root, state, max_target_bytes=SMALL)
    assert result["removed_count"] == 0 and (target / "debug").exists()
    assert {"path": str(target), "reason": "recent activity"} in result["skipped"]


def test_dry_run_reports_and_keeps(env):
    root, state = env
    target = project(root)
    result = sweep(root, state, max_target_bytes=SMALL, dry_run=True)
    assert result["would_remove"] == [str(target)] and (target / "debug").exists()


def test_a_process_in_the_workspace_keeps_an_oversized_target(env):
    root, state = env
    target = project(root)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=target.parent)
    try:
        result = sweep(root, state, max_target_bytes=SMALL)
    finally:
        holder.kill()
        holder.wait()
    assert result["removed_count"] == 0 and (target / "debug").exists()
    assert result["blocked"] and result["blocked"][0]["path"] == str(target)


def test_an_active_lease_keeps_an_oversized_target(env):
    root, state = env
    target = project(root)
    reg = br.Registry(state)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        attempt = reg.register(repo_id="r", input_digest=None, cmd_kind="cargo build", pid=owner.pid)
        reg.transition(attempt.attempt_id, "classified", "t")
        reg.transition(attempt.attempt_id, "admitted", "t")
        reg.acquire_lease(str(target.parent), attempt.attempt_id, 300)
        result = sweep(root, state, max_target_bytes=SMALL)
    finally:
        owner.kill()
        owner.wait()
    assert result["removed_count"] == 0 and (target / "debug").exists()
    assert {"path": str(target), "reason": "active_lease"} in result["skipped"]


def test_only_targets_limits_the_pass_to_those_targets(env):
    root, state = env
    first = project(root, "first")
    second = project(root, "second")
    result = cleanup.run_once([first.parent], state, **NO_PRESSURE, max_target_bytes=SMALL,
                              only_targets=[first])
    assert result["removed"] == [str(first)]
    assert (second / "debug").exists()


def test_only_targets_ignores_a_directory_that_is_not_a_cargo_target(env):
    root, state = env
    plain = root / "plain" / "target"
    plain.mkdir(parents=True)
    (plain / "data").write_bytes(b"x" * 100)
    result = cleanup.run_once([plain.parent], state, **NO_PRESSURE, max_target_bytes=SMALL,
                              only_targets=[plain])
    assert result["removed_count"] == 0 and (plain / "data").exists()


def test_a_just_built_target_can_be_capped_with_a_zero_idle_floor(env):
    root, state = env
    target = project(root, age=0)
    result = cleanup.run_once([target.parent], state, **NO_PRESSURE, max_target_bytes=SMALL,
                              only_targets=[target], size_cap_min_age_hours=0)
    assert result["removed"] == [str(target)]


def test_a_negative_cap_or_idle_floor_is_rejected(env):
    root, state = env
    with pytest.raises(ValueError):
        sweep(root, state, max_target_bytes=-1)
    with pytest.raises(ValueError):
        sweep(root, state, max_target_bytes=SMALL, size_cap_min_age_hours=-1)


def test_policy_converts_gigabytes_and_zero_disables_the_cap():
    assert maintenance.policy_kwargs({"max_target_gb": 50})["max_target_bytes"] == 50 * 1024**3
    assert maintenance.policy_kwargs({})["max_target_bytes"] == 50 * 1024**3
    assert maintenance.policy_kwargs({"max_target_gb": 0})["max_target_bytes"] is None


# --- default discovery of every Rust project under the home directory --------------------------

def test_home_discovery_finds_projects_and_skips_cargo_rustup_hidden_node_modules_and_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    wanted = project(home / "work", "app")
    deep = project(home / "a/b/c", "deep")
    skipped = [project(home / ".cargo/registry", "dep"), project(home / ".rustup", "tc"),
               project(home / ".local/share/Trash/files", "gone"), project(home / ".hidden", "h"),
               project(home / "web/node_modules", "pkg")]
    elsewhere = project(tmp_path / "outside", "other")
    (home / "link").symlink_to(elsewhere.parent)
    found = cleanup._discover(home)
    assert set(found) == {wanted, deep}
    assert not set(found) & set(skipped)


def test_home_discovery_is_depth_bounded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    near = project(home.joinpath(*"abcde"), "p")
    far = project(home.joinpath(*"abcdefgh"), "p")
    found = cleanup._discover(home)
    assert near in found and far not in found


def test_an_explicit_root_is_not_subject_to_the_home_exclusions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hidden = project(tmp_path / "other" / ".hidden", "p")
    assert hidden in cleanup._discover(tmp_path / "other")


def test_default_roots_are_the_home_directory(tmp_path, monkeypatch):
    from herdr_engineering.config import DEFAULTS
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    roots = maintenance.effective_roots(DEFAULTS["build_cleanup"])
    assert home in roots
    assert DEFAULTS["build_cleanup"]["max_target_gb"] == 50
    assert maintenance.effective_roots(DEFAULTS["build_cleanup"], [str(tmp_path)])[0] == tmp_path


# --- after a build ------------------------------------------------------------------------------

def test_after_build_cleans_only_that_builds_oversized_target(env, monkeypatch):
    root, state = env
    mine = project(root, "mine", age=0)
    other = project(root, "other", age=48)
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(state))
    outcome = cleanup.after_build_size_cap(["cargo", "build", "--target-dir", str(mine)], mine.parent,
                                           {"max_target_gb": 0.000001}, state)
    assert outcome["removed"] == [str(mine)]
    assert (other / "debug").exists()


def test_after_build_leaves_a_target_under_the_cap(env):
    root, state = env
    mine = project(root, "mine", age=0)
    outcome = cleanup.after_build_size_cap(["cargo", "build", "--target-dir", str(mine)], mine.parent,
                                           {"max_target_gb": 50}, state)
    assert outcome["removed"] == [] and (mine / "debug").exists()


def test_after_build_is_a_no_op_when_disabled_or_for_other_commands(env):
    root, state = env
    mine = project(root, "mine", age=0)
    cfg = {"max_target_gb": 0.000001}
    assert cleanup.after_build_size_cap(["cargo", "build", "--target-dir", str(mine)], mine.parent,
                                        {**cfg, "enabled": False}, state) is None
    assert cleanup.after_build_size_cap(["cargo", "clean"], mine.parent, cfg, state) is None
    assert cleanup.after_build_size_cap(["make"], mine.parent, cfg, state) is None
    assert (mine / "debug").exists()


def test_after_build_skips_a_target_another_build_still_holds(env):
    root, state = env
    mine = project(root, "mine", age=0)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=mine.parent)
    try:
        outcome = cleanup.after_build_size_cap(["cargo", "build", "--target-dir", str(mine)], mine.parent,
                                               {"max_target_gb": 0.000001}, state)
    finally:
        holder.kill()
        holder.wait()
    assert outcome["removed"] == [] and (mine / "debug").exists()


def test_after_build_never_raises(env):
    root, state = env
    mine = project(root, "mine", age=0)
    unusable = mine.parent / "Cargo.toml"  # a file where the state directory should be
    outcome = cleanup.after_build_size_cap(["cargo", "build", "--target-dir", str(mine)], mine.parent,
                                           {"max_target_gb": 0.000001}, unusable)
    assert outcome is None or outcome.get("errors")
    assert (mine / "debug").exists()


# --- through ``herdr-eng build-coordinate`` -----------------------------------------------------

def _coordinate(tmp_path, workspace, target, cap_gb, wait=True):
    from tests.real_cargo import cargo
    config = tmp_path / "config.yaml"
    config.write_text(f"build_cleanup:\n  max_target_gb: {cap_gb}\nfabric_build_cache:\n  enabled: false\n")
    env = {**os.environ, "HERDR_ENGINEERING_CONFIG": str(config),
           "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
           "PATH": f"{cargo().parent}{os.pathsep}{os.environ['PATH']}",
           "HERDR_DISK_GUARD_OVERRIDE": "1", "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    env.pop("RUSTC_WRAPPER", None)
    argv = [sys.executable, "-m", "herdr_engineering.cli", "build-coordinate", "--", "cargo", "build",
            "--offline", "--target-dir", str(target)]
    if not wait:
        return subprocess.Popen(argv, cwd=workspace, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    return subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True, check=False)


def _crate(tmp_path):
    workspace = tmp_path / "crate"
    (workspace / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)  # a project outside git is never cleaned
    (workspace / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (workspace / "src/main.rs").write_text("fn main() {}\n")
    return workspace


def test_build_coordinate_cleans_an_oversized_target_after_the_build_and_keeps_the_exit_code(tmp_path):
    workspace = _crate(tmp_path)
    target = workspace / "target"
    done = _coordinate(tmp_path, workspace, target, cap_gb=0.000001)
    assert done.returncode == 0, done.stderr
    assert "size cap pass" not in done.stderr, done.stderr
    assert not (target / "debug").exists()
    assert (workspace / "Cargo.toml").exists()


def test_build_coordinate_keeps_a_target_under_the_cap(tmp_path):
    workspace = _crate(tmp_path)
    target = workspace / "target"
    done = _coordinate(tmp_path, workspace, target, cap_gb=50)
    assert done.returncode == 0, done.stderr
    assert (target / "debug").exists()


def test_build_coordinate_keeps_the_exit_code_of_a_failing_build_and_does_not_cap_after_it(tmp_path):
    workspace = _crate(tmp_path)
    target = workspace / "target"
    assert _coordinate(tmp_path, workspace, target, cap_gb=0.000001).returncode == 0
    (workspace / "src/main.rs").write_text("fn main() { this does not compile }\n")
    done = _coordinate(tmp_path, workspace, target, cap_gb=0.000001)
    assert done.returncode == 101, done.stderr
    assert "size cap pass" not in done.stderr
    assert (target / "debug").exists()  # a failed build is not followed by a clean


def test_build_coordinate_forwards_sigterm_to_the_build_and_reports_the_signal(tmp_path):
    import signal
    workspace = _crate(tmp_path)
    started = tmp_path / "started"
    (workspace / "build.rs").write_text(
        f'fn main() {{ std::fs::write({json.dumps(str(started))}, "x").unwrap(); '
        'std::thread::sleep(std::time::Duration::from_secs(120)); }\n')
    proc = _coordinate(tmp_path, workspace, workspace / "target", cap_gb=50, wait=False)
    try:
        deadline = time.time() + 120
        while not started.exists() and time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert started.exists(), proc.communicate()
        began = time.time()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
        assert time.time() - began < 60
        assert proc.returncode == -signal.SIGTERM  # a death by the signal, as an exec'd build would have
    finally:
        proc.kill()
        proc.wait()
        subprocess.run(["pkill", "-f", str(started.parent)], check=False)
