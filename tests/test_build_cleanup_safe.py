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
        for _ in range(40):  # the running build may still be writing: that is "recent activity", try again
            _age(busy, 48)
            held = sweep(root)
            if held["blocked"] or busy.exists() is False:
                break
            time.sleep(0.25)
        assert held["removed"] in ([str(other)], []) and busy.exists(), held
        assert held["blocked"], held
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


# --- round 12: identity, not spelling; one bad leftover; ownership outside the deleted tree ---------------

def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    return (directory / "caseprobe").exists()


@pytest.mark.integration
def test_a_root_spelled_in_another_case_still_sees_the_process_using_its_target(tmp_path):
    """On a case-insensitive disk ``~/work`` and ``~/Work`` are one folder; lsof reports the on-disk spelling."""
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    real = Path(os.path.realpath(tmp_path)) / "Work"
    real.mkdir()
    target = local_target(real, "app")
    proc = _program(target / "debug/deps", "napper", cwd=target.parent)
    _age(target, 48)
    try:
        result = sweep(real.parent / "work")  # the root as the operator typed it
        assert result["removed"] == [] and target.exists(), result
        assert result["blocked"] and "napper" in result["blocked"][0]["reason"], result
    finally:
        _stop(proc)


def test_a_process_path_through_a_symlink_is_the_same_directory(root, tmp_path):
    """The busy check compares what the paths are (device and inode), never how they are spelled."""
    target = local_target(root)
    alias = tmp_path / "alias"
    alias.symlink_to(target.parent, target_is_directory=True)
    holds = [(4242, "napper", "uses", alias / "target/debug/deps/napper", frozenset())]
    assert cleanup._busy_reason(holds, target) is not None
    assert cleanup._busy_reason([(4242, "napper", "uses", tmp_path / "elsewhere/bin", frozenset())], target) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_trash_leftover_that_cannot_be_deleted_only_skips_itself(root):
    orphan, workspace = managed_target(root, "orphan", hours=0.01)
    shutil.rmtree(workspace)
    stuck = orphan.parent / ".trash-old-123"
    (stuck / "debug/readonly").mkdir(parents=True)
    (stuck / "debug/readonly/file").write_bytes(b"x")
    (stuck / "debug/readonly").chmod(0o500)  # rmtree cannot empty it
    try:
        result = sweep(root)
        assert result["removed"] == [str(orphan)] and not orphan.exists(), result  # the rest still gets cleaned
        assert [item for item in result["skipped"] if item["path"] == str(stuck)], result
        assert not any("skipped root" in warning for warning in result["warnings"]), result
    finally:
        (stuck / "debug/readonly").chmod(0o700)


def test_a_managed_target_without_its_marker_is_still_owned_and_prepares_again(root):
    target, workspace = managed_target(root, "markerless")
    (target / TARGET_MARKER).unlink()  # what a killed ``cargo clean`` can leave
    assert cleanup._managed_workspace(target) == workspace.resolve()
    assert prepare_target(root / "sccache", workspace) == target
    assert (target / TARGET_MARKER).is_file()
    _build(workspace, target)  # and the next build works


def test_a_directory_nobody_owns_is_still_refused(root):
    workspace = crate(root.parent / "workspaces/stranger")
    (root / "sccache").mkdir(exist_ok=True)
    stranger = prepare_target(root / "sccache", workspace)
    (stranger / TARGET_MARKER).unlink()
    for record in (stranger.parent / ".owners").glob("*"):
        record.unlink()
    (stranger / "file").write_text("not ours")
    with pytest.raises(ValueError, match="no ownership marker"):
        prepare_target(root / "sccache", workspace)


@pytest.mark.integration
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_clean_killed_in_the_middle_leaves_a_target_the_next_build_can_use(root):
    target, workspace = managed_target(root, "killed")
    for index in range(80):
        directory = target / ("a", "m", "z")[index % 3] / f"d{index}"
        directory.mkdir(parents=True)
        for number in range(200):
            (directory / f"f{number}").write_bytes(b"x")
    marker = target / TARGET_MARKER
    pid = os.fork()
    if pid == 0:
        os.setsid()  # the sweep and its cargo share one group, so one SIGKILL stops both
        cleanup._cargo_clean(target)
        os._exit(0)
    try:
        _wait_for(lambda: not marker.exists() or sum(1 for _ in target.iterdir()) < 5, "cargo to start deleting")
    finally:
        os.killpg(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    assert prepare_target(root / "sccache", workspace) == target
    assert marker.is_file()
    _build(workspace, target)


def _shared_build_dir(workspace: Path, shared: Path) -> None:
    (workspace / ".cargo").mkdir()
    (workspace / ".cargo/config.toml").write_text(f'[build]\nbuild-dir = "{shared}"\n')


@pytest.mark.integration
@pytest.mark.parametrize("how", ["config", "env"])
def test_cleaning_one_target_never_deletes_a_shared_build_dir(root, tmp_path, monkeypatch, how):
    shared = tmp_path / "shared-build-dir"
    first = local_target(root, "first", hours=0.01)
    other_ws = crate(root / "other")
    if how == "env":
        monkeypatch.setenv("CARGO_BUILD_BUILD_DIR", str(shared))
    else:
        _shared_build_dir(first.parent, shared)
        _shared_build_dir(other_ws, shared)
    _build(first.parent, first)
    _build(other_ws, other_ws / "target")  # the other workspace's intermediates live in the shared dir
    assert any(shared.rglob("*")), "cargo did not use the shared build-dir; this cargo is too old for the test"
    survivors = sorted(path.relative_to(shared) for path in shared.rglob("*"))
    _age(first, 48)
    result = sweep(root, min_age_hours=24)
    assert str(first) in result["removed"], result
    assert sorted(path.relative_to(shared) for path in shared.rglob("*")) == survivors


@pytest.mark.integration
def test_an_orphan_that_a_live_cargo_builds_into_is_kept(root):
    """The worktree was moved, not deleted: a cargo still told to build into the old managed target."""
    target, workspace = managed_target(root, "moved", hours=0.01)
    moved = workspace.parent / "moved-away"
    workspace.rename(moved)
    build = _slow_build(crate(root.parent / "workspaces/runner", 25), target)
    try:
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
        assert result["blocked"] and result["blocked"][0]["reason"].startswith("cargo (pid "), result
    finally:
        _stop(build)
    assert sweep(root)["removed"] == [str(target)]  # nothing uses it any more: now it is an orphan to remove


# --- round 13: identity from the kernel, a bounded busy check, a markerless target that is still cleanable --

_HOLDS = {  # how the process in the other namespace holds the bound tree, and the path it then holds
    "target": ("target", "", None),
    "workspace": ("workspace", "", None),
    "cwd below the bind root": ("workspace", "src", None),
    "open descriptor": ("target", "", "exec sleep 120 < {inside}/CACHEDIR.TAG"),
    "mapped file": ("target", "", None),
    "executable": ("target", "", None),
}


def _in_another_mount_namespace(kind: str, target: Path, tmp_path: Path) -> tuple[subprocess.Popen, Path]:
    """A real process (``bwrap`` mount namespace, as a devcontainer or rootless container would be) that holds
    part of ``target``'s tree through a path that does not exist on this host. ``kind`` picks the way it holds
    it: cwd (at the bind root or below it), an open descriptor, a mapping whose descriptor is closed, or its
    own executable. Returns the process and the path it sees (what ``_scan`` reports)."""
    if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
        pytest.skip("needs Linux and bubblewrap")
    source_name, below, shell = _HOLDS[kind]
    source = target if source_name == "target" else target.parent
    view = tmp_path / "ns-view"
    view.mkdir()
    inside = view / "workspace"
    held = inside / below if below else inside
    command = ["sleep", "120"]
    chdir = held
    if shell:
        command, chdir, held = ["sh", "-c", shell.format(inside=inside)], Path("/"), inside / "CACHEDIR.TAG"
    elif kind == "mapped file":
        mapped = inside / "CACHEDIR.TAG"
        command = [sys.executable, "-c", f"import mmap, time; f = open({str(mapped)!r}, 'rb'); "
                   "m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ); f.close(); time.sleep(120)"]
        chdir, held = Path("/"), mapped
    elif kind == "executable":
        shutil.copy(shutil.which("sleep"), target / "debug" / "napper")
        command, chdir, held = [str(inside / "debug" / "napper"), "120"], Path("/"), inside / "debug" / "napper"
    proc = subprocess.Popen(
        ["bwrap", "--die-with-parent", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--tmpfs", str(view), "--bind", str(source), str(inside), "--chdir", str(chdir), *command],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
        _wait_for(lambda: proc.poll() is not None or any(hold[3] == held for hold in cleanup._scan()),
                  "the namespaced process to be seen")
        if proc.poll() is not None:
            pytest.skip(f"cannot create a mount namespace here: {proc.stderr.read().decode().strip()}")
    except BaseException:
        _stop(proc)
        raise
    assert not inside.exists(), "the namespaced path must not exist in this namespace"
    return proc, held


@pytest.mark.integration
@pytest.mark.parametrize("kind", list(_HOLDS))
def test_a_process_in_another_mount_namespace_keeps_the_target(root, tmp_path, kind):
    target = local_target(root)
    proc, held = _in_another_mount_namespace(kind, target, tmp_path)
    try:
        _age(target, 48)
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
        assert result["blocked"] and f"uses {held}" in result["blocked"][0]["reason"], result
    finally:
        _stop(proc)
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]


def test_a_busy_check_that_cannot_finish_in_time_fails_closed_and_frees_the_lock(root, monkeypatch):
    """The scan stats files of arbitrary processes, which a dead network mount can block: it runs in a child
    with a deadline. (A hung mount cannot be made without privileges; a deadline nothing can meet is real.)"""
    target = local_target(root)
    monkeypatch.setenv("HERDR_BUSY_CHECK_SECONDS", "0.001")
    started = time.monotonic()
    held = sweep(root)
    assert time.monotonic() - started < 30
    assert held["removed"] == [] and target.exists() and held["aborted"], held
    assert any("busy check timed out" in error for error in held["errors"]), held
    monkeypatch.delenv("HERDR_BUSY_CHECK_SECONDS")
    assert sweep(root)["removed"] == [str(target)]  # the lock was released, the next sweep is fine


def test_an_idle_markerless_managed_target_is_cleaned_and_its_marker_restored(root):
    target, workspace = managed_target(root, "mless")
    (target / TARGET_MARKER).unlink()  # what a killed ``cargo clean`` leaves, the owner record still there
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]
    assert (target / TARGET_MARKER).is_file() and (target / "CACHEDIR.TAG").is_file()
    assert prepare_target(root / "sccache", workspace) == target


# --- round 14: non-dumpable processes, the child's cwd and exclusions, the deadline, leases first ----------

_NON_DUMPABLE = "import ctypes, time; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); time.sleep(120)"


@pytest.mark.integration
def test_a_non_dumpable_process_under_herdr_does_not_abort_the_scan(root, tmp_path):
    """This user's non-dumpable processes (``ping``, file-capability execs) keep their ``/proc/<pid>`` owner
    but have a root-owned ``fd/`` that refuses listing. One that descends from a process named herdr and
    names nothing of the target on its command line must be skipped, not turn into a failed sweep."""
    if not sys.platform.startswith("linux"):
        pytest.skip("needs /proc")
    herdr = tmp_path / "herdr"
    shutil.copy(shutil.which("sh"), herdr)  # a real process named herdr, as the ancestry check looks for
    target = local_target(root)
    proc = subprocess.Popen([str(herdr), "-c", f"{sys.executable} -c '{_NON_DUMPABLE}'; true"],
                            start_new_session=True)
    try:
        _wait_for(lambda: _refuses_fd_listing(proc.pid), "the non-dumpable child to refuse fd/")
        cleanup._scan()  # must not raise
        _age(target, 48)
        result = sweep(root)
        assert not result["aborted"] and result["removed"] == [str(target)], result
    finally:
        _stop(proc)


def _refuses_fd_listing(parent: int) -> bool:
    """True once a child of ``parent`` exists whose ``fd`` directory this very user cannot list."""
    try:
        children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
    except OSError:
        return False
    for child in children:
        try:
            os.listdir(f"/proc/{child}/fd")
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def test_the_busy_check_child_does_not_import_from_the_sweepers_directory(root, tmp_path, monkeypatch):
    target = local_target(root)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "json.py").write_text("raise RuntimeError('imported from the sweeper cwd')\n")
    monkeypatch.chdir(shadow)
    assert cleanup._busy_check(target) is None


def test_the_sweeper_itself_never_counts_as_a_holder(root, tmp_path):
    """A sweep started from a project directory (``herdr-eng build-hygiene run``, the cargo wrapper) has its
    cwd in the workspace; that must not make the workspace busy."""
    target = local_target(root)
    code = ("import sys; from pathlib import Path; from herdr_engineering import build_cleanup as c; "
            "print(c._busy_check(Path(sys.argv[1])))")
    out = subprocess.run([sys.executable, "-c", code, str(target)], cwd=target.parent, capture_output=True,
                         text=True, check=True, env={**os.environ, "PYTHONPATH": str(Path(cleanup.__file__).parent.parent)})
    assert out.stdout.strip() == "None", out


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "abc"])
def test_a_nonsense_busy_check_deadline_falls_back_to_the_default(root, monkeypatch, value, caplog):
    target = local_target(root)
    monkeypatch.setenv("HERDR_BUSY_CHECK_SECONDS", value)
    result = sweep(root)
    assert not result["aborted"] and result["removed"] == [str(target)], result
    assert "HERDR_BUSY_CHECK_SECONDS" in caplog.text


_GRANDCHILD_HOOK = """
import os, subprocess, time
grandchild = subprocess.Popen(["sleep", "300"])
with open(os.environ["HERDR_TEST_GRANDCHILD_PIDFILE"], "w") as pidfile:
    pidfile.write(str(grandchild.pid))
time.sleep(600)  # a scan that never finishes
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    listing = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(listing.strip()) and not listing.strip().startswith("Z")


def test_a_timed_out_busy_check_kills_its_whole_process_group(root, tmp_path, monkeypatch):
    """The scan child starts a real grandchild (through a ``sitecustomize`` on its path, as ``lsof`` is one on
    macOS) and then hangs. After the deadline both must be gone: killing only the direct child leaves it.
    The ``sitecustomize`` is a test seam that makes the child hang at interpreter start, so ``_busy_child``
    itself never runs: it stands in for a slow ``lsof`` and is not a substitute for any production code path."""
    local_target(root)
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_GRANDCHILD_HOOK)
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(hook), os.environ.get("PYTHONPATH")])))
    monkeypatch.setenv("HERDR_TEST_GRANDCHILD_PIDFILE", str(pidfile))
    monkeypatch.setenv("HERDR_BUSY_CHECK_SECONDS", "4")
    result = sweep(root)
    assert result["aborted"] and any("timed out" in error for error in result["errors"]), result
    assert pidfile.exists(), "the hung scan never started its grandchild"
    grandchild = int(pidfile.read_text())
    try:
        _wait_for(lambda: not _alive(grandchild), "the grandchild to be gone", 10)
    finally:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.integration
def test_a_non_dumpable_process_running_a_binary_from_the_target_keeps_the_target(root, tmp_path):
    """Its cwd, exe and fds are unreadable, but its command line is not: a program path inside the target
    (or any absolute argument inside the target or workspace) still marks the target busy."""
    if not sys.platform.startswith("linux"):
        pytest.skip("needs /proc")
    target = local_target(root)
    napper = target / "debug" / "napper"
    shutil.copy(sys.executable, napper)  # a real binary placed in the target that makes itself non-dumpable
    env = {**os.environ, "PYTHONHOME": sys.base_prefix, "PYTHONPATH": ""}
    proc = subprocess.Popen([str(napper), "-S", "-c", _NON_DUMPABLE], env=env, start_new_session=True)
    try:
        _wait_for(lambda: _refuses_fd(proc.pid), "napper to refuse fd/")
        _age(target, 48)
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
        assert result["blocked"] and f"uses {napper}" in result["blocked"][0]["reason"], result
    finally:
        _stop(proc)
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]


def _refuses_fd(pid: int) -> bool:
    try:
        os.listdir(f"/proc/{pid}/fd")
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def test_a_clean_that_finds_no_ownership_record_keeps_the_target_and_says_why(root):
    """The marker and the owner record vanish after ``_cargo_clean`` decided the target is managed, while it
    waits for the manager lock (a ``cargo clean`` killed meanwhile, then the record lost): the identity to put
    back is unknown, so nothing is cleaned and no ``null`` marker is written."""
    target, _workspace = managed_target(root, "noowner")
    lock = _wait_blocked_on(target.parent / ".target-manager.lock")
    code = ("import sys; from pathlib import Path; from herdr_engineering import build_cleanup as c; "
            "print('ready', flush=True); print(c._cargo_clean(Path(sys.argv[1])))")
    child = subprocess.Popen([sys.executable, "-c", code, str(target)], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": str(Path(cleanup.__file__).parent.parent)})
    try:
        assert child.stdout.readline().strip() == "ready"
        _wait_for(lambda: _waits_for_lock(child.pid), "the child to wait for the manager lock")
        (target / TARGET_MARKER).unlink()
        cleanup.owner_record(target.parent, target.name).unlink()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
    out, _ = child.communicate(timeout=120)
    assert "ownership" in out, out
    assert not (target / TARGET_MARKER).exists() and (target / "debug").exists()


def _waits_for_lock(pid: int) -> bool:
    """True once ``pid`` is blocked in flock (Linux: ``/proc/locks`` lists a waiter as ``->``). Without
    ``/proc/locks`` (macOS) nothing real can be observed, so the caller is simply given time."""
    try:
        for line in Path("/proc/locks").read_text().splitlines():
            fields = line.split()
            if "->" in fields and str(pid) in fields[fields.index("->"):][:7]:
                return True
        return False
    except OSError:
        time.sleep(2)  # no /proc/locks (macOS): the best a real process offers
        return True


def test_cmdline_paths_read_every_value_short_option_and_list_entry():
    assert "/abs/t" in cleanup._cmdline_paths(["--define=k=/abs/t"])
    assert "/abs/t" in cleanup._cmdline_paths(["-L/abs/t"])
    assert {"/a", "/b"} <= cleanup._cmdline_paths(["--path=/a:/b"])
    assert cleanup._cmdline_paths(["-c", "1", "relative/x", "--n=3", "-v"]) == set()


@pytest.mark.integration
def test_a_non_dumpable_process_run_by_a_relative_path_keeps_the_target(root, tmp_path):
    """``./target/debug/server`` after a ``setcap``: the cwd is unreadable and no absolute path is on the
    command line, but the program's name is a regular file of the target."""
    if not sys.platform.startswith("linux"):
        pytest.skip("needs /proc")
    target = local_target(root)
    server = target / "debug" / "server"
    shutil.copy(sys.executable, server)
    env = {**os.environ, "PYTHONHOME": sys.base_prefix, "PYTHONPATH": ""}
    proc = subprocess.Popen([f"./{target.name}/debug/server", "-S", "-c", _NON_DUMPABLE], env=env,
                            cwd=target.parent, start_new_session=True)
    try:
        _wait_for(lambda: _refuses_fd(proc.pid), "server to refuse fd/")
        _age(target, 48)
        result = sweep(root)
        assert result["removed"] == [] and target.exists(), result
        assert result["blocked"] and f"is named like {server}" in result["blocked"][0]["reason"], result
    finally:
        _stop(proc)
    _age(target, 48)
    assert sweep(root)["removed"] == [str(target)]


@pytest.mark.integration
def test_the_documented_limit_a_non_dumpable_cwd_in_the_target_is_not_seen(root):
    """Pins the limit stated in the docs: cwd and open files of a non-dumpable process are unreadable, so one
    that sits in the target without naming it on its command line does not keep the target."""
    if not sys.platform.startswith("linux"):
        pytest.skip("needs /proc")
    target = local_target(root)
    proc = subprocess.Popen([sys.executable, "-c", _NON_DUMPABLE], cwd=target / "debug", start_new_session=True)
    try:
        _wait_for(lambda: _refuses_fd(proc.pid), "the process to refuse fd/")
        _age(target, 48)
        assert sweep(root)["removed"] == [str(target)]
    finally:
        _stop(proc)


def test_a_busy_check_whose_child_dies_without_an_answer_fails_closed(root, tmp_path, monkeypatch):
    """The child exits non-zero for real (a ``sitecustomize`` that exits), the non-timeout failure path."""
    local_target(root)
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text("import sys\nsys.exit(3)\n")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(hook), os.environ.get("PYTHONPATH")])))
    result = sweep(root)
    assert result["aborted"] and any("no answer" in error for error in result["errors"]), result
    assert result["removed"] == []


# Not tested: an ``fd/`` that cannot be stat'ed under a user-owned ``/proc/<pid>`` (it must still fail closed
# under herdr). The kernel gives ``/proc/<pid>`` mode 0555, so it cannot be produced for real; only the mode-0
# stand-in in ``test_an_unverifiable_process_fails_the_scan_closed`` reaches that branch.


def test_cmdline_paths_keep_a_path_with_a_colon():
    assert "/abs/a:b/t" in cleanup._cmdline_paths(["--dir=/abs/a:b/t"])
    assert "/abs/a:b/t" in cleanup._cmdline_paths(["/abs/a:b/t"])
    assert "/abs/a:b" in cleanup._cmdline_paths(["-L/abs/a:b"])


def test_cmdline_paths_of_an_argument_full_of_equals_signs_is_linear():
    started = time.monotonic()
    assert "/x" in cleanup._cmdline_paths(["=" * 100_000 + "/x"])
    assert time.monotonic() - started < 1, "splitting an argument of many ``=`` must not be quadratic"


def _proc_dir(base: Path, comm: str, argv: list[str]) -> Path:
    """A real directory shaped like ``/proc/<pid>``: the two world-readable files the fallback reads."""
    pid = base / "4242"
    pid.mkdir(parents=True)
    (pid / "comm").write_text(comm + "\n")
    (pid / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    return pid


def test_cmdline_holds_tell_the_comm_from_the_argv0_basename(tmp_path):
    held = lambda pid: {(how, path.name) for _n, _c, how, path, _i in cleanup._cmdline_holds(pid, 1) if how == "named"}  # noqa: E731
    assert held(_proc_dir(tmp_path / "a", "server", ["./target/x/runner"])) == {("named", "server"), ("named", "runner")}
    assert held(_proc_dir(tmp_path / "b", "server", ["/opt/bin/server"])) == {("named", "server")}  # absolute: comm only
    assert held(_proc_dir(tmp_path / "c", "ping", ["ping", "-c", "1"])) == {("named", "ping")}


def test_a_rewritten_process_title_is_not_a_name(tmp_path):
    pid = _proc_dir(tmp_path, "sshd-session: u", ["sshd-session: user@pts/3"])
    assert [h for h in cleanup._cmdline_holds(pid, 1) if h[2] == "named"] == []
