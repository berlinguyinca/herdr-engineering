"""Spec 05: cleanup honors registry leases. Real sqlite registry, real child owner.

Thresholds are forced through the real policy parameters; targets are built by a real cargo.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_registry as br
from tests.real_cargo import build


def force(root: Path) -> dict:
    """Thresholds relative to what the filesystem under ``root`` really shows, so a nearly empty tmpfs
    still triggers a sweep (``high_water=1`` would assume at least 1% used)."""
    usage = cleanup._usage(root)
    if usage <= 0:
        pytest.skip("the filesystem reports no usage at all")
    return {"high_water": usage / 2, "low_water": 0.0, "min_age_hours": 24}


def project(root: Path, name="project", age=48) -> Path:
    p = root / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (p / "src").mkdir()
    (p / "src/main.rs").write_text("fn main() {}\n")
    t = build(p, p / "target")  # a real build: cargo writes the tag and the fingerprints
    stamp = time.time() - age * 3600
    for path in [*t.rglob("*"), t]:
        os.utime(path, (stamp, stamp))
    return t


def sweep(root, state, **kw):
    return cleanup.run_once([root], state, **{**force(root), **kw})


def reasons(res):
    return {Path(s["path"]).parent.name: s["reason"] for s in res["skipped"]}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    state = tmp_path / "state"
    return root, state


def hold_lease(state, scope, ttl=300):
    """A real, separate owner process with a registered attempt and a valid lease."""
    reg = br.Registry(state)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="cargo build", pid=owner.pid)
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    lease = reg.acquire_lease(scope, a.attempt_id, ttl)
    return owner, reg, a, lease


def test_active_lease_preserves_target_in_dry_run_and_real_run(env):
    root, state = env
    t = project(root)
    owner, reg, a, lease = hold_lease(state, root / "project")
    try:
        for dry in (True, False):
            res = sweep(root, state, dry_run=dry)
            assert t.exists()
            assert reasons(res) == {"project": "active_lease"}
            assert res["would_remove"] == [] and res["removed"] == []
    finally:
        owner.kill()
        owner.wait()


@pytest.mark.parametrize("sub", ["", "crates/inner", ".."])
def test_scope_prefix_relation_works_both_ways(env, sub):
    root, state = env
    t = project(root)
    scope = (root / "project" / sub).resolve() if sub != ".." else root.resolve()
    if sub == "crates/inner":
        scope.mkdir(parents=True)
    owner, *_ = hold_lease(state, scope)
    try:
        assert reasons(sweep(root, state)) == {"project": "active_lease"}
        assert t.exists()
    finally:
        owner.kill()
        owner.wait()


def test_unrelated_lease_does_not_protect(env):
    root, state = env
    t = project(root)
    other = root / "other"
    other.mkdir()
    owner, *_ = hold_lease(state, other)
    try:
        res = sweep(root, state, dry_run=True)
        assert res["would_remove"] == [str(t)] and t.exists()
    finally:
        owner.kill()
        owner.wait()


def test_released_lease_allows_real_deletion_only_in_tmp(env):
    root, state = env
    t = project(root)
    owner, reg, a, lease = hold_lease(state, root / "project")
    try:
        reg.transition(a.attempt_id, "running", "t", lease=lease)
        reg.finish(a.attempt_id, "completed", "t", exit_code=0, lease=lease)
    finally:
        owner.kill()
        owner.wait()
    res = sweep(root, state)
    assert res["removed"] == [str(t)] and not t.exists()
    assert (root / "project" / "Cargo.toml").exists()  # source is never touched


def test_expired_lease_allows_deletion(env):
    root, state = env
    t = project(root)
    reg = br.Registry(state, now=lambda: time.time() - 1000)  # lease written long ago
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="x")
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    reg.acquire_lease(root / "project", a.attempt_id, 10)
    res = sweep(root, state)
    assert res["removed"] == [str(t)] and not t.exists()


@pytest.mark.parametrize("damage", ["garbage", "directory", "empty"])
def test_unreadable_registry_fails_closed(env, damage):
    root, state = env
    t = project(root)
    state.mkdir()
    db = state / br.DB_NAME
    if damage == "garbage":
        db.write_bytes(b"not sqlite at all" * 200)
    elif damage == "directory":
        db.mkdir()
    else:
        db.write_bytes(b"")
    for dry in (True, False):
        res = sweep(root, state, dry_run=dry)
        assert t.exists()
        assert reasons(res) == {"project": "registry_unreadable"}
        assert res["status"] == "warning" and res["errors"] == []
        assert res["warnings"]


def test_no_registry_file_is_legacy_behavior(env):
    root, state = env
    t = project(root)
    res = sweep(root, state)
    assert res["status"] == "ok" and res["removed"] == [str(t)]
    assert not (state / br.DB_NAME).exists()  # cleanup never creates the registry


_RACING_CHILD = """
import json, sys
import herdr_engineering.build_cleanup as c
root, state, high = sys.argv[1:4]
print(json.dumps(c.run_once([root], state, high_water=float(high), low_water=0.0, min_age_hours=24)))
"""

def _busy_check_running(sweeper: subprocess.Popen) -> bool:
    """True while the sweep's own child (the process scan of the busy check) exists: a real process that
    runs between the sweep's first lease check and its re-read of the lease, before it removes anything."""
    out = subprocess.run(["ps", "-ww", "-o", "pid=,ppid=,args=", "-A"], capture_output=True, text=True).stdout
    return any(f" {sweeper.pid} " in f" {line.split(None, 2)[1]} " and "_busy_child" in line
               for line in out.splitlines() if len(line.split(None, 2)) == 3)


def sweep_while_racing(root, state, tmp_path, race):
    """Run a real sweep in another process and run ``race()`` while that sweep is between its first
    checks and its removal. The sync point is real: the sweep starts a child process to scan the host
    for busy targets (after its lease check, before it removes anything), and we act once that child
    exists. No fake on PATH, no fixed sleeps."""
    import json
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    child = subprocess.Popen([sys.executable, "-c", _RACING_CHILD, str(root), str(state),
                              str(force(root)["high_water"])], env=env, stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 300
        while not _busy_check_running(child):
            assert child.poll() is None and time.monotonic() < deadline, "sweep never reached its busy check"
            time.sleep(0.002)
        race()
    except BaseException:
        child.kill()
        raise
    out, _ = child.communicate(timeout=300)
    return json.loads(out.splitlines()[-1])


def test_lease_taken_between_scan_and_delete_is_rechecked(env, tmp_path):
    """A real build registers its lease while a real sweep (another process) is between its first lease
    check and the removal: the target must survive."""
    root, state = env
    t = project(root)

    def race():
        reg = br.Registry(state)
        a = reg.register(repo_id="r", input_digest=None, cmd_kind="x")
        reg.transition(a.attempt_id, "classified", "t")
        reg.transition(a.attempt_id, "admitted", "t")
        reg.acquire_lease(root / "project", a.attempt_id, 300)

    res = sweep_while_racing(root, state, tmp_path, race)
    assert t.exists()
    assert reasons(res) == {"project": "active_lease"}


def test_a_workspace_that_vanishes_during_the_scan_is_never_cleaned(env, tmp_path):
    root, state = env
    t = project(root)
    res = sweep_while_racing(root, state, tmp_path, lambda: (root / "project" / "Cargo.toml").unlink())
    assert t.exists() and res["removed"] == []


def test_a_leased_target_is_skipped_without_a_scan_and_does_not_abort_the_sweep(env, monkeypatch):
    root, state = env
    t = project(root)
    owner, _reg, _a, _lease = hold_lease(state, root / "project")
    monkeypatch.setenv("HERDR_BUSY_CHECK_SECONDS", "0.001")  # any scan would time out and abort
    try:
        res = sweep(root, state)
    finally:
        owner.kill()
        owner.wait()
    assert t.exists() and reasons(res) == {"project": "active_lease"}, res
    assert not res["aborted"] and res["process_scans"] == 0, res
