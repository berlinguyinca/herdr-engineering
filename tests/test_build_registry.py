"""Spec 05: durable build registry, leases and fencing. Real sqlite files, real processes."""
import json
import multiprocessing as mp
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_registry as br
from herdr_engineering.build_coord import run_gated


class Clock:
    """Controllable time source (a seam, not a mock of an external dependency)."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reg(tmp_path, clock):
    return br.Registry(tmp_path / "state", now=clock)


def new(reg, **kw):
    kw.setdefault("repo_id", "r1")
    kw.setdefault("input_digest", "d1")
    kw.setdefault("cmd_kind", "cargo build")
    return reg.register(**kw)


def drive(reg, aid, *states, lease=None):
    for s in states:
        reg.transition(aid, s, "test", lease=lease)


# --- file / schema -------------------------------------------------------------

def test_db_file_is_0600_wal_with_schema_version(tmp_path):
    r = br.Registry(tmp_path)
    assert stat.S_IMODE(r.path.stat().st_mode) == 0o600
    conn = sqlite3.connect(r.path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == br.SCHEMA_VERSION
    conn.close()


def test_newer_schema_refused(tmp_path):
    r = br.Registry(tmp_path)
    conn = sqlite3.connect(r.path)
    conn.execute("UPDATE schema_version SET version=99")
    conn.commit()
    conn.close()
    with pytest.raises(br.RegistryError):
        br.Registry(tmp_path)


# --- transition table ------------------------------------------------------------

def test_happy_path_persists_every_transition_with_reason(reg):
    a = new(reg)
    for s in ("classified", "admitted", "running", "publishing"):
        reg.transition(a.attempt_id, s, f"to_{s}")
    reg.transition(a.attempt_id, "completed", "done", exit_code=0)
    rows = reg.transitions(a.attempt_id)
    assert [r["to_state"] for r in rows] == [
        "requested", "classified", "admitted", "running", "publishing", "completed"]
    assert rows[-1]["reason"] == "done" and rows[-1]["from_state"] == "publishing"
    assert reg.get_build(a.build_id)["state"] == "completed"
    assert reg.get_attempt(a.attempt_id)["exit_code"] == 0


@pytest.mark.parametrize("bad", [
    ("requested", "running"), ("requested", "completed"), ("classified", "running"),
    ("admitted", "completed"), ("running", "admitted"), ("running", "running"),
    ("requested", "requested")])
def test_illegal_transitions_raise(reg, bad):
    frm, to = bad
    a = new(reg)
    path = ["classified", "admitted", "running"]
    if frm != "requested":
        drive(reg, a.attempt_id, *path[:path.index(frm) + 1] if frm in path else [frm])
    with pytest.raises(br.IllegalTransition):
        reg.transition(a.attempt_id, to, "x", exit_code=0)
    assert reg.get_attempt(a.attempt_id)["state"] == frm


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "timed_out"])
def test_terminal_states_are_immutable(reg, terminal):
    a = new(reg)
    drive(reg, a.attempt_id, "classified", "admitted", "running")
    reg.transition(a.attempt_id, terminal, "end", exit_code=1)
    for nxt in ("running", "completed", "failed", "requested"):
        with pytest.raises(br.IllegalTransition):
            reg.transition(a.attempt_id, nxt, "again", exit_code=0)


def test_rejected_is_terminal(reg):
    a = new(reg)
    reg.transition(a.attempt_id, "rejected", "policy")
    with pytest.raises(br.IllegalTransition):
        reg.transition(a.attempt_id, "classified", "x")


def test_completed_requires_exit_code_zero(reg):
    a = new(reg)
    drive(reg, a.attempt_id, "classified", "admitted", "running")
    with pytest.raises(br.IllegalTransition):
        reg.transition(a.attempt_id, "completed", "cache hit only")  # no exit code
    with pytest.raises(br.IllegalTransition):
        reg.transition(a.attempt_id, "completed", "bad", exit_code=2)
    assert reg.get_attempt(a.attempt_id)["state"] == "running"
    reg.transition(a.attempt_id, "completed", "ok", exit_code=0)


# --- idempotent register ---------------------------------------------------------

def test_same_key_joins_active_build_with_new_attempt(reg):
    a = new(reg)
    b = new(reg)
    assert not a.joined and b.joined
    assert b.build_id == a.build_id and b.attempt_id != a.attempt_id
    assert b.attempt_no == 2 and a.request_key == b.request_key


def test_join_expires_after_ttl_and_after_terminal(reg, clock):
    a = new(reg)
    clock.t += br.JOIN_TTL_S + 1
    b = new(reg)
    assert not b.joined and b.build_id != a.build_id
    drive(reg, b.attempt_id, "classified", "admitted", "running")
    reg.transition(b.attempt_id, "failed", "x", exit_code=1)
    c = new(reg)
    assert not c.joined and c.build_id != b.build_id


def test_heartbeat_keeps_build_joinable(reg, clock):
    a = new(reg)
    drive(reg, a.attempt_id, "classified", "admitted")
    lease = reg.acquire_lease("/tmp/ws-join", a.attempt_id, 30)
    clock.t += br.JOIN_TTL_S - 10
    reg.heartbeat(lease)
    clock.t += br.JOIN_TTL_S - 10
    assert new(reg).joined


def test_dirty_tree_without_digest_is_excluded_from_dedup(reg):
    a = new(reg, input_digest=None)
    b = new(reg, input_digest=None)
    assert a.dedup_excluded and b.dedup_excluded
    assert not b.joined and a.build_id != b.build_id and a.request_key != b.request_key


def test_trust_domains_never_dedup(reg):
    a = new(reg, trust_domain="uid:1")
    b = new(reg, trust_domain="uid:2")
    assert a.request_key != b.request_key and not b.joined


@pytest.mark.parametrize("field,value", [
    ("repo_id", "r2"), ("input_digest", "d2"), ("toolchain_digest", "t2"),
    ("profile", "release"), ("features_digest", "f2"), ("cmd_kind", "cargo test")])
def test_every_key_field_separates_builds(reg, field, value):
    a = new(reg)
    b = new(reg, **{field: value})
    assert a.request_key != b.request_key and not b.joined


def test_statements_are_parameterized(reg):
    evil = "x'); DROP TABLE builds; --"
    a = new(reg, repo_id=evil, owner_session=evil)
    assert reg.get_build(a.build_id)["repo_id"] == evil


# --- leases ----------------------------------------------------------------------

def test_scope_is_normalized(reg, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    link = tmp_path / "link"
    link.symlink_to(ws)
    a = new(reg)
    lease = reg.acquire_lease(str(link) + "/", a.attempt_id, 30)
    assert lease.scope == os.path.realpath(ws)
    assert reg.get_lease(ws)["epoch"] == 1
    assert br.normalize_scope("named-scope") == "named-scope"


def test_epoch_strictly_increases_over_20_acquisitions(reg, clock):
    seen = []
    for _ in range(20):
        a = new(reg, input_digest=None)
        lease = reg.acquire_lease("/tmp/ws-epoch", a.attempt_id, 10)
        seen.append(lease.epoch)
        reg.release(lease)
    assert seen == list(range(1, 21))


def test_second_holder_rejected_while_valid(reg):
    a, b = new(reg, input_digest=None), new(reg, input_digest=None)
    reg.acquire_lease("/tmp/ws-held", a.attempt_id, 30)
    with pytest.raises(br.LeaseHeld):
        reg.acquire_lease("/tmp/ws-held", b.attempt_id, 30)


def _race(state, aid, barrier, q):
    r = br.Registry(state)
    barrier.wait()
    try:
        q.put(("won", r.acquire_lease("/tmp/ws-race", aid, 60).epoch))
    except br.LeaseHeld:
        q.put(("lost", None))


def test_processes_racing_acquire_exactly_one_wins(tmp_path):
    state = tmp_path / "state"
    r = br.Registry(state)
    n = 6
    ids = [r.register(repo_id="r", input_digest=None, cmd_kind="x").attempt_id for _ in range(n)]
    ctx = mp.get_context("fork")
    barrier, q = ctx.Barrier(n), ctx.Queue()
    procs = [ctx.Process(target=_race, args=(state, i, barrier, q)) for i in ids]
    for p in procs:
        p.start()
    results = [q.get(timeout=30) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    assert sorted(x[0] for x in results) == ["lost"] * (n - 1) + ["won"]
    assert [x[1] for x in results if x[0] == "won"] == [1]


def test_takeover_after_expiry_bumps_epoch_and_fences_old_holder(reg, clock):
    a, b = new(reg, input_digest=None), new(reg, input_digest=None)
    drive(reg, a.attempt_id, "classified", "admitted")
    old = reg.acquire_lease("/tmp/ws-take", a.attempt_id, 10)
    clock.t += 11
    new_lease = reg.acquire_lease("/tmp/ws-take", b.attempt_id, 10)
    assert new_lease.epoch == old.epoch + 1
    with pytest.raises(br.FencedError):
        reg.transition(a.attempt_id, "running", "late", lease=old)
    with pytest.raises(br.FencedError):
        reg.heartbeat(old)
    with pytest.raises(br.FencedError):
        reg.finish(a.attempt_id, "failed", "late", exit_code=1, lease=old)
    with pytest.raises(br.FencedError):
        reg.release(old)
    assert reg.get_attempt(a.attempt_id)["state"] == "admitted"
    assert reg.get_lease("/tmp/ws-take")["owner_attempt_id"] == b.attempt_id


def test_leased_attempt_requires_its_lease_to_mutate(reg):
    a = new(reg)
    drive(reg, a.attempt_id, "classified", "admitted")
    reg.acquire_lease("/tmp/ws-req", a.attempt_id, 30)
    with pytest.raises(br.FencedError):
        reg.transition(a.attempt_id, "running", "no lease presented")


def test_heartbeat_extends_expiry(reg, clock, tmp_path):
    a = new(reg)
    lease = reg.acquire_lease("/tmp/ws-hb", a.attempt_id, 10)
    clock.t += 8
    reg.heartbeat(lease)
    clock.t += 8  # 16s after acquire: would be expired without the heartbeat
    assert br.active_scopes(reg.path.parent, now=clock.t) == ["/tmp/ws-hb"]
    clock.t += 5
    assert br.active_scopes(reg.path.parent, now=clock.t) == []


def test_finish_releases_lease_and_scope_reusable(reg):
    a = new(reg)
    drive(reg, a.attempt_id, "classified", "admitted")
    lease = reg.acquire_lease("/tmp/ws-fin", a.attempt_id, 30)
    reg.transition(a.attempt_id, "running", "go", lease=lease)
    reg.finish(a.attempt_id, "completed", "ok", exit_code=0, lease=lease)
    assert reg.get_lease("/tmp/ws-fin")["released_ts"] is not None
    b = new(reg, input_digest=None)
    assert reg.acquire_lease("/tmp/ws-fin", b.attempt_id, 30).epoch == 2


# --- reconcile -------------------------------------------------------------------

def _leased_child(reg, clock, scope):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="x", pid=child.pid)
    drive(reg, a.attempt_id, "classified", "admitted")
    lease = reg.acquire_lease(scope, a.attempt_id, 10)
    reg.transition(a.attempt_id, "running", "go", lease=lease)
    return child, a, lease


def test_reconcile_marks_dead_owner_but_not_live_one(reg, clock):
    dead, da, _ = _leased_child(reg, clock, "/tmp/ws-dead")
    live, la, _ = _leased_child(reg, clock, "/tmp/ws-live")
    try:
        dead.kill()
        dead.wait()
        assert reg.reconcile() == []  # not expired yet: nothing happens
        clock.t += 11
        assert reg.reconcile() == [da.attempt_id]
        att = reg.get_attempt(da.attempt_id)
        assert att["state"] == "failed"
        assert reg.transitions(da.attempt_id)[-1]["reason"] == "owner_vanished"
        assert reg.get_lease("/tmp/ws-dead")["released_ts"] is not None
        assert reg.get_attempt(la.attempt_id)["state"] == "running"
        assert reg.get_lease("/tmp/ws-live")["released_ts"] is None
    finally:
        live.kill()
        live.wait()


def test_unreadable_proc_means_alive(reg, clock, tmp_path):
    dead, da, _ = _leased_child(reg, clock, "/tmp/ws-noproc")
    dead.kill()
    dead.wait()
    clock.t += 11
    assert reg.reconcile(proc_root=tmp_path / "no-such-proc") == []
    assert reg.get_attempt(da.attempt_id)["state"] == "running"
    assert br.pid_alive(os.getpid(), proc_root=tmp_path / "nope") is True
    assert br.pid_alive(None) is True


def test_reused_pid_with_other_starttime_is_dead(reg, clock):
    assert br.pid_alive(os.getpid(), br.proc_starttime(os.getpid()))
    assert not br.pid_alive(os.getpid(), br.proc_starttime(os.getpid()) + 1)


# --- cancel flag -----------------------------------------------------------------

def test_request_cancel_only_sets_flag(reg):
    a = new(reg)
    assert not reg.cancel_requested(a.build_id)
    assert reg.request_cancel(a.build_id, "operator")
    assert reg.cancel_requested(a.build_id)
    assert reg.get_build(a.build_id)["state"] == "requested"
    assert not reg.request_cancel("no-such-build", "x")


# --- bounds ----------------------------------------------------------------------

def test_prune_old_terminal_rows_keeps_active(tmp_path, clock):
    r = br.Registry(tmp_path, now=clock)
    old = new(r, input_digest=None)
    r.transition(old.attempt_id, "rejected", "old")
    active = new(r, input_digest=None)
    clock.t += br.RETENTION_S + 10
    r = br.Registry(tmp_path, now=clock)
    assert r.get_build(old.build_id) is None and r.transitions(old.attempt_id) == []
    assert r.get_build(active.build_id) is not None


def test_row_cap_prunes_oldest_terminal(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(br, "MAX_BUILDS", 5)
    r = br.Registry(tmp_path, now=clock)
    ids = []
    for _ in range(8):
        clock.t += 1
        a = new(r, input_digest=None)
        r.transition(a.attempt_id, "rejected", "x")
        ids.append(a.build_id)
    br.Registry(tmp_path, now=clock)
    left = [i for i in ids if r.get_build(i)]
    assert left == ids[-5:]


# --- build_coord record path (end to end, real processes) ---------------------------

REPO = Path(__file__).resolve().parents[1]


def _events(state):
    return [json.loads(x) for x in (state / "builds.jsonl").read_text().splitlines()]


def _rec(cmd, state, **kw):
    return run_gated(cmd, state_dir=state, record=True, slots=2, max_load=0, **kw)


def test_recorded_build_completes_and_releases_lease(tmp_path):
    assert _rec(["sh", "-c", "exit 0"], tmp_path) == 0
    started, fin = _events(tmp_path)
    r = br.Registry(tmp_path)
    for e in (started, fin):
        assert e["attempt_id"] and len(e["request_key"]) == 64 and e["joined"] is False
    assert started["build_id"] == fin["build_id"]
    assert "registry_unavailable" not in fin["unavailable"]
    build = r.get_build(fin["build_id"])
    assert build["state"] == "completed" and build["request_key"] == fin["request_key"]
    att = r.get_attempt(fin["attempt_id"])
    assert att["state"] == "completed" and att["exit_code"] == 0
    assert [t["to_state"] for t in r.transitions(fin["attempt_id"])] == [
        "requested", "classified", "admitted", "running", "completed"]
    assert r.get_lease(os.getcwd())["released_ts"] is not None
    assert br.active_scopes(tmp_path) == []


def test_failed_build_exit_code_and_state(tmp_path):
    assert _rec(["sh", "-c", "exit 7"], tmp_path) == 7
    fin = _events(tmp_path)[-1]
    r = br.Registry(tmp_path)
    assert r.get_build(fin["build_id"])["state"] == "failed"
    assert r.get_attempt(fin["attempt_id"])["exit_code"] == 7


def test_missing_binary_is_127_and_attempt_failed(tmp_path):
    assert _rec(["definitely-not-a-binary-xyz"], tmp_path) == 127
    r = br.Registry(tmp_path)
    (row,) = r._all("SELECT * FROM attempts", ())
    assert row["state"] == "failed"
    assert br.active_scopes(tmp_path) == []


def _wrapper(tmp_path, cmd, preamble=""):
    script = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); "
        "from herdr_engineering import build_registry as br; " + preamble +
        "from herdr_engineering.build_coord import run_gated; "
        f"sys.exit(run_gated({cmd!r}, slots=2, max_load=0, record=True, "
        f"state_dir={str(tmp_path)!r}))")
    return subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path)


def _wait_running(tmp_path):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            rows = br.Registry(tmp_path)._all(
                "SELECT * FROM attempts WHERE state='running'", ())
            if rows:
                return rows[0]
        except br.RegistryError:
            pass
        time.sleep(0.05)
    raise AssertionError("attempt never reached running")


def test_sigterm_ends_cancelled_and_releases_lease(tmp_path):
    proc = _wrapper(tmp_path, ["sleep", "30"])
    try:
        att = _wait_running(tmp_path)
        assert br.active_scopes(tmp_path) == [os.path.realpath(tmp_path)]
        time.sleep(0.3)  # signal handlers are installed right after the fork
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 128 + signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
    r = br.Registry(tmp_path)
    assert r.get_attempt(att["attempt_id"])["state"] == "cancelled"
    assert r.get_build(att["build_id"])["state"] == "cancelled"
    assert br.active_scopes(tmp_path) == []


def test_request_cancel_makes_owner_terminate_its_own_child(tmp_path):
    proc = _wrapper(tmp_path, ["sleep", "30"], preamble="br.DEFAULT_LEASE_TTL_S = 0.3; ")
    try:
        att = _wait_running(tmp_path)
        assert br.Registry(tmp_path).request_cancel(att["build_id"], "operator")
        assert proc.wait(timeout=20) == 128 + signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
    r = br.Registry(tmp_path)
    assert r.get_attempt(att["attempt_id"])["state"] == "cancelled"
    assert r.transitions(att["attempt_id"])[-1]["reason"] == "cancel_requested"


def test_heartbeat_thread_extends_lease_while_child_runs(tmp_path):
    proc = _wrapper(tmp_path, ["sleep", "30"], preamble="br.DEFAULT_LEASE_TTL_S = 0.6; ")
    try:
        att = _wait_running(tmp_path)
        r = br.Registry(tmp_path)
        first = r.get_lease(tmp_path)["heartbeat_ts"]
        time.sleep(1.5)  # > ttl: only heartbeats keep it valid
        later = r.get_lease(tmp_path)
        assert later["heartbeat_ts"] > first
        assert br.active_scopes(tmp_path) == [os.path.realpath(tmp_path)]
        assert later["owner_attempt_id"] == att["attempt_id"]
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("breakage", ["garbage", "directory", "readonly"])
def test_registry_failure_never_fails_the_build(tmp_path, breakage):
    db = tmp_path / br.DB_NAME
    if breakage == "garbage":
        db.write_bytes(b"this is not a sqlite database" * 100)
    elif breakage == "directory":
        db.mkdir()
    else:
        br.Registry(tmp_path)
        db.chmod(0o400)
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    assert _rec(["sh", "-c", "exit 5"], tmp_path) == 5
    assert _rec(["sh", "-c", "exit 0"], tmp_path) == 0
    fin = _events(tmp_path)[-1]
    assert "registry_unavailable" in fin["unavailable"]
    assert fin["exit_code"] == 0


def test_non_recorded_path_creates_no_registry(tmp_path):
    script = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); "
        "from herdr_engineering.build_coord import run_gated; "
        f"sys.exit(run_gated(['true'], slots=2, max_load=0, state_dir={str(tmp_path)!r}))")
    assert subprocess.run([sys.executable, "-c", script]).returncode == 0
    assert not (tmp_path / br.DB_NAME).exists()


# --- review fixes: M1 child pid, M2 lease required, M3 pid-reuse safe signals ----------

def test_build_run_records_child_pid_so_reconcile_spares_live_build(tmp_path):
    state = tmp_path / "state"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        run = br.BuildRun.open(state, cwd=str(tmp_path), cmd_kind="cargo build", host="h",
                               cmd=["cargo", "build"], ttl_s=0.2)
        run.on_spawn(child.pid)
        row = run.registry.get_attempt(run.reg.attempt_id)
        assert row["pid"] == child.pid
        assert row["pid_starttime"] == br.proc_starttime(child.pid)
        run._stop.set()  # the wrapper "dies": heartbeats stop, lease expires
        time.sleep(0.5)
        assert run.registry.reconcile() == []  # child alive => not owner_vanished
        assert run.registry.get_attempt(run.reg.attempt_id)["state"] == "running"
    finally:
        child.kill()
        child.wait()


def test_finish_without_lease_after_takeover_is_fenced(reg, clock):
    a, b = new(reg, input_digest=None), new(reg, input_digest=None)
    drive(reg, a.attempt_id, "classified", "admitted")
    drive(reg, b.attempt_id, "classified", "admitted")
    la = reg.acquire_lease("/tmp/ws-fence", a.attempt_id, 5)
    reg.transition(a.attempt_id, "running", "go", lease=la)
    clock.t += 6
    reg.acquire_lease("/tmp/ws-fence", b.attempt_id, 5)  # takeover, epoch 2
    with pytest.raises(br.FencedError):
        reg.finish(a.attempt_id, "completed", "exit_0", exit_code=0)
    with pytest.raises(br.FencedError):
        reg.transition(a.attempt_id, "publishing", "x")
    assert reg.get_attempt(a.attempt_id)["state"] == "running"


def test_halt_stops_heartbeat_and_cancel_signals(tmp_path):
    state = tmp_path / "state"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        run = br.BuildRun.open(state, cwd=str(tmp_path), cmd_kind="cargo build", host="h",
                               cmd=["cargo", "build"], ttl_s=0.2)
        run.on_spawn(child.pid)
        run.halt()  # called before the child is reaped
        run.registry.request_cancel(run.build_id, "late")
        time.sleep(0.5)
        assert child.poll() is None  # no signal after halt
        assert run._thread is None or not run._thread.is_alive()
    finally:
        child.kill()
        child.wait()
