"""The narrow default cleanup: orphaned targets, and idle targets only while no cargo-family process runs.

Real filesystems, real git, a real cargo and rustc, real processes; nothing is mocked. Pressure comes from
thresholds against the real measured usage. The one seam is ``processes=`` of ``run_once``: the positive
tests use the real census minus the cargo-family processes this developer host already runs (an unrelated
``rust-analyzer`` of another session must not decide whether a test may clean), and the tests that need a
busy host start their own real processes.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import disk_guard
from herdr_engineering.cargo_target_storage import prepare_target

REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("CARGO_HOME", os.environ.get("CARGO_HOME", str(REAL_HOME / ".cargo")))
    monkeypatch.setenv("RUSTUP_HOME", os.environ.get("RUSTUP_HOME", str(REAL_HOME / ".rustup")))


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "projects"
    path.mkdir()
    return Path(os.path.realpath(path))


def _cargo() -> Path:
    for candidate in (REAL_HOME / ".cargo/bin/cargo", shutil.which("cargo") or ""):
        path = Path(candidate) if candidate else None
        if path and path.is_file() and ".local/bin" not in str(path):
            return path
    pytest.skip("a real cargo is required")


def _env(**extra: str) -> dict:
    env = {**os.environ, **extra}
    env["PATH"] = f"{_cargo().parent}{os.pathsep}{env['PATH']}"
    env["CARGO_HOME"] = os.environ.get("CARGO_HOME", str(REAL_HOME / ".cargo"))
    env.setdefault("RUSTUP_HOME", os.environ.get("RUSTUP_HOME", str(REAL_HOME / ".rustup")))
    for key in ("RUSTC_WRAPPER", "CARGO_TARGET_DIR", "HERDR_CARGO_HOST_COORDINATED"):
        if key not in extra:
            env.pop(key, None)
    return env


def crate(path: Path, build_seconds: int = 0) -> Path:
    """A real crate; with ``build_seconds`` its build script keeps a real ``cargo build`` running."""
    (path / "src").mkdir(parents=True)
    manifest = '[package]\nname = "fx"\nversion = "0.1.0"\nedition = "2021"\n'
    if build_seconds:
        manifest += 'build = "build.rs"\n'
        (path / "build.rs").write_text(
            f"fn main() {{ std::thread::sleep(std::time::Duration::from_secs({build_seconds})); }}\n")
    (path / "Cargo.toml").write_text(manifest)
    (path / "src/main.rs").write_text("fn main() {}\n")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _age(target: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    for path in [*target.rglob("*"), target]:
        os.utime(path, (stamp, stamp), follow_symlinks=False)


def _output(target: Path, hours: float) -> Path:
    (target / "debug/.fingerprint").mkdir(parents=True, exist_ok=True)
    (target / "debug/artifact").write_bytes(b"x" * 4096)
    (target / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n")  # newer cargo refuses to clean a directory without it
    _age(target, hours)
    return target


def local_target(root: Path, name: str = "project", hours: float = 48) -> Path:
    """``<root>/<name>/target``: an operator's target beside a real crate."""
    return _output(crate(root / name) / "target", hours)


def managed_target(root: Path, name: str = "app", hours: float = 48) -> tuple[Path, Path]:
    """A real managed target and the real crate (worktree) that owns it."""
    workspace = crate(root.parent / "workspaces" / name)
    (root / "sccache").mkdir(exist_ok=True)
    return _output(prepare_target(root / "sccache", workspace), hours), workspace


def _descendants() -> set[int]:
    """Pids below this test process (a build the test started), from a real ``ps``."""
    rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    children: dict[int, list[int]] = {}
    for line in rows.splitlines():
        pid, ppid = (int(part) for part in line.split())
        children.setdefault(ppid, []).append(pid)
    found, queue = set(), [os.getpid()]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.add(child)
                queue.append(child)
    return found


def quiet():
    """The real census, narrowed to what this test started: another session's cargo or rust-analyzer on a
    developer host must neither block nor excuse a sweep here."""
    return lambda: ([p for p in cleanup._cargo_processes() if p[0] in _descendants()], cleanup._process_paths())


def sweep(root: Path, processes=None, **kwargs):
    usage = cleanup._usage(root)
    if usage <= 0:
        pytest.skip("the filesystem reports no usage at all")
    base = {"high_water": usage / 2, "low_water": 0, "min_age_hours": 24, "processes": processes or quiet()}
    return cleanup.run_once([root], root.parent / "state", **{**base, **kwargs})


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _wait_for(predicate, what: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _slow_build(crate_dir: Path, target: Path) -> subprocess.Popen:
    proc = subprocess.Popen([str(_cargo()), "build", "--offline"], cwd=crate_dir,
                            env=_env(CARGO_TARGET_DIR=str(target)), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    _wait_for(lambda: any(name == "cargo" for _, name in cleanup._cargo_processes()) and
              (target / "debug").is_dir(), "the build to be running")
    return proc


def _program(directory: Path, name: str) -> subprocess.Popen:
    """A real executable called ``name`` (compiled by the real rustc) that stays alive."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / f"{name}.rs"
    source.write_text("fn main() { std::thread::sleep(std::time::Duration::from_secs(300)); }\n")
    binary = directory / name
    subprocess.run([str(_cargo().parent / "rustc"), "-o", str(binary), str(source)], check=True,
                   env=_env(), capture_output=True)
    proc = subprocess.Popen([str(binary)], start_new_session=True)
    if name != "napper":  # the census must see it before the sweep looks
        _wait_for(lambda: any(pid == proc.pid for pid, _ in cleanup._cargo_processes()), f"{name} to run")
    return proc


# --- idle targets are cleaned, through cargo, when nothing cargo-like runs ----------------------

def test_an_idle_operator_target_is_cleaned_by_cargo_and_the_sources_stay(root):
    target = local_target(root)
    result = sweep(root)
    assert result["removed"] == [str(target)] and not target.exists(), result["skipped"][0]["reason"]
    assert (target.parent / "src/main.rs").is_file() and (target.parent / "Cargo.toml").is_file()


def test_an_idle_managed_target_is_cleaned_by_cargo_and_prepares_cleanly_again(root):
    target, workspace = managed_target(root)
    assert sweep(root)["removed"] == [str(target)]
    assert os.listdir(target) == [".herdr-cargo-target.json"]  # emptied, still owned: a ./target alias never dangles
    assert prepare_target(root / "sccache", workspace) == target and target.is_dir()  # re-prepared


def test_a_recent_target_is_never_cleaned_and_pressure_lowers_only_the_idle_floor(root):
    recent = local_target(root, "recent", hours=0.5)
    idle = local_target(root, "idle", hours=3)
    assert sweep(root, pressure_water=100)["removed_count"] == 0  # normal floor: 24 h
    pressured = sweep(root, pressure_water=0)
    assert pressured["pressure"] is True and pressured["removed"] == [str(idle)]
    assert recent.exists()


def test_a_dry_run_names_what_it_would_clean_and_deletes_nothing(root):
    target = local_target(root)
    result = sweep(root, dry_run=True)
    assert result["would_remove"] == [str(target)] and target.exists()


def test_a_target_cargo_cannot_clean_is_kept_and_named(root):
    target = local_target(root)
    (target.parent / "Cargo.toml").write_text("this is not a manifest\n")
    result = sweep(root)
    assert result["removed"] == [] and target.exists()
    assert any(item["path"] == str(target) and "cargo clean" in item["reason"] for item in result["skipped"])


def test_a_target_without_a_workspace_to_clean_through_is_left_alone(root):
    pool = root / "pool/slot/target"
    _output(pool, 48)
    (pool / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    _age(pool, 48)
    result = sweep(root)
    assert result["removed"] == [] and pool.exists()


# --- nothing non-orphaned is cleaned while any cargo-family process runs ---------------------------

@pytest.mark.integration
def test_nothing_is_cleaned_while_a_real_cargo_build_runs_anywhere(root):
    busy, busy_crate = managed_target(root, "busy")
    other = local_target(root, "other")  # an unrelated idle target: still protected this pass
    build = _slow_build(crate(root.parent / "workspaces/slow", 25), busy)
    try:
        _age(busy, 48)
        held = sweep(root)
        assert held["removed"] == [] and busy.exists() and other.exists(), held
        assert all("cargo" in item["reason"] for item in held["skipped"] if item["path"] in {str(busy), str(other)})
    finally:
        _stop(build)
    _age(busy, 48)
    assert sorted(sweep(root)["removed"]) == sorted([str(busy), str(other)])  # the build is gone


@pytest.mark.integration
@pytest.mark.parametrize("name", ["rustc", "rust-analyzer", "cargo-nextest"])
def test_nothing_is_cleaned_while_a_process_of_that_name_runs(root, name, tmp_path):
    target = local_target(root)
    proc = _program(tmp_path / "bin", name)
    try:
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
        assert any(name in item["reason"] for item in result["skipped"]), result
    finally:
        _stop(proc)
    assert sweep(root)["removed"] == [str(target)]


@pytest.mark.integration
def test_nothing_is_cleaned_while_a_test_binary_runs_from_a_target(root):
    target = local_target(root)
    proc = _program(target / "debug/deps", "napper")
    try:
        _age(target, 48)
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
    finally:
        _stop(proc)
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]


def test_an_unreadable_process_list_fails_closed_and_is_reported(root, monkeypatch):
    target = local_target(root)
    monkeypatch.setenv("PATH", "")  # no ``ps`` to ask
    usage = cleanup._usage(root)
    result = cleanup.run_once([root], root.parent / "state", high_water=max(usage / 2, 0.1), low_water=0,
                              min_age_hours=24)
    assert result["removed"] == [] and target.exists()
    assert result["aborted"] is True and result["errors"]


# --- orphaned targets are provably dead and go regardless ------------------------------------------

@pytest.mark.integration
def test_an_orphaned_target_is_cleaned_even_while_cargo_runs(root):
    orphan, workspace = managed_target(root, "gone", hours=0.1)  # even a fresh one
    live, _ = managed_target(root, "alive")
    shutil.rmtree(workspace)
    build = _slow_build(crate(root.parent / "workspaces/slow", 25), live)
    try:
        _age(live, 48)
        result = sweep(root)
        assert result["removed"] == [str(orphan)] and not orphan.exists() and live.exists(), result
    finally:
        _stop(build)


def test_a_worktree_recreated_at_the_same_path_is_not_an_orphan(root):
    target, workspace = managed_target(root, "back")
    shutil.rmtree(workspace)
    crate(workspace)  # recreated before the sweep looks
    result = sweep(root, min_age_hours=100000)
    assert result["removed"] == [] and target.exists()


def test_an_orphan_is_decided_under_the_manager_lock(root, tmp_path):
    """The worktree comes back (and is re-prepared) after the sweep listed the orphan but before it holds
    the manager lock: the sweep must look again under the lock and leave the target alone."""
    import fcntl
    orphan, workspace = managed_target(root, "race", hours=0.01)  # fresh: only an orphan is a candidate
    managed_target(root, "other")  # a live, idle target, so the sweep takes its process snapshot
    shutil.rmtree(workspace)
    ready = tmp_path / "sweep-is-past-listing"
    fd = os.open(orphan.parent / ".target-manager.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    pid = os.fork()
    if pid == 0:
        census = quiet()

        def snapshot():
            ready.write_text("go")
            return census()
        try:
            result = sweep(root, processes=snapshot)
            os._exit(0 if str(orphan) not in result["removed"] else 1)
        except BaseException:
            os._exit(2)
    _wait_for(ready.exists, "the sweep to list its candidates")
    crate(workspace)  # recreated while the sweep waits for the lock
    fcntl.flock(fd, fcntl.LOCK_UN)  # the forked sweep shares the lock's open file description
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0 and orphan.exists()


def test_a_missing_volume_is_not_proof_of_an_orphan(root):
    target, workspace = managed_target(root, "volume")
    marker = target / ".herdr-cargo-target.json"
    import json
    data = json.loads(marker.read_text())
    data["workspace"] = "/Volumes/not-mounted-here/deeper/workspace"
    marker.write_text(json.dumps(data))
    _age(target, 48)
    result = sweep(root)
    assert result["removed"] == [] and target.exists()


# --- the guard: the same safe set under pressure; nothing safe means a refusal ------------------------

def _policy(root: Path) -> dict:
    return {"enabled": True, "roots": [str(root)], "high_water_percent": 1, "low_water_percent": 0,
            "min_age_hours": 24, "pressure_percent": 0, "pressure_min_age_hours": 2,
            "refuse_builds_percent": 0, "refuse_builds_min_free_gib": 1e12}


@pytest.mark.integration
def test_pressure_with_only_busy_targets_refuses_and_frees_nothing(root, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "hstate"))
    busy, _ = managed_target(root, "busy")
    build = _slow_build(crate(root.parent / "workspaces/slow", 25), busy)
    try:
        _age(busy, 48)
        decision = disk_guard.check_build_allowed(root, _policy(root), tmp_path / "gstate", env={},
                                                  processes=quiet())
        assert not decision.allowed and "refusing to start a new build" in decision.message
        assert busy.is_dir() and disk_guard.EXIT_REFUSED == 75
    finally:
        _stop(build)


def test_pressure_cleans_the_idle_set_and_still_refuses_when_that_is_not_enough(root, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "hstate"))
    idle = local_target(root, hours=5)
    decision = disk_guard.check_build_allowed(root, _policy(root), tmp_path / "gstate", env={},
                                              processes=quiet())
    assert not decision.allowed and decision.cleanup_ran and not idle.exists()
