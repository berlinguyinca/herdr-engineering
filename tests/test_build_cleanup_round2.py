# ruff: noqa: F811
"""Round 2 review fixes of the size cap: the sweep alone enforces it (idle floor, full content guard), a survey
child that cannot hold the sweep's locks, a cache that is best effort and validated, unsearched homes.
Real files, real processes, real syscall failures; the only constants overridden are the module's own."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_maintenance as maintenance
from tests.test_build_cleanup_size_cap import (  # noqa: F401  (env is a fixture)
    HUGE,
    NO_PRESSURE,
    SMALL,
    env,
    project,
    sweep,
)

REPO = Path(__file__).resolve().parent.parent


def _old(path: Path, hours=48) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def _spec(root: Path, **kw) -> dict:
    return {"root": str(root), "cap": SMALL, "full": False, "cap_cutoff": None, "exclude": [], "targets": None, **kw}


# --- the idle floor of the cap ---------------------------------------------------------------

def test_an_over_cap_target_idle_for_three_hours_is_cleaned_by_default(env):
    root, state = env
    target = project(root, age=3)
    assert sweep(root, state, max_target_bytes=SMALL)["removed"] == [str(target)]


def test_the_idle_floor_is_configurable(env):
    root, state = env
    target = project(root, age=3)
    result = sweep(root, state, max_target_bytes=SMALL, size_cap_idle_hours=6)
    assert result["removed_count"] == 0 and (target / "debug").exists()


# --- M2: the content guard covers the whole target, not what the capped walk happened to see ----

def _poisoned(root: Path, marker: str) -> tuple[Path, int]:
    """An old target with a fine-looking size and ``marker`` 20 levels down, in a tree whose every level also
    holds a file: a walk that stops at its first bytes over the cap never gets that deep."""
    target = project(root)
    deep = target / "debug"
    for level in range(20):
        (deep / f"filler{level}.bin").write_bytes(b"x" * 200_000)
        deep = deep / f"d{level}"
        deep.mkdir()
    if marker == ".git":
        (deep / ".git").mkdir()
    elif marker == "Cargo.toml":
        (deep / "Cargo.toml").write_text("[package]\n")
    elif marker == "symlink":
        (deep / "link").symlink_to("/etc")
    elif marker == "fifo":
        os.mkfifo(deep / "pipe")
    for path in [*target.rglob("*"), target]:
        os.utime(path, (time.time() - 48 * 3600,) * 2, follow_symlinks=False)
    return target, 100_000


@pytest.mark.parametrize("marker", [".git", "Cargo.toml", "symlink", "fifo"])
def test_a_marker_past_the_capped_walk_still_stops_the_clean(env, marker):
    root, state = env
    target, cap = _poisoned(root, marker)
    cleanup._inspect(target, stop_at=cap)  # the capped walk of the first round does not see it
    with pytest.raises(OSError):
        cleanup._guard(target, time.monotonic() + 60)
    result = sweep(root, state, max_target_bytes=cap)
    assert result["removed_count"] == 0 and (target / "debug").exists(), result
    assert result["status"] == "warning"
    assert any(str(target) in warning for warning in result["warnings"])


def test_a_clean_target_passes_the_guard(env):
    root, _ = env
    target = project(root)
    cleanup._guard(target, time.monotonic() + 60)


def test_a_guard_that_cannot_finish_in_time_is_undecided(env):
    root, _ = env
    target = project(root)
    for index in range(1500):
        (target / "debug" / f"f{index}").write_bytes(b"")
    with pytest.raises(TimeoutError):
        cleanup._guard(target, time.monotonic() - 1)


def test_the_guard_ignores_what_vanishes_under_it(env):
    root, _ = env
    target = project(root)
    cleanup._guard(target, time.monotonic() + 60)
    gone = target / "debug" / "gone"
    gone.mkdir()
    gone.rmdir()
    cleanup._guard(target, time.monotonic() + 60)


# --- M1: the survey child holds nothing of the sweep -----------------------------------------

def test_a_child_inherits_no_descriptor_not_even_an_inheritable_lock(tmp_path):
    import fcntl
    locks = []
    try:
        for name in ("build-cleanup.lock", "build-maintenance.lock"):
            fd = os.open(tmp_path / name, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.set_inheritable(fd, True)
            locks.append(fd)
        code = ("import os, sys\nfor fd in sys.argv[1:]:\n"
                "    try:\n        os.fstat(int(fd)); print('open', fd)\n"
                "    except OSError:\n        print('closed', fd)\n")
        out = cleanup._run_child(code, [str(fd) for fd in locks], 30, "probe")
    finally:
        for fd in locks:
            os.close(fd)
    assert out.split() == [word for fd in locks for word in ("closed", str(fd))]


def test_a_hung_child_is_killed_with_its_group_and_leaves_no_zombie(tmp_path):
    code = ("import subprocess, sys, time\n"
            "grandchild = subprocess.Popen(['sleep', '120'])\n"
            "print(sys.argv[0] or 'x', flush=True)\nprint(grandchild.pid, flush=True)\ntime.sleep(120)\n")
    began = time.time()
    with pytest.raises(cleanup.ChildTimeout) as caught:
        cleanup._run_child(code, [], 1.5, "hang")
    assert time.time() - began < 20
    grandchild = int(caught.value.output.split()[-1])
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild of a timed-out child is still alive")
    assert cleanup._ABANDONED == []


def test_a_killed_child_that_exits_later_is_reaped_at_the_next_call():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.time() + 10
    while child.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    cleanup._ABANDONED.append(child)
    cleanup._reap()
    assert cleanup._ABANDONED == []


def test_both_locks_are_free_while_a_survey_runs_past_its_deadline(env, monkeypatch):
    """``run_once`` with the real survey child and a real deadline it cannot meet: the sweep gives up at the
    deadline, reports, and its locks are free."""
    import fcntl
    root, state = env
    monkeypatch.setenv("HERDR_SURVEY_SECONDS", "0.01")  # shorter than the child's own start
    project(root)
    result = sweep(root, state, max_target_bytes=SMALL)
    assert result["removed_count"] == 0
    assert any("skipped root" in warning and "timed out" in warning for warning in result["warnings"])
    for name in ("build-cleanup.lock", "build-maintenance.lock"):
        fd = os.open(state / name, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def _bulk_workspaces(root: Path, count: int, files: int) -> None:
    """``count`` real, old Cargo targets (manifest, fingerprints, ``files`` files each): what the survey's
    content guard has to walk entry by entry."""
    for index in range(count):
        workspace = root / f"w{index:02d}"
        deps = workspace / "target" / "debug" / "deps"
        deps.mkdir(parents=True)
        (workspace / "Cargo.toml").write_text('[package]\nname = "bulk"\nversion = "0.1.0"\nedition = "2021"\n')
        (workspace / "target" / ".rustc_info.json").write_text("{}")
        (workspace / "target" / "debug" / ".fingerprint").mkdir()
        for number in range(files):
            (deps / f"f{number}.o").write_bytes(b"x")
        for path in [*(workspace / "target").rglob("*"), workspace / "target"]:
            _old(path)


def test_a_survey_cut_off_after_discovery_keeps_the_rows_it_had(tmp_path):
    """The real survey child over many real targets, cut off by a real deadline (half of what the whole
    survey takes) after it listed them and measured some."""
    root = tmp_path / "root"
    _bulk_workspaces(root, 30, 2500)
    spec = _spec(root, cap_cutoff=time.time() + 3600)  # every target is idle: each gets the content guard
    began = time.monotonic()
    full = cleanup._run_survey(spec, root, {"warnings": []})
    took = time.monotonic() - began
    assert len(full["rows"]) == 30 and full["done"]
    os.environ["HERDR_SURVEY_SECONDS"] = f"{took / 2:.3f}"
    try:
        result = {"warnings": []}
        survey = cleanup._run_survey(spec, root, result)
    finally:
        del os.environ["HERDR_SURVEY_SECONDS"]
    assert 0 < len(survey["rows"]) < 30 and not survey["done"], (took, len(survey["rows"]))
    assert any(f"{len(survey['rows'])} of 30 targets" in warning for warning in result["warnings"])


def test_a_survey_cut_off_before_discovery_ended_raises(tmp_path, monkeypatch):
    project(tmp_path / "root")
    monkeypatch.setenv("HERDR_SURVEY_SECONDS", "0.01")
    with pytest.raises(TimeoutError):
        cleanup._run_survey(_spec(tmp_path / "root"), tmp_path / "root", {"warnings": []})


def test_what_a_child_wrote_just_before_the_deadline_is_kept():
    code = "import time\nfor n in range(5):\n    print(n, flush=True)\ntime.sleep(120)\n"
    with pytest.raises(cleanup.ChildTimeout) as caught:
        cleanup._run_child(code, [], 2, "writer")
    assert caught.value.output.split() == ["0", "1", "2", "3", "4"]


def test_the_partial_survey_output_is_parsed_up_to_the_cut():
    survey = cleanup._parse_survey('{"found": {"targets": []}}\n{"row": {"path": "/a"}}\n{"row": {"pa')
    assert survey["found"] == {"targets": []} and survey["rows"] == [{"path": "/a"}] and not survey["done"]


# --- H1: a disk too full for the state directory ----------------------------------------------

_FULL_DISK = """
import json, resource, signal, sys
from pathlib import Path
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (0, resource.RLIM_INFINITY))  # every write to a file fails
from herdr_engineering import build_cleanup as cleanup
root, state = Path(sys.argv[1]), Path(sys.argv[2])
result = cleanup.run_once([root], state, high_water=100, low_water=0.0, max_target_bytes=1)
print(json.dumps(result))
"""


def test_a_state_directory_that_cannot_be_written_does_not_stop_the_sweep(env):
    """A real failing write (``RLIMIT_FSIZE``: EFBIG, the same OSError as ENOSPC) in a child process."""
    root, state = env
    target = project(root)
    state.mkdir()
    for name in ("build-cleanup.lock", "build-maintenance.lock"):
        (state / name).touch()
    done = subprocess.run([sys.executable, "-c", _FULL_DISK, str(root), str(state)], capture_output=True,
                          text=True, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, check=False)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    # the limit is inherited by ``cargo clean``, which may or may not manage without writing: either way the
    # sweep got as far as cleaning this target
    assert result["removed"] == [str(target)] or [item["path"] for item in result["skipped"]] == [str(target)], result
    assert not result["aborted"] and not result["errors"]
    assert result["status"] == "warning"
    assert any("discovery cache" in warning for warning in result["warnings"])
    assert any("report" in warning for warning in result["warnings"])


# --- L3 / L4: the discovery cache --------------------------------------------------------------

def test_cache_only_without_a_cache_still_sweeps_the_explicit_roots(tmp_path, monkeypatch):
    real_data = Path.home() / ".local/share/herdr-engineering"  # ``cargo clean`` goes through the cargo host
    home = tmp_path / "home"
    (home / ".local/share").mkdir(parents=True)
    if real_data.exists():
        (home / ".local/share/herdr-engineering").symlink_to(real_data)
    for name in (".rustup", ".cargo"):  # the toolchain lives in HOME
        if (Path.home() / name).exists():
            (home / name).symlink_to(Path.home() / name)
    monkeypatch.setenv("HOME", str(home))
    project(home / "src", "app")
    other = tmp_path / "work"
    mine = project(other, "mine")
    state = tmp_path / "state"
    result = cleanup.run_once([home, other], state, max_target_bytes=SMALL, discovery="cache_only", **NO_PRESSURE)
    assert result["removed"] == [str(mine)], result["skipped"]
    assert any(item["path"] == str(home) and "not searched" in item["reason"] for item in result["skipped"])


def test_a_cached_target_that_no_longer_exists_is_dropped_silently(tmp_path):
    root = tmp_path / "root"
    kept = project(root, "kept")
    state = tmp_path / "state"
    sweep_args = {"discovery": "cached", "max_target_bytes": HUGE, **NO_PRESSURE}
    cleanup.run_once([root], state, **sweep_args)
    gone = project(root, "gone")
    cleanup.run_once([root], state, **{**sweep_args, "discovery": "walk"})
    cache = state / "build-cleanup-targets.json"
    at = json.loads(cache.read_text())[str(root)]["at"]
    subprocess.run(["rm", "-rf", str(gone.parent)], check=True)
    result = cleanup.run_once([root], state, **sweep_args)
    assert result["warnings"] == [] and result["status"] == "ok", result
    entry = json.loads(cache.read_text())[str(root)]
    assert entry["targets"] == [str(kept)] and entry["at"] == at


@pytest.mark.parametrize("entry", ["text", {"at": "x", "targets": []}, {"at": 1.0, "targets": 3},
                                   {"at": 1.0, "targets": ["relative"]}, {"at": True, "targets": []},
                                   {"targets": []}, [1]])
def test_a_malformed_cache_entry_is_ignored_with_a_warning(tmp_path, entry):
    root = tmp_path / "root"
    target = project(root)
    state = tmp_path / "state"
    state.mkdir()
    (state / "build-cleanup-targets.json").write_text(json.dumps({str(root): entry}))
    result = cleanup.run_once([root], state, discovery="cached", max_target_bytes=SMALL, **NO_PRESSURE)
    assert result["removed"] == [str(target)]  # walked afresh
    assert result["status"] == "warning"
    assert any("malformed discovery cache" in warning for warning in result["warnings"])


def test_an_unsearched_home_is_named_in_the_refusal(tmp_path, monkeypatch):
    from herdr_engineering import disk_guard
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state = tmp_path / "state"
    state.mkdir()
    disk_guard.relieve({"roots": [str(home)], "max_target_gb": 50, "min_age_hours": 24}, state, 1)
    assert "home directory not searched" in disk_guard.last_unsearched(state)


# --- L5 / L6: configuration --------------------------------------------------------------------

@pytest.mark.parametrize("bad", [False, None])
def test_false_and_null_are_the_default_with_one_warning_per_process(bad, caplog):
    maintenance._WARNED.clear()
    with caplog.at_level("WARNING"):
        for _ in range(3):
            kwargs = maintenance.policy_kwargs({"max_target_gb": bad})
    assert kwargs["max_target_bytes"] == 50 * 1024**3
    assert len([m for m in caplog.messages if "max_target_gb" in m]) == 1


def test_the_idle_floor_is_validated_with_a_minimum_of_one_hour(caplog):
    maintenance._WARNED.clear()
    assert maintenance.policy_kwargs({})["size_cap_idle_hours"] == 2.0
    assert maintenance.policy_kwargs({"size_cap_idle_hours": 6})["size_cap_idle_hours"] == 6.0
    with caplog.at_level("WARNING"):
        assert maintenance.policy_kwargs({"size_cap_idle_hours": 0})["size_cap_idle_hours"] == 2.0
        assert maintenance.policy_kwargs({"size_cap_idle_hours": "soon"})["size_cap_idle_hours"] == 2.0
    assert len([m for m in caplog.messages if "size_cap_idle_hours" in m]) == 2


@pytest.mark.parametrize("alias_first", [True, False])
def test_a_root_named_by_a_link_and_by_its_path_is_swept_once_by_the_real_path(tmp_path, alias_first):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    named = [str(alias), str(real)] if alias_first else [str(real), str(alias)]
    assert maintenance.effective_roots({"roots": named}, explicit=named) == [real]


# --- L1: what the idle time looks at -----------------------------------------------------------

def test_a_fresh_file_in_tmp_or_doc_of_a_huge_target_is_recent_activity(env):
    root, state = env
    target = project(root)
    (target / "tmp").mkdir()
    (target / "tmp" / "rustc-scratch").write_bytes(b"y")
    assert cleanup._shallow_newest(target) > time.time() - 60
    result = sweep(root, state, max_target_bytes=SMALL)
    assert result["removed_count"] == 0 and (target / "debug").exists()



# --- round 3: one slow busy check skips that target only ---------------------------------------

_SLOW_FOR_ONE_TARGET = """
import os, sys, time
if sys.argv[1:2] == [os.environ["HERDR_TEST_SLOW_TARGET"]]:
    time.sleep(600)  # this target's process scan never finishes (a stat on a dead mount under it)
"""


def test_a_busy_check_that_times_out_skips_that_target_and_the_sweep_goes_on(env, tmp_path, monkeypatch):
    """The first target's scan child hangs (a ``sitecustomize`` that only does so for that target's path: a
    stand-in for a dead mount under it, not for any production code). It is kept with a warning, never
    cleaned; the second, idle target is still judged and cleaned, and the sweep does not abort."""
    root, state = env
    slow = project(root, "slow", age=72)  # oldest: judged first
    fast = project(root, "fast", age=48)
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(_SLOW_FOR_ONE_TARGET)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(hook), os.environ.get("PYTHONPATH")])))
    monkeypatch.setenv("HERDR_TEST_SLOW_TARGET", str(slow))
    monkeypatch.setenv("HERDR_BUSY_CHECK_SECONDS", "4")
    result = sweep(root, state, max_target_bytes=SMALL)
    assert (slow / "debug").exists(), "a target whose busy check timed out must never be cleaned"
    assert result["removed"] == [str(fast)], result
    assert not result["aborted"], result
    assert any("timed out" in w and str(slow) in w for w in result["warnings"]), result["warnings"]
    assert any(s["path"] == str(slow) and "timed out" in s["reason"] for s in result["skipped"]), result


# --- round 3: work not done for a target that cannot be cleaned ------------------------------------

def test_a_cache_only_target_over_the_cap_is_skipped_before_any_scan(env):
    root, state = env
    target = project(root)
    (target.parent / "Cargo.toml").unlink()  # CACHEDIR.TAG and fingerprints, no manifest beside it
    result = sweep(root, state, max_target_bytes=SMALL)
    assert result["removed_count"] == 0 and target.exists()
    assert result["process_scans"] == 0, "a target that is never cleaned must not be scanned for"
    assert {"path": str(target), "reason": cleanup._NO_WORKSPACE} in result["skipped"]


def test_a_target_in_use_is_turned_away_by_the_scan_alone(env):
    root, state = env
    target = project(root)
    proc = subprocess.Popen(["sleep", "300"], cwd=target.parent / "src")
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not cleanup._busy_check(target, quick=True):
            time.sleep(0.2)
        result = sweep(root, state, max_target_bytes=SMALL)
    finally:
        proc.kill()
        proc.wait()
    assert result["removed_count"] == 0 and (target / "debug").exists()
    assert result["process_scans"] == 1 and "sleep" in result["blocked"][0]["reason"], result


def test_the_clean_time_guard_stops_a_target_that_changed_after_the_survey(env):
    """The survey saw a clean target; before the clean something foreign appeared deep inside. Without the
    guard of ``_clean_idle`` the real ``cargo clean`` would run."""
    root, state = env
    target = project(root)
    deep = target / "debug" / "deeper" / "still" / "further" / "down"
    deep.mkdir(parents=True)
    (deep / "link").symlink_to("/etc")
    result = {"skipped": [], "warnings": [], "errors": [], "blocked": [], "process_scans": 0, "dry_run": False,
              "would_remove": [], "reasons": {}}
    assert cleanup._clean_idle(target, str(target), time.time(), state, result, "size_cap") is False
    assert (target / "debug").exists()
    assert result["skipped"] and "content check failed" in result["skipped"][0]["reason"]
    assert result["process_scans"] == 1  # only the cheap pre-check ran; the guard stopped it before the full scan


def test_a_tiny_positive_cap_is_the_default_with_a_warning(caplog):
    maintenance._WARNED.clear()
    with caplog.at_level("WARNING"):
        assert maintenance.policy_kwargs({"max_target_gb": 1e-10})["max_target_bytes"] == 50 * 1024**3
    assert len([m for m in caplog.messages if "max_target_gb" in m]) == 1
    assert maintenance.policy_kwargs({"max_target_gb": 0})["max_target_bytes"] is None
