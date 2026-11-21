# ruff: noqa: F811
"""Round 1 review fixes of the size cap: huge targets, a pass that really cleans (invoking shell, cargo wrapper),
signals, rebuild churn, bounded and cached discovery, git and status-file semantics, config validation.
Real cargo builds, real files and processes; the only constants overridden are the module's own limits."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_maintenance as maintenance
from tests.test_build_cleanup_size_cap import (  # noqa: F401  (env is a fixture)
    HUGE,
    SMALL,
    _coordinate,
    _crate,
    env,
    project,
    sweep,
)

REPO = Path(__file__).resolve().parent.parent
TINY_GB = 0.000001


def _old(path: Path, hours=48) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def _fill(target: Path, count: int) -> Path:
    """``count`` small files in ``deps``: far more entries than a lowered limit, old like the rest."""
    deps = target / "debug/deps"
    deps.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (deps / f"f{index}.o").write_bytes(b"x")
        _old(deps / f"f{index}.o")
    _old(deps)
    return deps


# --- H1: a target past the entry limit is still capped, and an undecidable one is reported -------

def test_a_target_past_the_entry_limit_is_still_capped(env, monkeypatch):
    root, state = env
    target = project(root)
    _fill(target, 300)
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 150)
    with pytest.raises(OSError, match="limit"):
        cleanup._inspect(target)
    result = sweep(root, state, max_target_bytes=8192)
    assert result["removed"] == [str(target)], result
    assert result["reasons"][str(target)] == "size_cap"


def test_an_undecidable_target_is_a_warning_never_a_silent_ok(env, monkeypatch):
    root, state = env
    target = project(root)
    _fill(target, 300)
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 150)
    result = sweep(root, state, max_target_bytes=HUGE)
    assert result["status"] == "warning", result
    assert any(str(target) in warning and "limit" in warning for warning in result["warnings"])
    assert (target / "debug").exists()


def test_a_fresh_file_in_a_huge_target_is_recent_activity(env, monkeypatch):
    root, state = env
    target = project(root)
    deps = _fill(target, 300)
    (deps / "just-built.o").write_bytes(b"y")  # now: creating it also moved the mtime of deps
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 150)
    result = sweep(root, state, max_target_bytes=8192)
    assert result["removed_count"] == 0 and (target / "debug").exists()
    assert {"path": str(target), "reason": "recent activity"} in result["skipped"]


# --- H2: the pass must really clean ----------------------------------------------------------

def _through_sh(tmp_path, workspace, target, cap_gb, preamble=""):
    """``build-coordinate`` started by a shell that stays alive with its cwd in the workspace."""
    from tests.real_cargo import cargo
    config = tmp_path / "config.yaml"
    config.write_text(f"build_cleanup:\n  max_target_gb: {cap_gb}\nfabric_build_cache:\n  enabled: false\n")
    env_ = {**os.environ, "HERDR_ENGINEERING_CONFIG": str(config),
            "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
            "PATH": f"{cargo().parent}{os.pathsep}{os.environ['PATH']}",
            "HERDR_DISK_GUARD_OVERRIDE": "1", "PYTHONPATH": str(REPO)}
    env_.pop("RUSTC_WRAPPER", None)
    script = (f'cd "$1"; {preamble} "{sys.executable}" -m herdr_engineering.cli build-coordinate -- '
              f'cargo build --offline --target-dir "$2"; code=$?; true; exit $code')
    return subprocess.run(["/bin/sh", "-c", script, "sh", str(workspace), str(target)], env=env_,
                          capture_output=True, text=True, check=False)


def test_the_invoking_shell_with_its_cwd_in_the_workspace_does_not_block_the_pass(tmp_path):
    workspace = _crate(tmp_path)
    done = _through_sh(tmp_path, workspace, workspace / "target", TINY_GB)
    assert done.returncode == 0, done.stderr
    assert not (workspace / "target/debug").exists(), done.stderr


def test_an_ancestor_with_a_file_open_in_the_target_still_blocks_the_pass(tmp_path):
    workspace = _crate(tmp_path)
    target = workspace / "target"
    assert _coordinate(tmp_path, workspace, target, cap_gb=50).returncode == 0
    held = next((target / "debug/.fingerprint").rglob("invoked.timestamp"))
    done = _through_sh(tmp_path, workspace, target, TINY_GB, preamble=f'exec 3< "{held}";')
    assert done.returncode == 0, done.stderr
    assert (target / "debug").exists()
    assert "pass blocked" in done.stderr, done.stderr  # the reason is visible at WARNING


def test_a_process_that_is_not_an_ancestor_still_blocks_by_cwd(tmp_path):
    from herdr_engineering.build_cleanup import after_build_size_cap
    workspace = _crate(tmp_path)
    target = workspace / "target"
    assert _coordinate(tmp_path, workspace, target, cap_gb=50).returncode == 0
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=workspace)
    try:
        outcome = after_build_size_cap(["cargo", "build", "--target-dir", str(target)], workspace,
                                       {"max_target_gb": TINY_GB}, tmp_path / "state")
    finally:
        holder.kill()
        holder.wait()
    assert outcome["removed"] == [] and (target / "debug").exists()


def test_ancestors_are_the_parent_chain(tmp_path):
    chain = cleanup._ancestors()
    assert os.getpid() in chain and os.getppid() in chain


def _wrapper_env(tmp_path, cap_gb, cache):
    from herdr_engineering.cargo_host import install
    from tests.real_cargo import cargo
    home = tmp_path / "home"
    install(home, cargo(), Path(sys.executable))
    config = tmp_path / "config.yaml"
    cache_cfg = ("fabric_build_cache:\n  enabled: true\n  local:\n    capacity_gib: 1\n"
                 "    min_free_disk_percent: 0\n" if cache else "fabric_build_cache:\n  enabled: false\n")
    config.write_text(f"build_cleanup:\n  max_target_gb: {cap_gb}\n{cache_cfg}")
    env_ = {**os.environ, "HOME": str(home), "CARGO_HOME": str(home / ".cargo"),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
            "HERDR_ENGINEERING_CONFIG": str(config), "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
            "HERDR_DISK_GUARD_OVERRIDE": "1", "PYTHONPATH": str(REPO),
            "PATH": os.pathsep.join([str(home / ".local/bin"), str(cargo().parent), os.environ["PATH"]])}
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_TARGET_DIR", "HERDR_CARGO_HOST_COORDINATED"):
        env_.pop(key, None)
    return home, env_


@pytest.mark.parametrize("cache", [False, True])
def test_the_cargo_wrapper_cleans_an_oversized_target_after_the_build(tmp_path, cache):
    import shutil
    if cache and not shutil.which("sccache"):
        pytest.skip("sccache is required for the cached wrapper path")
    home, env_ = _wrapper_env(tmp_path, TINY_GB, cache)
    workspace = _crate(tmp_path)
    target = workspace / "target"
    done = subprocess.run([str(home / ".local/bin/cargo"), "build", "--offline", "--target-dir", str(target)],
                          cwd=workspace, env=env_, capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stderr
    assert not (target / "debug").exists(), done.stderr
    assert (workspace / "Cargo.toml").exists()


def test_the_cargo_wrapper_keeps_the_exit_code_and_does_not_clean_after_a_failed_build(tmp_path):
    home, env_ = _wrapper_env(tmp_path, TINY_GB, False)
    workspace = _crate(tmp_path)
    target = workspace / "target"
    assert _coordinate(tmp_path, workspace, target, cap_gb=50).returncode == 0
    (workspace / "src/main.rs").write_text("fn main() { not rust }\n")
    done = subprocess.run([str(home / ".local/bin/cargo"), "build", "--offline", "--target-dir", str(target)],
                          cwd=workspace, env=env_, capture_output=True, text=True, timeout=300, check=False)
    assert done.returncode == 101, done.stderr
    assert (target / "debug").exists()


# --- M1 / L2: signals -----------------------------------------------------------------------

_COUNTER = ("import os, signal, sys, time\n"
            "log = sys.argv[1]\n"
            "def note(n, f):\n"
            "    open(log, 'a').write('x')\n"
            "signal.signal(signal.SIGINT, note)\n"
            "signal.signal(signal.SIGTERM, note)\n"
            "open(log + '.up', 'w').write('1')\n"
            "end = time.time() + 2.5\n"
            "while time.time() < end:\n"
            "    time.sleep(0.05)\n")
_PARENT = ("import sys\n"
           "from herdr_engineering.build_coord import _run_child\n"
           "sys.exit(_run_child([sys.executable, '-c', sys.argv[2], sys.argv[1]], None))\n")


def _start_parent(tmp_path):
    log = tmp_path / "deliveries"
    proc = subprocess.Popen([sys.executable, "-c", _PARENT, str(log), _COUNTER], cwd=REPO, env={
        **os.environ, "PYTHONPATH": str(REPO)}, start_new_session=True)
    deadline = time.time() + 30
    while not Path(f"{log}.up").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert Path(f"{log}.up").exists()
    return proc, log


def test_one_ctrl_c_to_the_process_group_reaches_the_child_once(tmp_path):
    proc, log = _start_parent(tmp_path)
    os.killpg(proc.pid, signal.SIGINT)  # what a terminal does: the whole foreground group
    proc.wait(timeout=30)
    assert log.read_text() == "x"
    assert proc.returncode == 0  # the child handled it and ended normally


def test_sigterm_to_the_parent_is_forwarded_once_and_the_parent_dies_by_it(tmp_path):
    log = tmp_path / "deliveries"
    victim = ("import os, signal, sys, time\n"
              "open(sys.argv[1] + '.up', 'w').write('1')\n"
              "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", _PARENT, str(log), victim], cwd=REPO,
                            env={**os.environ, "PYTHONPATH": str(REPO)})
    deadline = time.time() + 30
    while not Path(f"{log}.up").exists() and time.time() < deadline:
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)
    assert proc.returncode == -signal.SIGTERM  # a death by signal, as the exec model gave


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux only")
def test_a_killed_parent_does_not_leave_the_build_running(tmp_path):
    log = tmp_path / "deliveries"
    victim = ("import os, sys, time\n"
              "open(sys.argv[1] + '.up', 'w').write(str(os.getpid()))\n"
              "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", _PARENT, str(log), victim], cwd=REPO,
                            env={**os.environ, "PYTHONPATH": str(REPO)})
    deadline = time.time() + 30
    while not Path(f"{log}.up").exists() and time.time() < deadline:
        time.sleep(0.05)
    child = int(Path(f"{log}.up").read_text())
    proc.kill()
    proc.wait()
    for _ in range(100):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(child, signal.SIGKILL)
    pytest.fail("the build outlived its killed parent")


# --- M2: no churn ---------------------------------------------------------------------------

CAP_CFG = {"max_target_gb": TINY_GB}


def _after(cmd_target, workspace, state, code=0):
    return cleanup.finish_build(["cargo", "build", "--target-dir", str(cmd_target)], workspace, CAP_CFG, state, code)


def test_a_failed_or_signalled_build_is_never_followed_by_a_clean(env):
    root, state = env
    mine = project(root, "mine", age=0)
    for code in (101, 130, 143):
        _after(mine, mine.parent, state, code)
        assert (mine / "debug").exists()


def test_a_target_is_cleaned_at_most_once_a_day_and_the_repeat_is_warned_about(env, caplog):
    from tests.real_cargo import build
    root, state = env
    mine = project(root, "mine", age=0)
    _after(mine, mine.parent, state)
    assert not (mine / "debug").exists()
    build(mine.parent, mine)  # the next build, as big as before
    _after(mine, mine.parent, state)
    assert (mine / "debug").exists()  # inside the 24 h window: left alone
    record = state / "build-cleanup-after-build.json"
    data = json.loads(record.read_text())
    data[str(mine)] = [time.time() - 25 * 3600]
    record.write_text(json.dumps(data))
    with caplog.at_level("WARNING"):
        _after(mine, mine.parent, state)
    assert not (mine / "debug").exists()
    assert any("max_target_gb" in message for message in caplog.messages), caplog.messages


# --- M3 / M4: bounded and cached discovery ---------------------------------------------------

def test_a_blocked_child_is_killed_at_the_deadline(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    began = time.time()
    with pytest.raises(TimeoutError):
        cleanup._in_child(lambda: os.close(os.open(fifo, os.O_RDONLY)), 1.0)  # blocks: no writer, ever
    assert time.time() - began < 10


def test_the_discovery_limit_keeps_what_was_found(tmp_path, monkeypatch):
    target = project(tmp_path, "p")
    for index in range(10):
        (target.parent / f"d{index}").mkdir()
    monkeypatch.setattr(cleanup, "_MAX_ENTRIES", 3)
    notes: list[str] = []
    found = cleanup._discover(target.parent, notes=notes)
    assert found == [target]
    assert notes and "limit" in notes[0]


def test_home_discovery_skips_library_and_other_roots_inside_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    mine = project(home / "src", "a")
    library = project(home / "Library", "x")
    nested = project(home / "work", "b")
    found = cleanup._discover(home, exclude=frozenset({home / "work"}))
    assert mine in found and library not in found and nested not in found


def _home_with_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home, project(home / "src", "app")


def test_cached_discovery_does_not_walk_again_until_the_refresh_interval(tmp_path, monkeypatch):
    home = tmp_path / "root"
    home.mkdir()
    first = project(home / "src", "app")
    state = tmp_path / "state"
    cleanup.run_once([home], state, max_target_bytes=HUGE, discovery="cached", **{"high_water": 100, "low_water": 0.0})
    cache = state / "build-cleanup-targets.json"
    assert str(first) in cache.read_text()
    second = project(home / "src", "newer")
    result = cleanup.run_once([home], state, max_target_bytes=SMALL, discovery="cached",
                              high_water=100, low_water=0.0)
    assert result["removed"] == [str(first)] and (second / "debug").exists()  # the new one is not known yet
    data = json.loads(cache.read_text())
    for entry in data.values():
        entry["at"] = time.time() - 7 * 3600
    cache.write_text(json.dumps(data))
    result = cleanup.run_once([home], state, max_target_bytes=SMALL, discovery="cached",
                              high_water=100, low_water=0.0)
    assert result["removed"] == [str(second)]


def test_the_disk_guard_relief_never_walks_the_home_directory(tmp_path, monkeypatch):
    from herdr_engineering.disk_guard import relieve
    home, target = _home_with_project(tmp_path, monkeypatch)
    state = tmp_path / "state"
    cfg = {"roots": [str(home)], "max_target_gb": 50, "min_age_hours": 24}
    relieve(cfg, state, 1)
    assert (target / "debug").exists()  # no cache yet: the home root is not walked before a build
    assert not (state / "build-cleanup-targets.json").exists()


def test_default_roots_cover_work_and_ideaprojects_without_duplicates(tmp_path, monkeypatch):
    from herdr_engineering.config import DEFAULTS
    home = tmp_path / "home"
    for name in ("work", "IdeaProjects", ".cache"):
        (home / name).mkdir(parents=True)
    (home / "alias").symlink_to(home / "work")
    monkeypatch.setenv("HOME", str(home))
    assert DEFAULTS["build_cleanup"]["roots"] == ["~", "~/work", "~/IdeaProjects", "~/.cache"]
    roots = maintenance.effective_roots({"roots": ["~", "~/work", "~/alias", "~/IdeaProjects", "~/.cache"]})
    assert roots[:4] == [home, home / "work", home / "IdeaProjects", home / ".cache"]
    assert home / "alias" not in roots


# --- L1: git ---------------------------------------------------------------------------------

def test_a_project_outside_git_inside_a_home_work_tree_is_not_cleaned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)  # dotfiles kept as ``git init ~``
    monkeypatch.setenv("HOME", str(home))
    target = project(home / "src", "app")
    subprocess.run(["rm", "-rf", str(target.parent / ".git")], check=True)
    assert not cleanup._untracked(target)


def test_inherited_git_variables_do_not_hide_tracked_output(env, monkeypatch, tmp_path):
    root, _ = env
    target = project(root)
    marker = next(target.rglob("invoked.timestamp"))
    subprocess.run(["git", "-C", str(target.parent), "add", "-f", str(marker)], check=True)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert not cleanup._untracked(target)


# --- L3: status files ------------------------------------------------------------------------

def test_a_one_target_pass_leaves_the_sweep_report_alone(env):
    root, state = env
    mine = project(root, "mine", age=0)
    state.mkdir()
    report = state / "build-cleanup.json"
    report.write_text(json.dumps({"timestamp": "2020-01-01T00:00:00+00:00", "status": "ok"}))
    outcome = cleanup.after_build_size_cap(["cargo", "build", "--target-dir", str(mine)], mine.parent,
                                           CAP_CFG, state)
    assert outcome["removed"] == [str(mine)]
    assert json.loads(report.read_text())["timestamp"] == "2020-01-01T00:00:00+00:00"
    assert json.loads((state / "build-cleanup-last-build.json").read_text())["removed"] == [str(mine)]


# --- L4: configuration -----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "50GB", float("nan"), float("inf"), -1, True, [1]])
def test_a_bad_max_target_gb_falls_back_to_the_default_with_a_warning(bad, caplog):
    with caplog.at_level("WARNING"):
        kwargs = maintenance.policy_kwargs({"max_target_gb": bad})
    assert kwargs["max_target_bytes"] == 50 * 1024**3
    assert any("max_target_gb" in message for message in caplog.messages)


def test_a_valid_max_target_gb_is_taken_as_is():
    assert maintenance.policy_kwargs({"max_target_gb": 0})["max_target_bytes"] is None
    assert maintenance.policy_kwargs({"max_target_gb": 1.5})["max_target_bytes"] == int(1.5 * 1024**3)


def test_a_numeric_string_max_target_gb_is_accepted():
    """YAML reads ``1e-06`` as a string; a string that is a finite number >= 0 is a number all the same."""
    assert maintenance.policy_kwargs({"max_target_gb": "1e-06"})["max_target_bytes"] == int(1e-06 * 1024**3)
    assert maintenance.policy_kwargs({"max_target_gb": "0"})["max_target_bytes"] is None
