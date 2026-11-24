"""Follow-up 1: Registry.reconcile runs from build start and from the cleanup sweep. Real sqlite, real processes."""
import subprocess
import sys
import time
from pathlib import Path

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_registry as br


def _owner(state, scope, ttl=0.05):
    """A leased attempt owned by a child process; returns (child, attempt_id)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    reg = br.Registry(state)
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="x", pid=child.pid)
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    lease = reg.acquire_lease(scope, a.attempt_id, ttl)
    reg.transition(a.attempt_id, "running", "t", lease=lease)
    return child, a.attempt_id


def test_build_start_reconciles_expired_dead_owner(tmp_path):
    state = tmp_path / "state"
    child, aid = _owner(state, "/tmp/ws-stale")
    child.kill()
    child.wait()
    time.sleep(0.15)
    br.BuildRun.open(state, cwd=str(tmp_path), cmd_kind="cargo build", host="h", cmd=["cargo", "build"])
    reg = br.Registry(state)
    assert reg.get_attempt(aid)["state"] == "failed"
    assert reg.get_lease("/tmp/ws-stale")["released_ts"] is not None


def test_build_start_leaves_live_owner_alone(tmp_path):
    state = tmp_path / "state"
    child, aid = _owner(state, "/tmp/ws-live")
    try:
        time.sleep(0.15)
        br.BuildRun.open(state, cwd=str(tmp_path), cmd_kind="cargo build", host="h", cmd=["cargo", "build"])
        reg = br.Registry(state)
        assert reg.get_attempt(aid)["state"] == "running"
        assert reg.get_lease("/tmp/ws-live")["released_ts"] is None
    finally:
        child.kill()
        child.wait()


def test_cleanup_sweep_reconciles_and_reports_without_removing_anything(tmp_path):
    state = tmp_path / "state"
    root = tmp_path / "projects"
    root.mkdir()
    child, aid = _owner(state, "/tmp/ws-sweep")
    child.kill()
    child.wait()
    time.sleep(0.15)
    res = cleanup.run_once([root], state, dry_run=True)
    assert res["reconciled"] == [aid]
    assert res["removed_count"] == 0
    assert br.Registry(state).get_attempt(aid)["state"] == "failed"


def test_cleanup_sweep_does_not_create_a_registry(tmp_path):
    state = tmp_path / "state"
    root = tmp_path / "projects"
    root.mkdir()
    res = cleanup.run_once([root], state, dry_run=True)
    assert res["reconciled"] == []
    assert not br.db_path(state).exists()
