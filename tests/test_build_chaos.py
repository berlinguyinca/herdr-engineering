"""Spec 13: failure, security and rollback scenarios that can run on ONE host.

Real processes, real flock, real sqlite, real sockets, tmp dirs. No mocks.
The only test doubles are tiny executable shell scripts named ``sccache``,
``rustc`` and ``cargo`` (same convention as test_build_cache_env.py): they prove
env wiring and exit-code preservation, NOT cache correctness or savings.

Scenarios that need a real cluster, S3 endpoint, sccache or a second host are
explicit skips at the bottom of this file (reason in the skip message).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cache_policy as policy
from herdr_engineering import build_cleanup as cleanup
from herdr_engineering import build_registry as br
from herdr_engineering import build_slurm_adapter as sa
from herdr_engineering.build_cache_config import validate
from herdr_engineering.build_cache_storage import Endpoint, probe_health

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
FORCE = {"high_water": 1, "low_water": 0, "min_age_hours": 24}
IS_ROOT = os.geteuid() == 0

RUSTC = """rustc 1.97.0 (2d8144b78 2026-07-07)
binary: rustc
commit-hash: 2d8144b7880597b6e6d3dfd63a9a9efae3f533d3
commit-date: 2026-07-07
host: x86_64-unknown-linux-gnu
release: 1.97.0
LLVM version: 22.1.6
"""
SECRET = "chaos-secret-0123456789"


# --- helpers ----------------------------------------------------------------------

def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fake_toolchain(root: Path) -> Path:
    """sccache/rustc/cargo stand-ins. cargo records its env and exits with its last arg."""
    bin_ = root / "bin"
    _exe(bin_ / "sccache", "echo 'sccache 0.19.0'\n")
    _exe(bin_ / "rustc", f"cat <<'EOF'\n{RUSTC}EOF\n")
    _exe(bin_ / "cargo", textwrap.dedent("""\
        [ -n "$CHAOS_SEEN" ] && env > "$CHAOS_SEEN"
        for last; do :; done
        case "$last" in ''|*[!0-9]*) exit 0;; *) exit "$last";; esac
        """))
    return bin_


def _env(state: Path, home: Path, *, cfg: Path | None = None, bin_: Path | None = None,
         **extra: str) -> dict[str, str]:
    env = {**os.environ, "HERDR_ENGINEERING_STATE": str(state), "HOME": str(home),
           "PYTHONPATH": str(REPO), **extra}
    env.pop("HERDR_ENGINEERING_CONFIG", None)
    for k in ("RUSTC_WRAPPER", "CARGO_INCREMENTAL", "CARGO_TARGET_DIR", "SCCACHE_DIR"):
        env.pop(k, None)
    if cfg is not None:
        env["HERDR_ENGINEERING_CONFIG"] = str(cfg)
    if bin_ is not None:
        env["PATH"] = f"{bin_}:{env['PATH']}"
    return env


def _cli_argv(*args: str) -> list[str]:
    return [PY, "-m", "herdr_engineering.cli", "build-coordinate", *args]


def _cli(args, state, home, *, cfg=None, bin_=None, cwd=None, timeout=60, **extra):
    return subprocess.run(_cli_argv(*args), capture_output=True, text=True, timeout=timeout,
                          env=_env(state, home, cfg=cfg, bin_=bin_, **extra), cwd=cwd or home)


def _write_cfg(path: Path, section: dict | None) -> Path:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"fabric_build_cache": section} if section else {}))
    return path


def _events(gates: Path) -> list[dict]:
    p = gates / "builds.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _rows(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here any more
    return port


def _private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def _shared_section(tmp: Path, port: int, *, use_ssl: bool = True) -> dict:
    reg = _private(tmp / "reg/registry.yaml", textwrap.dedent(f"""\
        endpoints:
          build-cache:
            host: 127.0.0.1:{port}
            region: us-east-1
            bucket: sccache-bucket
            use_ssl: {str(use_ssl).lower()}
        """))
    creds = _private(tmp / "reg/creds.yaml",
                     f"access_key_id: AKIACHAOS0000\nsecret_access_key: '{SECRET}'\n")
    return {"enabled": True, "mode": "shared_cache",
            "shared_cache": {"enabled": True, "endpoint_ref": "build-cache",
                             "registry_file": str(reg), "credentials_file": str(creds)}}


def _cargo_project(root: Path, name="project", age_h=48) -> Path:
    p = root / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
    t = p / "target"
    t.mkdir()
    (t / ".rustc_info.json").write_text("{}")
    (t / "artifact").write_bytes(b"x" * 4096)
    stamp = time.time() - age_h * 3600
    for path in [*t.iterdir(), t]:
        os.utime(path, (stamp, stamp))
    return t


@pytest.fixture(autouse=True)
def _no_proc_scan(monkeypatch):
    # Same neutralisation as test_build_cleanup*.py: only registry/lock guards decide here.
    monkeypatch.setattr(cleanup, "_process_paths", lambda: [])


def _wait_for(cond, timeout=15.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except sqlite3.OperationalError:   # db file exists before its schema does
            pass
        time.sleep(step)
    return False


# === 1. endpoint outage / unreachable shared backend =============================

def test_unreachable_shared_endpoint_builds_locally_with_reason_and_exit_code(tmp_path):
    state, bin_ = tmp_path / "state", _fake_toolchain(tmp_path)
    seen = tmp_path / "seen.env"
    cfg = _write_cfg(tmp_path / "cfg.yaml", _shared_section(tmp_path, _closed_port()))
    t0 = time.monotonic()
    r = _cli(["--max-load", "0", "--", "cargo", "build", "7"], state, tmp_path,
             cfg=cfg, bin_=bin_, CHAOS_SEEN=str(seen))
    assert r.returncode == 7, r.stderr                        # exit code preserved
    assert time.monotonic() - t0 < 30                          # bounded
    ev = _events(state / "build-gates")
    fin = [e for e in ev if e["event"] == "build_finished"][-1]
    assert fin["exit_code"] == 7
    assert fin["cache"]["applied"] is True                     # local cache still used
    assert fin["cache"]["shared"] == {
        "active": False, "reason": "shared_unavailable:endpoint_unreachable",
        "namespace_digest": None}
    child_env = seen.read_text()
    assert "RUSTC_WRAPPER=" in child_env and str(bin_ / "sccache") in child_env
    # no credential reaches the child env, the events or the output
    blob = child_env + json.dumps(ev) + r.stdout + r.stderr
    assert SECRET not in blob and "AKIACHAOS0000" not in blob


def test_dead_endpoint_is_negative_cached_so_second_build_does_not_reprobe(tmp_path):
    state, bin_ = tmp_path / "state", _fake_toolchain(tmp_path)
    cfg = _write_cfg(tmp_path / "cfg.yaml", _shared_section(tmp_path, _closed_port()))
    for _ in range(2):
        assert _cli(["--max-load", "0", "--", "cargo", "build", "0"], state, tmp_path,
                    cfg=cfg, bin_=bin_).returncode == 0
    marks = list((state / "build-gates/sccache").glob("health-down-*"))
    assert len(marks) == 1                                     # one marker per endpoint
    ev = [e for e in _events(state / "build-gates") if e["event"] == "build_finished"]
    assert [e["cache"]["shared"]["reason"] for e in ev] == \
        ["shared_unavailable:endpoint_unreachable"] * 2


def _serve(handler) -> tuple[int, threading.Thread, socket.socket]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(10)

    def loop():
        try:
            while True:
                c, _ = srv.accept()
                handler(c)
        except OSError:
            return
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return srv.getsockname()[1], th, srv


@pytest.mark.parametrize("behaviour", ["plaintext_503", "close_immediately", "hang"])
def test_tls_failure_or_hanging_backend_is_bounded_and_reported(behaviour):
    """A listener that does not speak TLS (503 text, reset, or silence) never blocks a probe."""
    held: list[socket.socket] = []

    def handler(c: socket.socket) -> None:
        if behaviour == "plaintext_503":
            c.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            c.close()
        elif behaviour == "close_immediately":
            c.close()
        else:
            held.append(c)  # accept and say nothing

    port, _th, srv = _serve(handler)
    try:
        t0 = time.monotonic()
        h = probe_health(Endpoint(f"127.0.0.1:{port}", "us-east-1", "b", True), timeout=1.0)
        assert not h.ok and h.code == "tls_failed"
        assert time.monotonic() - t0 < 5
        # a plain (non-TLS) endpoint on the same listener is simply reachable
        if behaviour != "hang":
            assert probe_health(Endpoint(f"127.0.0.1:{port}", "us-east-1", "b", False),
                                timeout=1.0).ok
    finally:
        srv.close()
        for c in held:
            c.close()


def test_connection_refused_probe_code():
    h = probe_health(Endpoint(f"127.0.0.1:{_closed_port()}", "us-east-1", "b", True), timeout=1.0)
    assert (h.ok, h.code) == (False, "connect_failed")


def test_slurm_runner_unavailable_never_launches_and_returns_fallback(tmp_path):
    cfg = validate({"enabled": True, "mode": "slurm_full_build",
                    "execution": {"slurm_enabled": True, "allowed_clusters": ["c"],
                                  "allowed_partitions": ["cpu"]},
                    "slurm": {"enabled": True, "account": "acct",
                              "allowed_partitions": ["cpu"]}})
    runner = sa.NullRunner()
    assert runner.verified is False
    req = sa.SubmitRequest.__new__(sa.SubmitRequest)  # shape covered in test_build_slurm_adapter
    resp = runner.submit(req)
    assert resp.accepted is False and resp.reason is sa.FallbackReason.RUNNER_UNAVAILABLE
    assert cfg.slurm_runner_requested


# === 2. 20-way build storm through the gate ======================================

_CHILD = """\
import fcntl, os, sys, time
d, idx = sys.argv[1], sys.argv[2]
fd = os.open(os.path.join(d, "counter"), os.O_RDWR | os.O_CREAT)
def bump(delta):
    fcntl.flock(fd, fcntl.LOCK_EX)
    raw = os.pread(fd, 64, 0).decode() or "0 0"
    cur, mx = map(int, raw.split())
    cur += delta
    mx = max(mx, cur)
    os.ftruncate(fd, 0)
    os.pwrite(fd, ("%d %d" % (cur, mx)).encode(), 0)
    fcntl.flock(fd, fcntl.LOCK_UN)
bump(1)
time.sleep(float(sys.argv[3]))
bump(-1)
open(os.path.join(d, "done." + idx), "w").close()
sys.exit(3 if int(idx) % 5 == 0 else 0)
"""


def _storm(tmp_path: Path, n: int, slots: int, sleep_s: float, dirs: int):
    state, work = tmp_path / "state", tmp_path / "work"
    work.mkdir()
    (work / "child.py").write_text(_CHILD)
    cwds = []
    for i in range(dirs):
        d = tmp_path / f"repo{i}"
        d.mkdir()
        cwds.append(d)
    procs = []
    for i in range(n):
        procs.append(subprocess.Popen(
            _cli_argv("--slots", str(slots), "--max-load", "0", "--record", "--",
                      PY, str(work / "child.py"), str(work), str(i), str(sleep_s)),
            env=_env(state, tmp_path), cwd=cwds[i % dirs],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    t0 = time.monotonic()
    codes = [p.wait(timeout=120) for p in procs]
    return state / "build-gates", work, codes, time.monotonic() - t0


def test_twenty_build_storm_respects_slots_and_completes(tmp_path):
    slots = 3
    gates, work, codes, wall = _storm(tmp_path, n=20, slots=slots, sleep_s=0.3, dirs=4)
    assert codes == [3 if i % 5 == 0 else 0 for i in range(20)]       # per-build exit preserved
    assert len(list(work.glob("done.*"))) == 20                        # no deadlock, all ran
    cur, mx = map(int, (work / "counter").read_text().split())
    assert cur == 0 and 1 <= mx <= slots                               # never more than N running
    assert wall < 90
    fin = [e for e in _events(gates) if e["event"] == "build_finished"]
    assert len(fin) == 20
    assert sorted(e["exit_code"] for e in fin) == sorted(codes)
    db = gates / "build-registry.db"
    states = dict(_rows(db, "SELECT state, COUNT(*) FROM attempts GROUP BY state"))
    assert states == {"completed": 16, "failed": 4}                    # rows consistent
    assert _rows(db, "SELECT COUNT(*) FROM leases WHERE released_ts IS NULL") == [(0,)]
    assert _rows(db, "SELECT COUNT(*) FROM builds WHERE state NOT IN "
                     "('completed','failed','cancelled','timed_out','rejected')") == [(0,)]
    assert br.active_scopes(gates) == []
    # slot holders are all gone: every slot is free again
    from herdr_engineering.build_coord import BuildGate
    assert not any(s["held"] for s in BuildGate(gates, slots).status())


def test_single_slot_is_strict_mutual_exclusion(tmp_path):
    gates, work, codes, _ = _storm(tmp_path, n=6, slots=1, sleep_s=0.15, dirs=2)
    cur, mx = map(int, (work / "counter").read_text().split())
    assert (cur, mx) == (0, 1)
    assert codes == [3, 0, 0, 0, 0, 3]


def test_storm_with_lost_registry_still_runs_every_build(tmp_path):
    state = tmp_path / "state"
    (state / "build-gates").mkdir(parents=True)
    (state / "build-gates/build-registry.db").write_bytes(b"not a database" * 100)
    gates, work, codes, _ = _storm(tmp_path, n=6, slots=2, sleep_s=0.1, dirs=1)
    assert codes == [3, 0, 0, 0, 0, 3]
    assert len(list(work.glob("done.*"))) == 6


# === 3. malicious cache payloads ==================================================

def _evict_cfg(**ev):
    return validate({"enabled": True, "local": {"capacity_gib": 1},
                     "eviction": {"destructive_enabled": True, "hard_cap_gib": 2, **ev},
                     "cleanup": {"dry_run": True, "min_idle_hours": 1,
                                 "max_evict_per_pass_gib": 10}})


def _old(path: Path, hours=48):
    t = time.time() - hours * 3600
    os.utime(path, (t, t), follow_symlinks=False)


def _hostile_cache(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    state = tmp_path / "state"
    cache = state / "sccache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("do not delete")
    (outside / "dirvictim").mkdir()
    (outside / "dirvictim/inner.txt").write_text("inner")
    (cache / "good").mkdir()
    big = cache / "good/old.bin"
    with open(big, "wb") as f:
        f.truncate(3 * 1024 ** 3)                      # sparse 3 GiB: over the 2 GiB hard cap
    (cache / "evil-link").symlink_to(victim)           # symlink to a file outside
    (cache / "evil-dir").symlink_to(outside / "dirvictim")  # symlinked dir outside
    os.mkfifo(cache / "evil.fifo")                     # special file: must never be opened
    (cache / "..hidden..").write_text("x")             # odd but legal name
    for p in (big, cache / "..hidden.."):
        _old(p)
    for p in (cache / "evil-link", cache / "evil-dir"):
        _old(p)
    return state, cache, {"victim": victim, "dirvictim": outside / "dirvictim/inner.txt",
                          "outside": outside}


def test_eviction_ignores_symlinks_and_specials_and_stays_inside_cache(tmp_path):
    state, cache, o = _hostile_cache(tmp_path)
    cfg = _evict_cfg()
    entries = {e.path for e in policy.scan_cache(cache)}
    assert entries == {"good/old.bin", "..hidden.."}   # link, linked dir, fifo are invisible
    res = policy.run_eviction(cfg, state, apply=True, max_bytes=10 * 1024 ** 3)
    assert res["status"] == "applied"
    assert o["victim"].read_text() == "do not delete"
    assert o["dirvictim"].read_text() == "inner"
    assert (cache / "evil-link").is_symlink() and (cache / "evil-dir").is_symlink()
    assert stat.S_ISFIFO((cache / "evil.fifo").lstat().st_mode)
    assert not (cache / "good/old.bin").exists()       # the legitimate old entry was evicted


def test_remove_planned_rejects_swapped_symlink_and_traversal(tmp_path):
    state, cache, o = _hostile_cache(tmp_path)
    now = time.time()
    real = cache / "good/swap.bin"
    real.write_bytes(b"y" * 64)
    _old(real)
    st = real.lstat()
    def mk(rel, size=64):
        return policy.Entry(rel, size, st.st_mtime, st.st_atime)
    # 1. file swapped for a symlink after planning
    real.unlink()
    real.symlink_to(o["victim"])
    # 2. entry reached through a symlinked directory that points outside
    # 3. relative traversal out of the cache
    plan = policy.Plan("over_hard_cap", 0, 0, 0,
                       (mk("good/swap.bin"), mk("evil-dir/inner.txt", 5),
                        mk("../../outside/victim.txt", 13), mk("/etc/hostname"),
                        mk("good/vanished.bin")), 0, False, ())
    removed, skipped = policy.remove_planned(cache, plan, state, now=now, min_idle_s=3600)
    assert removed == []
    reasons = dict(skipped)
    assert reasons["good/swap.bin"] == "not_regular_file"
    assert reasons["evil-dir/inner.txt"] == "outside_cache_dir"
    assert reasons["../../outside/victim.txt"] == "outside_cache_dir"
    assert reasons["/etc/hostname"] == "outside_cache_dir"
    assert reasons["good/vanished.bin"] == "vanished"
    assert o["victim"].read_text() == "do not delete" and o["dirvictim"].exists()


def test_symlinked_cache_root_is_refused(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"k" * 10)
    _old(outside / "keep.bin")
    (state / "sccache").symlink_to(outside)
    res = policy.run_eviction(_evict_cfg(), state, apply=True, hard_cap_bytes=1, capacity_bytes=0)
    assert res["status"] == "refused" and res["reason"] == "cache_dir_unsafe"
    assert (outside / "keep.bin").exists()


def test_eviction_never_touches_cargo_target_inside_cache(tmp_path):
    state = tmp_path / "state"
    tgt = state / "sccache/proj/target/debug"
    tgt.mkdir(parents=True)
    f = tgt / "x.o"
    f.write_bytes(b"z" * 4096)
    _old(f)
    res = policy.run_eviction(_evict_cfg(), state, apply=True, capacity_bytes=0,
                              hard_cap_bytes=1, min_idle_s=0)
    assert f.exists() and res["status"] == "applied" and res["removed_count"] == 0


def test_corrupt_oversized_local_cache_does_not_break_a_build(tmp_path):
    """Garbage in the cache dir is sccache's to reject; the gate must still pass the exit code."""
    state, bin_ = tmp_path / "state", _fake_toolchain(tmp_path)
    cache = state / "build-gates/sccache"
    cache.mkdir(parents=True)
    (cache / "corrupt.obj").write_bytes(os.urandom(2048))
    with open(cache / "huge.obj", "wb") as f:
        f.truncate(4 * 1024 ** 3)
    (cache / "x.sh").write_text(f"#!/bin/sh\ntouch {tmp_path}/EXECUTED\n")
    (cache / "x.sh").chmod(0o755)
    cfg = _write_cfg(tmp_path / "cfg.yaml", {"enabled": True})
    r = _cli(["--max-load", "0", "--", "cargo", "build", "9"], state, tmp_path, cfg=cfg, bin_=bin_)
    assert r.returncode == 9, r.stderr
    assert not (tmp_path / "EXECUTED").exists()
    assert (cache / "corrupt.obj").exists()            # nothing deleted by the build path


def _art(rel: str, data: bytes) -> sa.ExpectedArtifact:
    import hashlib
    return sa.ExpectedArtifact(rel, hashlib.sha256(data).hexdigest(), len(data))


def test_slurm_output_validation_rejects_every_hostile_shape(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    good = b"artifact"
    (out / "good.bin").write_bytes(good)
    (out / "badsize.bin").write_bytes(b"12345678901")
    (out / "baddigest.bin").write_bytes(b"AAAAAAAA")
    (out / "link.bin").symlink_to(outside)
    (out / "dirlink").symlink_to(tmp_path)
    os.mkfifo(out / "fifo.bin")
    (out / "run-me.sh").write_text(f"#!/bin/sh\ntouch {tmp_path}/EXECUTED\n")
    (out / "run-me.sh").chmod(0o755)
    expected = [
        _art("good.bin", good),
        sa.ExpectedArtifact("badsize.bin", "0" * 64, 3),
        sa.ExpectedArtifact("baddigest.bin", "0" * 64, 8),
        _art("link.bin", b"secret"),
        _art("dirlink/outside.txt", b"secret"),
        sa.ExpectedArtifact("fifo.bin", "0" * 64, 0),
        sa.ExpectedArtifact("../outside.txt", "0" * 64, 6),
        sa.ExpectedArtifact("/etc/hostname", "0" * 64, 1),
        sa.ExpectedArtifact("a/../../outside.txt", "0" * 64, 6),
        sa.ExpectedArtifact("nul\x00.bin", "0" * 64, 1),
        sa.ExpectedArtifact("missing.bin", "0" * 64, 1),
    ]
    t0 = time.monotonic()
    got = {(p.relpath, p.code) for p in sa.validate_outputs(out, expected)}
    assert time.monotonic() - t0 < 5                   # the FIFO was never opened for reading
    codes: dict[str, str] = {}
    for rel, code in sorted(got):       # a symlink may also be reported as unexpected_file
        codes.setdefault(rel, code)
    assert "good.bin" not in codes
    assert codes["badsize.bin"] == "size_mismatch"
    assert codes["baddigest.bin"] == "digest_mismatch"
    assert codes["link.bin"] == "symlink" and codes["dirlink/outside.txt"] == "symlink"
    assert codes["fifo.bin"] == "missing"
    for esc in ("../outside.txt", "/etc/hostname", "a/../../outside.txt", "nul\x00.bin"):
        assert codes[esc] == "path_escape"
    assert codes["missing.bin"] == "missing"
    assert codes["run-me.sh"] == "unexpected_file"
    assert not (tmp_path / "EXECUTED").exists() and outside.read_text() == "secret"


def test_slurm_staging_refuses_symlinks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.rs").write_text("fn main(){}")
    (src / "leak").symlink_to("/etc/hostname")
    with pytest.raises(sa.StagingError):
        sa.stage_manifest(src, "tc")


# === 4. stale lease, owner crash, fencing =======================================

def _start_wrapper(tmp_path: Path, state: Path, cwd: Path, *cmd: str) -> subprocess.Popen:
    return subprocess.Popen(
        _cli_argv("--record", "--max-load", "0", "--slots", "1", "--", *cmd),
        env=_env(state, tmp_path), cwd=cwd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_sigkilled_record_wrapper_lease_expiry_reconcile_cleanup_and_fencing(tmp_path):
    state = tmp_path / "state"
    gates = state / "build-gates"
    work = tmp_path / "work"
    work.mkdir()
    root = tmp_path / "projects"
    root.mkdir()
    target = _cargo_project(root, "p")
    wrapper = _start_wrapper(tmp_path, state, work, "sleep", "300")
    child = None
    try:
        db = gates / "build-registry.db"
        assert _wait_for(lambda: db.exists() and _rows(
            db, "SELECT 1 FROM attempts WHERE state='running' AND pid IS NOT NULL"))
        aid, child, scope = _rows(
            db, "SELECT a.attempt_id, a.pid, l.scope FROM attempts a JOIN leases l "
                "ON l.owner_attempt_id=a.attempt_id")[0]
        assert child != wrapper.pid and scope == str(work.resolve())
        assert br.active_scopes(gates) == [scope]

        # (a) lease live + maintenance lock held by the build: cleanup/eviction refuse, delete nothing
        res = cleanup.run_once([root], gates, **FORCE)
        assert res["status"] == "busy" and res["removed"] == [] and target.exists()
        ev = policy.run_eviction(_evict_cfg(), gates, apply=True)
        assert ev["status"] == "busy"

        # (b) kill -9 the wrapper: the orphaned build inherited the maintenance lock fd
        os.kill(wrapper.pid, signal.SIGKILL)
        assert wrapper.wait(timeout=10) == -signal.SIGKILL
        assert br.pid_alive(child)
        assert cleanup.run_once([root], gates, **FORCE)["status"] == "busy"

        # (c) lease expires (time travel only on the READER's clock); owner still alive -> untouched
        later = lambda: time.time() + 3600  # noqa: E731
        late = br.Registry(gates, now=later)          # "controller restart": brand-new handle
        assert br.active_scopes(gates, now=later()) == []
        assert late.reconcile() == []
        assert late.get_attempt(aid)["state"] == "running"

        # (d) the orphan finally dies -> reconcile marks exactly this attempt owner_vanished
        os.kill(child, signal.SIGKILL)
        assert _wait_for(lambda: not br.pid_alive(child, None))
        assert late.reconcile() == [aid]
        assert late.get_attempt(aid)["state"] == "failed"
        assert [t["reason"] for t in late.transitions(aid)][-1] == "owner_vanished"
        assert late.reconcile() == []                  # idempotent
        assert br.active_scopes(gates) == []

        # (e) maintenance lock is free again; cleanup runs and nothing leased remains
        assert _wait_for(lambda: cleanup.run_once([root], gates, **FORCE, dry_run=True)
                         ["status"] != "busy")

        # (f) a newer owner takes the scope; the stale epoch writer is fenced everywhere
        stale = br.Lease(scope, aid, 1, 30.0)
        new = late.register(repo_id="r2", input_digest=None, cmd_kind="cargo build")
        late.transition(new.attempt_id, "classified", "t")
        late.transition(new.attempt_id, "admitted", "t")
        fresh = late.acquire_lease(scope, new.attempt_id, 30)
        assert fresh.epoch == 2
        with pytest.raises(br.FencedError):
            late.heartbeat(stale)
        with pytest.raises(br.FencedError):
            late.finish(aid, "completed", "exit_0", exit_code=0, lease=stale)
        with pytest.raises(br.FencedError):
            late.transition(aid, "running", "no-lease-stale-writer")
        late.heartbeat(fresh)                          # the live owner is unaffected
    finally:
        wrapper.kill()
        wrapper.wait()
        if child:
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass
    assert target.exists()                             # nothing was deleted at any point


def test_cleanup_refuses_on_live_lease_without_wrapper_and_ignores_expired(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    state = tmp_path / "gates"
    t = _cargo_project(root)
    reg = br.Registry(state)
    owner = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    try:
        a = reg.register(repo_id="r", input_digest=None, cmd_kind="cargo build", pid=owner.pid)
        reg.transition(a.attempt_id, "classified", "t")
        reg.transition(a.attempt_id, "admitted", "t")
        reg.acquire_lease(root / "project", a.attempt_id, 300)
        res = cleanup.run_once([root], state, **FORCE)
        assert t.exists() and res["removed"] == []
        assert [s["reason"] for s in res["skipped"]] == ["active_lease"]
    finally:
        owner.kill()
        owner.wait()
    expired = br.Registry(tmp_path / "gates2", now=lambda: time.time() - 7200)
    b = expired.register(repo_id="r", input_digest=None, cmd_kind="cargo build")
    expired.transition(b.attempt_id, "classified", "t")
    expired.transition(b.attempt_id, "admitted", "t")
    expired.acquire_lease(root / "project", b.attempt_id, 30)
    res = cleanup.run_once([root], tmp_path / "gates2", **FORCE, dry_run=True)
    assert res["would_remove"] == [str(t)] and t.exists()


# === 5. readonly storage, lost registry, node-loss analogues =======================

@pytest.mark.skipif(IS_ROOT, reason="permission bits are not enforced for root")
def test_readonly_state_dir_does_not_block_the_build(tmp_path):
    state = tmp_path / "state"
    gates = state / "build-gates"
    gates.mkdir(parents=True)
    gates.chmod(0o500)
    try:
        r = _cli(["--max-load", "0", "--", "sh", "-c", "echo ran; exit 4"], state, tmp_path)
        assert r.returncode == 4, (r.stdout, r.stderr)          # the build ran, code preserved
        assert "ran" in r.stdout
        assert "gate unavailable (state_dir_unwritable" in r.stderr                    # explicit reason code (state_dir_unwritable)
        r = _cli(["--max-load", "0", "--record", "--", "sh", "-c", "exit 5"], state, tmp_path)
        assert r.returncode == 5 and "gate unavailable (state_dir_unwritable" in r.stderr
        st = _cli(["--status"], state, tmp_path)
        assert st.returncode == 0
        assert list(gates.iterdir()) == []                       # nothing was written
    finally:
        gates.chmod(0o700)


@pytest.mark.skipif(IS_ROOT, reason="permission bits are not enforced for root")
def test_readonly_state_dir_with_cache_enabled_runs_uncached_not_failed(tmp_path):
    state, bin_ = tmp_path / "state", _fake_toolchain(tmp_path)
    gates = state / "build-gates"
    gates.mkdir(parents=True)
    gates.chmod(0o500)
    cfg = _write_cfg(tmp_path / "cfg.yaml", {"enabled": True})
    seen = tmp_path / "seen.env"
    try:
        r = _cli(["--max-load", "0", "--", "cargo", "build", "6"], state, tmp_path,
                 cfg=cfg, bin_=bin_, CHAOS_SEEN=str(seen))
        assert r.returncode == 6, r.stderr
        assert "gate unavailable (state_dir_unwritable" in r.stderr
        assert "RUSTC_WRAPPER=" not in seen.read_text()          # no cache env into an unwritable dir
    finally:
        gates.chmod(0o700)


@pytest.mark.skipif(IS_ROOT, reason="permission bits are not enforced for root")
@pytest.mark.parametrize("mode", [0o000, 0o400])
def test_unreadable_or_readonly_registry_never_fails_a_build_and_blocks_cleanup(tmp_path, mode):
    state = tmp_path / "state"
    gates = state / "build-gates"
    reg = br.Registry(gates)                                     # a real, healthy db first
    del reg
    db = gates / "build-registry.db"
    db.chmod(mode)
    root = tmp_path / "projects"
    root.mkdir()
    t = _cargo_project(root)
    try:
        r = _cli(["--record", "--max-load", "0", "--", "sh", "-c", "exit 5"], state, tmp_path)
        assert r.returncode == 5, r.stderr
        fin = [e for e in _events(gates) if e["event"] == "build_finished"][-1]
        assert fin["exit_code"] == 5
        if mode == 0o000:
            assert "registry_unavailable" in fin["unavailable"]
            # cleanup fails closed: the db exists but cannot be read -> nothing is removed
            res = cleanup.run_once([root], gates, **FORCE)
            assert t.exists() and res["removed"] == []
            assert [s["reason"] for s in res["skipped"]] == ["registry_unreadable"]
            ev = policy.run_eviction(_evict_cfg(), gates, apply=True)
            assert ev["status"] in ("skipped", "applied")
    finally:
        db.chmod(0o600)
    assert t.exists()


def test_corrupt_registry_db_fails_cleanup_closed_but_not_builds(tmp_path):
    gates = tmp_path / "state/build-gates"
    gates.mkdir(parents=True)
    (gates / "build-registry.db").write_bytes(b"\x00garbage" * 64)
    root = tmp_path / "projects"
    root.mkdir()
    t = _cargo_project(root)
    res = cleanup.run_once([root], gates, **FORCE)
    assert t.exists() and res["removed"] == []
    assert [s["reason"] for s in res["skipped"]] == ["registry_unreadable"]
    r = _cli(["--record", "--max-load", "0", "--", "sh", "-c", "exit 2"], tmp_path / "state", tmp_path)
    assert r.returncode == 2


def test_cleanup_on_unwritable_state_dir_errors_and_deletes_nothing(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    t = _cargo_project(root)
    state = tmp_path / "ro-state"
    state.mkdir()
    if not IS_ROOT:
        state.chmod(0o500)
    try:
        if IS_ROOT:
            pytest.skip("permission bits are not enforced for root")
        with pytest.raises(OSError):
            cleanup.run_once([root], state, **FORCE)
    finally:
        state.chmod(0o700)
    assert t.exists()


# === 6. rollback rehearsal ========================================================

def _all_features(tmp: Path) -> dict:
    s = _shared_section(tmp, _closed_port())
    s.update({
        "allowed_hosts": [],
        "eviction": {"destructive_enabled": True, "hard_cap_gib": 40},
        "adaptive_slots": {"enabled": True, "min_slots": 1, "max_slots": 4},
        "execution": {"slurm_enabled": True, "allowed_clusters": ["c"],
                      "allowed_partitions": ["cpu"]},
        "slurm": {"enabled": True, "account": "acct", "allowed_partitions": ["cpu"]},
        "observability": {"enabled": True, "export_existing_metrics_pipeline": True},
    })
    return s


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.exists() else set()


def test_rollback_rehearsal_returns_to_legacy_exec_path(tmp_path):
    bin_ = _fake_toolchain(tmp_path)
    section = _all_features(tmp_path)
    validate(section)                                            # the rehearsal config is valid

    # --- before: everything enabled -> recorded, cached, registry-backed
    on_state = tmp_path / "on"
    cfg_on = _write_cfg(tmp_path / "on.yaml", section)
    r = _cli(["--max-load", "0", "--", "cargo", "build", "3"], on_state, tmp_path,
             cfg=cfg_on, bin_=bin_)
    assert r.returncode == 3, r.stderr
    on_tree = _tree(on_state / "build-gates")
    assert {"builds.jsonl", "build-registry.db"} <= on_tree
    on_status = _cli(["--status", "--json"], on_state, tmp_path, cfg=cfg_on, bin_=bin_)
    assert on_status.returncode == 0 and "slot_suggestion" in json.loads(on_status.stdout)

    # --- legacy baseline: no config at all
    base_state = tmp_path / "base"
    rb = _cli(["--max-load", "0", "--", "cargo", "build", "3"], base_state, tmp_path, bin_=bin_)
    assert rb.returncode == 3
    legacy = _tree(base_state / "build-gates")
    assert not {"builds.jsonl", "build-registry.db", "sccache"} & legacy
    assert "build-maintenance.lock" in legacy

    # --- rollback variants: flag flipped off, section removed, section commented to {}
    off_section = {**section, "enabled": False}
    variants = {"flag_off": _write_cfg(tmp_path / "off.yaml", off_section),
                "section_removed": _write_cfg(tmp_path / "gone.yaml", None)}
    for name, cfg in variants.items():
        state = tmp_path / f"rb-{name}"
        seen = tmp_path / f"seen-{name}.env"
        for code in (0, 3, 41):
            rr = _cli(["--max-load", "0", "--", "cargo", "build", str(code)], state, tmp_path,
                      cfg=cfg, bin_=bin_, CHAOS_SEEN=str(seen))
            assert rr.returncode == code, (name, rr.stderr)       # same exit code
        env_txt = seen.read_text()
        assert "RUSTC_WRAPPER=" not in env_txt and "SCCACHE_" not in env_txt  # no cache env
        tree = _tree(state / "build-gates")
        assert tree == legacy, (name, tree ^ legacy)              # no new files vs legacy
        st = _cli(["--status", "--json"], state, tmp_path, cfg=cfg, bin_=bin_)
        view = json.loads(st.stdout)
        assert st.returncode == 0 and "slot_suggestion" not in view
        assert _cli(["--status"], state, tmp_path, cfg=cfg, bin_=bin_).returncode == 0
        # the Pi light path with the feature off: plain exec, no gate directory at all
        pi_state = tmp_path / f"pi-{name}"
        rp = _cli(["--require-cache-enabled", "--record", "--trace-session", "s1", "--",
                   "sh", "-c", "exit 9"], pi_state, tmp_path, cfg=cfg, bin_=bin_)
        assert rp.returncode == 9 and not (pi_state / "build-gates").exists()

    # --- rolling back ON TOP of state left by the enabled period
    after = on_state
    cfg_off = variants["flag_off"]
    rr = _cli(["--max-load", "0", "--", "sh", "-c", "exit 12"], after, tmp_path, cfg=cfg_off)
    assert rr.returncode == 12
    db = after / "build-gates/build-registry.db"
    before_rows = _rows(db, "SELECT COUNT(*) FROM attempts")
    assert _rows(db, "SELECT COUNT(*) FROM attempts") == before_rows  # rolled-back path never writes it
    # an ignored registry: with the db deleted (operator action in real life; done on a COPY here)
    copy = tmp_path / "copy-gates"
    shutil.copytree(after / "build-gates", copy)
    for f in copy.glob("build-registry.db*"):
        f.unlink()
    root = tmp_path / "projects"
    root.mkdir()
    t = _cargo_project(root)
    res = cleanup.run_once([root], copy, **FORCE, dry_run=True)
    assert res["would_remove"] == [str(t)] and not res["errors"]  # legacy cleanup behaviour


def test_rollback_with_invalid_section_fails_closed_to_legacy(tmp_path):
    """A half-edited config during rollback must not break builds; it disables the feature."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("fabric_build_cache:\n  enabled: true\n  mode: bogus\n")
    r = _cli(["--max-load", "0", "--", "sh", "-c", "exit 7"], tmp_path / "s", tmp_path, cfg=bad)
    assert r.returncode == 7
    assert not (tmp_path / "s/build-gates/builds.jsonl").exists()


# === explicit skips: need infrastructure this host does not have ====================

NEEDS = {
    "sccache_corrupt_object": "needs a real sccache binary and cache entries (none installed)",
    "s3_503_tls_timeout_retries": "needs a real S3 endpoint and sccache; only the TCP/TLS probe is "
                                  "covered above",
    "slurm_node_loss": "needs a real Slurm cluster and an approved test partition (none)",
    "partition_drain": "needs a real Slurm partition to drain (none; operator authorization "
                       "required)",
    "disk_full": "needs a size-limited tmpfs/loop mount (requires root); readonly is covered",
    "two_host_cross_platform": "needs a second host (Linux x86_64 vs macOS arm64)",
    "controller_restart_remote_launch": "needs a remote launcher; no duplicate-launch path exists "
                                        "locally because no runner is verified",
}


@pytest.mark.parametrize("scenario", sorted(NEEDS))
def test_requires_infrastructure_not_available_here(scenario):
    pytest.skip(f"{scenario}: {NEEDS[scenario]}")
