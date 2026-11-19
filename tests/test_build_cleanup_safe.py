"""The narrow default cleanup: orphaned targets, and idle targets that nothing is using.

Real filesystems, real git, a real cargo and rustc, real processes; nothing is mocked and the sweep takes no
test seam: a process of another session on the developer host does not touch these temporary workspaces, so
it cannot decide whether a test may clean. Targets are built by a real ``cargo build`` (managed ones through
``prepare_target``), so nothing here writes a ``CACHEDIR.TAG`` by hand. Pressure comes from thresholds
against the real measured usage.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import disk_guard
from herdr_engineering.cargo_target_storage import CACHEDIR_TAG, TARGET_MARKER, prepare_target

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


def _build(workspace: Path, target: Path) -> None:
    subprocess.run([str(_cargo()), "build", "--offline"], cwd=workspace, env=_env(CARGO_TARGET_DIR=str(target)),
                   check=True, capture_output=True)


def _output(workspace: Path, target: Path, hours: float) -> Path:
    """A real build into ``target``, aged ``hours``."""
    _build(workspace, target)
    _age(target, hours)
    return target


def local_target(root: Path, name: str = "project", hours: float = 48) -> Path:
    """``<root>/<name>/target``: an operator's target beside a real crate, as cargo made it."""
    workspace = crate(root / name)
    return _output(workspace, workspace / "target", hours)


def managed_target(root: Path, name: str = "app", hours: float = 48) -> tuple[Path, Path]:
    """A real managed target (prepared by the manager, built by cargo) and the crate that owns it."""
    workspace = crate(root.parent / "workspaces" / name)
    (root / "sccache").mkdir(exist_ok=True)
    return _output(workspace, prepare_target(root / "sccache", workspace), hours), workspace


def sweep(root: Path, **kwargs):
    usage = cleanup._usage(root)
    if usage <= 0:
        pytest.skip("the filesystem reports no usage at all")
    base = {"high_water": usage / 2, "low_water": 0, "min_age_hours": 24}
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


def _slow_build(crate_dir: Path, target: Path, how: str = "env") -> subprocess.Popen:
    """A real ``cargo build`` (its build script sleeps) that writes into ``target``: told by the environment
    or by ``--target-dir``."""
    argv = [str(_cargo()), "build", "--offline"] + (["--target-dir", str(target)] if how == "argv" else [])
    env = _env(CARGO_TARGET_DIR=str(target)) if how == "env" else _env()
    proc = subprocess.Popen(argv, cwd=crate_dir, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    _wait_for(lambda: cleanup._busy_reason(cleanup._scan(), target) is not None, "the build to be seen")
    return proc


def _program(directory: Path, name: str, cwd: Path | None = None) -> subprocess.Popen:
    """A real executable called ``name`` (compiled by the real rustc) that stays alive."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / f"{name}.rs"
    source.write_text("fn main() { std::thread::sleep(std::time::Duration::from_secs(300)); }\n")
    binary = directory / name
    subprocess.run([str(_cargo().parent / "rustc"), "-o", str(binary), str(source)], check=True,
                   env=_env(), capture_output=True)
    proc = subprocess.Popen([str(binary)], cwd=cwd, start_new_session=True)
    _wait_for(lambda: any(hold[0] == proc.pid for hold in cleanup._scan()), f"{name} to run")
    return proc


# --- idle targets are cleaned, through cargo ----------------------------------------------------

def test_an_idle_operator_target_is_cleaned_by_cargo_and_the_sources_stay(root):
    target = local_target(root)
    result = sweep(root)
    assert result["removed"] == [str(target)] and not target.exists(), result["skipped"]
    assert (target.parent / "src/main.rs").is_file() and (target.parent / "Cargo.toml").is_file()


def test_an_idle_managed_target_is_cleaned_by_cargo_and_prepares_cleanly_again(root):
    target, workspace = managed_target(root)
    assert sweep(root)["removed"] == [str(target)]
    assert sorted(os.listdir(target)) == sorted([TARGET_MARKER, "CACHEDIR.TAG"])  # emptied, still owned and tagged
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
    _output(crate(root.parent / "elsewhere"), pool, 48)  # tagged by cargo itself, no manifest beside it
    result = sweep(root)
    assert result["removed"] == [] and pool.exists()


# --- managed targets carry the CACHEDIR.TAG that newer cargo demands before it cleans ---------------

def test_prepare_target_writes_the_standard_tag_and_backfills_older_targets(root):
    workspace = crate(root.parent / "workspaces/tagged")
    (root / "sccache").mkdir()
    target = prepare_target(root / "sccache", workspace)
    assert (target / "CACHEDIR.TAG").read_text() == CACHEDIR_TAG
    assert (target / "CACHEDIR.TAG").read_bytes().startswith(b"Signature: 8a477f597d28d172789f06886806bc55")
    (target / "CACHEDIR.TAG").unlink()  # a target made before the tag existed
    assert prepare_target(root / "sccache", workspace) == target
    assert (target / "CACHEDIR.TAG").read_text() == CACHEDIR_TAG


def test_an_old_managed_target_without_a_tag_is_still_cleaned(root):
    target, _ = managed_target(root)
    (target / "CACHEDIR.TAG").unlink()
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]


# --- only what a process is using is kept; the rest of the host does not matter -----------------------

@pytest.mark.integration
@pytest.mark.parametrize("how", ["env", "argv"])
def test_a_real_cargo_build_into_a_target_keeps_that_target_only(root, how):
    busy, _ = managed_target(root, "busy")
    other = local_target(root, "other")  # idle and unrelated: cleaned while the build runs
    build = _slow_build(crate(root.parent / "workspaces/slow", 25), busy, how)
    try:
        _age(busy, 48)
        held = sweep(root)
        assert held["removed"] == [str(other)] and busy.exists(), held
        reason = next(item["reason"] for item in held["skipped"] if item["path"] == str(busy))
        assert reason.startswith("cargo (pid ") and ("builds into" in reason or "uses" in reason), reason
        assert held["blocked"] == [{"path": str(busy), "reason": reason}]
    finally:
        _stop(build)
    _age(busy, 48)
    assert sweep(root)["removed"] == [str(busy)]  # the build is gone


@pytest.mark.integration
@pytest.mark.parametrize("name", ["rustc", "rust-analyzer", "cargo-nextest"])
def test_cargo_family_processes_elsewhere_do_not_stop_the_cleanup(root, name, tmp_path):
    target = local_target(root)
    proc = _program(tmp_path / "bin", name)  # an IDE's rust-analyzer, a watcher: nowhere near the target
    try:
        result = sweep(root)
        assert result["removed"] == [str(target)] and not target.exists(), result
    finally:
        _stop(proc)


@pytest.mark.integration
@pytest.mark.parametrize("name", ["napper", "rust-analyzer"])
def test_a_process_with_its_cwd_in_the_workspace_keeps_the_target(root, name, tmp_path):
    managed, workspace = managed_target(root, "ws")
    local = local_target(root, "local")
    other = local_target(root, "other")
    held = [_program(tmp_path / "bin", name, cwd=workspace), _program(tmp_path / "bin2", name, cwd=local.parent)]
    try:
        _age(managed, 48)
        result = sweep(root)
        assert result["removed"] == [str(other)] and managed.exists() and local.exists(), result
        assert all("uses" in item["reason"] for item in result["skipped"])
    finally:
        for proc in held:
            _stop(proc)


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


@pytest.mark.integration
def test_a_library_mapped_from_a_target_keeps_it_after_its_descriptor_is_closed(root, tmp_path):
    target = local_target(root)
    library = target / "debug/deps" / ("libfx.dylib" if sys.platform == "darwin" else "libfx.so")
    source = tmp_path / "fx.rs"
    source.write_text('#[no_mangle] pub extern "C" fn fx() -> i32 { 7 }\n')
    subprocess.run([str(_cargo().parent / "rustc"), "--crate-type", "cdylib", "-o", str(library), str(source)],
                   check=True, env=_env(), capture_output=True)
    ready = tmp_path / "loaded"
    code = ("import ctypes,sys,time,pathlib; lib=ctypes.CDLL(sys.argv[1]); pathlib.Path(sys.argv[2]).write_text('1');"
            "time.sleep(300)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(library), str(ready)], cwd=tmp_path,
                            start_new_session=True)  # cwd, executable and descriptors are all elsewhere
    try:
        _wait_for(ready.exists, "the library to load")
        _age(target, 48)
        result = sweep(root)
        assert result["removed"] == [] and library.exists(), result
        assert "uses" in next(i["reason"] for i in result["skipped"] if i["path"] == str(target))
    finally:
        _stop(proc)
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]


def test_a_scan_that_fails_fails_closed_and_is_reported(root, tmp_path, monkeypatch):
    if Path("/proc").is_dir():
        pytest.skip("on /proc hosts the scan has no external program to lose")
    target = local_target(root)
    tools = tmp_path / "only-git"
    tools.mkdir()
    (tools / "git").symlink_to(shutil.which("git") or "git")
    monkeypatch.setenv("PATH", str(tools))  # git, but no ``lsof`` or ``ps`` to ask
    result = cleanup.run_once([root], root.parent / "state", high_water=max(cleanup._usage(root) / 2, 0.1),
                              low_water=0, min_age_hours=24)
    assert result["removed"] == [] and target.exists()
    assert result["aborted"] is True and result["errors"]


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
def test_an_unverifiable_process_fails_the_scan_closed(tmp_path):
    proc = tmp_path / "proc"
    (proc / str(os.getpid() + 1)).mkdir(parents=True)  # a process directory the scan cannot read
    (proc / str(os.getpid() + 1)).chmod(0)
    try:
        if os.geteuid() == 0:
            pytest.skip("root can read anything")
        with pytest.raises(OSError):
            cleanup._scan(proc)
    finally:
        (proc / str(os.getpid() + 1)).chmod(0o700)


# --- a process that appears after the sweep started is still seen ------------------------------------

def _wait_blocked_on(lock_file: Path) -> int:
    """Hold the manager lock so a forked sweep stops at its first clean; the sweep is given time to get there."""
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _finish(fd: int, pid: int) -> int:
    fcntl.flock(fd, fcntl.LOCK_UN)  # the forked sweep shares the lock's open file description
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _abandon(fd: int, pid: int) -> None:
    """Never leave the forked sweep behind (it would hold the test run's pipes open) if the test failed early."""
    fcntl.flock(fd, fcntl.LOCK_UN)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


@pytest.mark.integration
@pytest.mark.parametrize("managed", [True, False])
def test_a_process_started_after_the_sweep_began_keeps_the_target_it_uses(root, tmp_path, managed):
    first, _ = managed_target(root, "first", hours=60)  # cleaned first; its clean waits for the manager lock
    if managed:
        second, _ = managed_target(root, "second", hours=48)
    else:
        second = local_target(root, "second", hours=48)
    fd = _wait_blocked_on(first.parent / ".target-manager.lock")
    pid = os.fork()
    if pid == 0:
        try:
            result = sweep(root)
            os._exit(0 if str(second) not in result["removed"] else 1)
        except BaseException:
            os._exit(2)
    proc = None
    try:
        time.sleep(2)  # the sweep has listed everything and is waiting at the first clean
        proc = _program(second / "debug/deps", "napper")  # starts only now: any scan taken before misses it
        _age(second, 48)  # compiling it touched the target; only the process keeps it now
        assert _finish(fd, pid) == 0 and second.exists() and (second / "debug/deps/napper").exists()
    finally:
        _abandon(fd, pid)
        if proc is not None:
            _stop(proc)
    assert not (first / "debug").exists()  # the sweep did clean the idle one


@pytest.mark.integration
def test_a_target_whose_worktree_comes_back_is_checked_for_processes_when_the_sweep_reaches_it(root, tmp_path):
    older, older_workspace = managed_target(root, "older", hours=100)
    back, back_workspace = managed_target(root, "back", hours=48)
    shutil.rmtree(older_workspace)
    shutil.rmtree(back_workspace)  # both look orphaned when the sweep lists them: no scan would be needed
    fd = _wait_blocked_on(older.parent / ".target-manager.lock")
    pid = os.fork()
    if pid == 0:
        try:
            result = sweep(root)
            os._exit(0 if str(back) not in result["removed"] else 1)
        except BaseException:
            os._exit(2)
    proc = None
    try:
        time.sleep(2)  # the sweep waits to remove the first orphan
        crate(back_workspace)  # the worktree is back ...
        proc = _program(back / "debug/deps", "napper")  # ... and something runs from its target
        _age(back, 48)
        assert _finish(fd, pid) == 0 and (back / "debug/deps/napper").exists()
    finally:
        _abandon(fd, pid)
        if proc is not None:
            _stop(proc)
    assert not older.exists()


# --- orphaned targets are provably dead and go regardless ------------------------------------------

@pytest.mark.integration
def test_an_orphaned_target_is_cleaned_even_while_cargo_builds_elsewhere(root):
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
    target, workspace = managed_target(root, "back", hours=0.01)  # fresh: only an orphan is a candidate
    shutil.rmtree(workspace)
    crate(workspace)  # recreated before the sweep looks
    result = sweep(root)
    assert result["removed"] == [] and target.exists()


def test_an_orphan_is_decided_under_the_manager_lock(root):
    """The worktree comes back (and is re-prepared) after the sweep listed the orphan but before it holds
    the manager lock: the sweep must look again under the lock and leave the target alone."""
    orphan, workspace = managed_target(root, "race", hours=0.01)  # fresh: only an orphan is a candidate
    shutil.rmtree(workspace)
    fd = _wait_blocked_on(orphan.parent / ".target-manager.lock")
    pid = os.fork()
    if pid == 0:
        try:
            result = sweep(root)
            os._exit(0 if str(orphan) not in result["removed"] else 1)
        except BaseException:
            os._exit(2)
    try:
        time.sleep(2)
        crate(workspace)  # recreated while the sweep waits for the lock
        assert _finish(fd, pid) == 0 and orphan.exists()
    finally:
        _abandon(fd, pid)


def test_a_missing_volume_is_not_proof_of_an_orphan(root):
    target, workspace = managed_target(root, "volume")
    marker = target / TARGET_MARKER
    data = json.loads(marker.read_text())
    data["workspace"] = "/Volumes/not-mounted-here/deeper/workspace"
    marker.write_text(json.dumps(data))
    _age(target, 48)
    result = sweep(root)
    assert result["removed"] == [] and target.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("mode", [0o600, 0o000])
def test_a_worktree_that_cannot_be_looked_at_is_not_an_orphan(root, tmp_path, mode):
    """macOS TCC and plain permissions: ``lstat`` of the worktree fails with EACCES/EPERM although it exists."""
    parent = tmp_path / "Documents"
    workspace = crate(parent / "app")
    (root / "sccache").mkdir()
    target = prepare_target(root / "sccache", workspace)
    _build(workspace, target)  # a build just wrote it: fresh
    parent.chmod(mode)
    try:
        assert cleanup._orphaned(target) is False
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
    finally:
        parent.chmod(0o700)
    assert (workspace / "Cargo.toml").is_file()


# --- an interrupted removal leaves nothing that is never cleaned ------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_orphan_removal_that_stops_part_way_is_finished_by_the_next_sweep(root):
    target, workspace = managed_target(root, "stuck", hours=0.01)
    shutil.rmtree(workspace)
    (target / "debug/readonly").mkdir()
    (target / "debug/readonly/file").write_bytes(b"x" * 4096)
    (target / "debug/readonly").chmod(0o500)  # rmtree cannot empty it
    try:
        first = sweep(root)
        trash = [p for p in target.parent.iterdir() if p.name.startswith(".trash-")]
        assert first["errors"] and len(trash) == 1 and not target.exists(), first
        assert trash[0].name == f".trash-{target.name}-{os.getpid()}"
    finally:
        for path in trash:
            (path / "debug/readonly").chmod(0o700)
    second = sweep(root)
    assert not second["errors"] and not [p for p in target.parent.iterdir() if p.name.startswith(".trash-")], second
    assert (target.parent / ".herdr-target-root.json").is_file()


# --- cargo clean stops part-way: the target stays a managed one --------------------------------------------

@pytest.mark.parametrize("names", [("a", "m", "z"), ("0", "1", "2"), ("zz", "yy", "xx"), ("_", "-", "~")])
def test_a_cargo_clean_that_times_out_leaves_the_ownership_marker(root, names):
    target, workspace = managed_target(root, "slowclean")
    for index in range(60):  # a large target: cargo is still deleting when the timeout fires
        directory = target / names[index % 3] / f"d{index}"
        directory.mkdir(parents=True)
        for number in range(150):
            (directory / f"f{number}").write_bytes(b"x")
    reason = cleanup._cargo_clean(target, timeout=0.15)
    assert reason is None or "could not run" in reason  # killed by the timeout, or (fast host) just done
    assert target.is_dir() and (target / TARGET_MARKER).is_file()
    assert prepare_target(root / "sccache", workspace) == target  # the next build is not refused


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("name", ["a", "m", "zz", "0"])
def test_a_cargo_clean_that_fails_part_way_leaves_the_ownership_marker(root, name):
    target, workspace = managed_target(root, "failclean")
    for index in range(30):
        (target / f"{name}{index}").mkdir()
        (target / f"{name}{index}/f").write_bytes(b"x")
    (target / name).mkdir()
    (target / name / "f").write_bytes(b"x")
    (target / name).chmod(0o500)
    try:
        assert "cargo clean failed" in (cleanup._cargo_clean(target) or "")
        assert (target / TARGET_MARKER).is_file()
    finally:
        (target / name).chmod(0o700)
    assert prepare_target(root / "sccache", workspace) == target


# --- the guard: the same safe set under pressure; nothing safe means a refusal ------------------------

def _policy(root: Path) -> dict:
    return {"enabled": True, "roots": [str(root)], "high_water_percent": 1, "low_water_percent": 0,
            "min_age_hours": 24, "pressure_percent": 0, "pressure_min_age_hours": 2,
            "refuse_builds_percent": 0, "refuse_builds_min_free_gib": 1e12}


@pytest.mark.integration
def test_pressure_with_only_busy_targets_refuses_and_names_what_blocked_it(root, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "hstate"))
    busy, _ = managed_target(root, "busy")
    build = _slow_build(crate(root.parent / "workspaces/slow", 25), busy)
    try:
        _age(busy, 48)
        decision = disk_guard.check_build_allowed(root, _policy(root), tmp_path / "gstate", env={})
        assert not decision.allowed and "refusing to start a new build" in decision.message
        assert busy.is_dir() and disk_guard.EXIT_REFUSED == 75
        assert "left a target alone" in decision.message and "cargo (pid " in decision.message, decision.message
    finally:
        _stop(build)


def test_pressure_cleans_the_idle_set_and_still_refuses_when_that_is_not_enough(root, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "hstate"))
    idle = local_target(root, hours=5)
    decision = disk_guard.check_build_allowed(root, _policy(root), tmp_path / "gstate", env={})
    assert not decision.allowed and decision.cleanup_ran and not idle.exists()
