"""Spec 05: cleanup honors registry leases. Real sqlite registry, real child owner.

``_process_paths`` is neutralised (like in test_build_cleanup.py) so only the lease
guard decides; thresholds are forced through the real policy parameters.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_registry as br

FORCE = {"high_water": 1, "low_water": 0, "min_age_hours": 24}


@pytest.fixture(autouse=True)
def no_proc_scan(monkeypatch):
    monkeypatch.setattr(cleanup, "_process_paths", lambda: [])


def project(root: Path, name="project", age=48) -> Path:
    p = root / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
    t = p / "target"
    t.mkdir()
    (t / ".rustc_info.json").write_text("{}")
    (t / "artifact").write_bytes(b"x" * 4096)
    stamp = time.time() - age * 3600
    for path in [*t.iterdir(), t]:
        os.utime(path, (stamp, stamp))
    return t


def sweep(root, state, **kw):
    return cleanup.run_once([root], state, **{**FORCE, **kw})


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


def test_lease_taken_between_scan_and_delete_is_rechecked(env, monkeypatch):
    """The 2nd ``_process_paths`` call happens after the first lease check and before
    rmtree (the existing revalidation step). Acquire a lease there, from the same
    seam the legacy tests already patch, and expect the target to survive."""
    root, state = env
    t = project(root)
    reg = br.Registry(state)
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="x")
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    calls = []

    def scan():
        calls.append(1)
        if len(calls) == 2:
            reg.acquire_lease(root / "project", a.attempt_id, 300)
        return []

    monkeypatch.setattr(cleanup, "_process_paths", scan)
    res = sweep(root, state)
    assert len(calls) == 2, "revalidation seam not reached"
    assert t.exists()
    assert reasons(res) == {"project": "active_lease"}


def test_target_no_longer_recognized_is_never_removed(env, monkeypatch):
    root, state = env
    t = project(root)
    calls = []

    def scan():
        calls.append(1)
        if len(calls) == 2:  # between scan and delete the project is replaced
            (root / "project" / "Cargo.toml").unlink()
        return []

    monkeypatch.setattr(cleanup, "_process_paths", scan)
    res = sweep(root, state)
    assert t.exists() and res["removed"] == []
