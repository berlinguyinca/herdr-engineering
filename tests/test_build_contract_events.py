"""Follow-up 2: the four contract events are written to contract-events.jsonl where the facts occur. Real files/sqlite/processes
(a labelled shell ``sccache`` only feeds the stats parser, as in test_build_metrics.py)."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_metrics as bm
from herdr_engineering import build_registry as br


def events(state: Path, name: str) -> list[dict]:
    p = state / "contract-events.jsonl"
    if not p.exists():
        return []
    return [e for e in map(json.loads, p.read_text().splitlines()) if e.get("event") == name]


def test_cache_stats_updated_follows_a_build_with_stats(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "sccache"
    fake.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        n=$(cat {tmp_path}/n 2>/dev/null || echo 0); n=$((n+1)); echo $n > {tmp_path}/n
        if [ $n -eq 1 ]; then echo '{{"stats":{{"compile_requests":10}}}}'; else echo '{{"stats":{{"compile_requests":15}}}}'; fi
    """))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    state = tmp_path / "state"
    bm.run_measured(["sh", "-c", "exit 0"], state_dir=state, cache={"applied": True})
    [ev] = events(state, "cache_stats_updated")
    assert ev["sccache_stats"] == {"compile_requests": 5}
    assert ev["cache_applied"] is True and ev["build_id"] and ev["host"] and ev["ts"]


def test_no_cache_stats_event_without_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    if bm.shutil.which("sccache"):
        return
    state = tmp_path / "state"
    bm.run_measured(["sh", "-c", "exit 0"], state_dir=state)
    assert events(state, "cache_stats_updated") == []


def test_storage_degraded_when_shared_tier_unavailable(tmp_path):
    state = tmp_path / "state"
    shared = {"active": False, "reason": "shared_unavailable:endpoint_down", "namespace_digest": None}
    bm.run_measured(["sh", "-c", "exit 0"], state_dir=state, cache={"applied": True, "shared": shared})
    [ev] = events(state, "storage_degraded")
    assert ev["tier"] == "shared" and ev["reason"] == "shared_unavailable:endpoint_down" and ev["build_id"]


def test_no_storage_degraded_when_shared_not_requested(tmp_path):
    state = tmp_path / "state"
    shared = {"active": False, "reason": "not_requested", "namespace_digest": None}
    bm.run_measured(["sh", "-c", "exit 0"], state_dir=state, cache={"applied": True, "shared": shared})
    assert events(state, "storage_degraded") == []


def test_lease_changed_on_acquire_release_and_reconcile(tmp_path):
    state = tmp_path / "state"
    reg = br.Registry(state)
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="x")
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    lease = reg.acquire_lease("/tmp/ws-ev", a.attempt_id, 60)
    reg.release(lease)
    changes = [e["change"] for e in events(state, "lease_changed")]
    assert changes == ["acquired", "released"]
    ev = events(state, "lease_changed")[0]
    assert ev["attempt_id"] == a.attempt_id and ev["epoch"] == 1
    assert "/tmp" not in json.dumps(ev)  # scope is an opaque id, never a path


def test_lease_changed_on_reconcile(tmp_path):
    state = tmp_path / "state"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    reg = br.Registry(state)
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="x", pid=child.pid)
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    reg.acquire_lease("/tmp/ws-ev2", a.attempt_id, 0.05)
    child.kill()
    child.wait()
    time.sleep(0.15)
    assert reg.reconcile() == [a.attempt_id]
    last = events(state, "lease_changed")[-1]
    assert last["change"] == "reconciled" and last["attempt_id"] == a.attempt_id


def test_cleanup_completed_after_a_real_sweep_not_a_dry_run(tmp_path):
    state, root = tmp_path / "state", tmp_path / "projects"
    root.mkdir()
    cleanup.run_once([root], state, dry_run=True)
    assert events(state, "cleanup_completed") == []
    cleanup.run_once([root], state)
    [ev] = events(state, "cleanup_completed")
    assert ev["kind"] == "cleanup" and ev["status"] == "ok" and ev["removed_count"] == 0 and ev["removed_bytes"] == 0


def test_cleanup_completed_after_an_applied_eviction(tmp_path):
    from tests.test_build_cache_policy import fill, tiny
    from herdr_engineering import build_cache_policy as pol
    st = tmp_path / "st"
    fill(st)
    out = pol.run_eviction(state_dir=st, apply=True, **tiny())
    assert out["status"] == "applied"
    [ev] = events(st, "cleanup_completed")
    assert ev["kind"] == "eviction" and ev["removed_count"] == 4 and ev["removed_bytes"] == 4000
