"""Spec 01 (measure): opt-in build instrumentation tests.

Real subprocesses, real temp state dirs. The only fake is a shell ``sccache``
script used in a clearly labelled parser-contract test.
"""
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from herdr_engineering import build_metrics as bm
from herdr_engineering.build_coord import run_gated

REPO = Path(__file__).resolve().parents[1]
SECRET = "S3CR3T-TOKEN-xyz123"


def _events(state: Path) -> list[dict]:
    p = state / "builds.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()]


def _rec(cmd, state, **kw):
    kw.setdefault("slots", 2)
    kw.setdefault("max_load", 0)
    return run_gated(cmd, state_dir=state, record=True, **kw)


# --- exit code propagation -------------------------------------------------

@pytest.mark.parametrize("script,code", [("exit 0", 0), ("exit 7", 7), ("kill -9 $$", 137)])
def test_exit_code_propagated(tmp_path, script, code):
    assert _rec(["sh", "-c", script], tmp_path) == code
    fin = [e for e in _events(tmp_path) if e["event"] == "build_finished"][0]
    assert fin["exit_code"] == code


def test_missing_binary_returns_127(tmp_path):
    assert _rec(["definitely-not-a-binary-xyz"], tmp_path) == 127


# --- lock lifetime (AUDIT hazard 2) ------------------------------------------

def test_child_inherits_slot_and_maintenance_locks(tmp_path):
    out = tmp_path / "probe.txt"
    probe = textwrap.dedent(f"""
        import fcntl, os, sys
        res = []
        for name in ("gate.0", "build-maintenance.lock"):
            fd = os.open(os.path.join({str(tmp_path)!r}, name), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                res.append(name + ":FREE")
            except BlockingIOError:
                res.append(name + ":HELD")
        open({str(out)!r}, "w").write(",".join(res))
    """)
    assert _rec([sys.executable, "-c", probe], tmp_path) == 0
    assert out.read_text() == "gate.0:HELD,build-maintenance.lock:HELD"


def test_cleanup_lock_blocked_while_child_runs_then_free(tmp_path):
    script = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); "
        "from herdr_engineering.build_coord import run_gated; "
        f"sys.exit(run_gated(['sleep','2'], slots=2, max_load=0, record=True, "
        f"state_dir={str(tmp_path)!r}))"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        time.sleep(1.0)
        fd = os.open(tmp_path / "build-maintenance.lock", os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
    finally:
        proc.wait(timeout=15)
    fd = os.open(tmp_path / "build-maintenance.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free now
    os.close(fd)


def test_sigterm_forwarded_to_child_only(tmp_path):
    script = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); "
        "from herdr_engineering.build_coord import run_gated; "
        f"sys.exit(run_gated(['sleep','30'], slots=2, max_load=0, record=True, "
        f"state_dir={str(tmp_path)!r}))"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    fin = [e for e in _events(tmp_path) if e["event"] == "build_finished"][0]
    assert fin["exit_code"] == 128 + signal.SIGTERM


# --- events ----------------------------------------------------------------

def test_events_have_required_fields_and_0600(tmp_path):
    assert _rec(["sh", "-c", "exit 0"], tmp_path) == 0
    path = tmp_path / "builds.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ev = _events(tmp_path)
    assert [e["event"] for e in ev] == ["build_started", "build_finished"]
    fin = ev[1]
    for k in bm.FINISHED_FIELDS:
        assert k in fin, k
    assert fin["build_id"] == ev[0]["build_id"]
    assert fin["elapsed_s"] >= 0
    assert fin["cmd_kind"] == "other"
    assert isinstance(fin["unavailable"], list)
    for k in fin["unavailable"]:
        assert fin[k] is None


def test_cpu_time_captured(tmp_path):
    burn = "import time; t=time.process_time()\nwhile time.process_time()-t<0.3: pass"
    _rec([sys.executable, "-c", burn], tmp_path)
    fin = _events(tmp_path)[-1]
    assert fin["user_cpu_s"] + fin["sys_cpu_s"] >= 0.25
    assert fin["max_rss_kb"] > 0


def test_redaction_of_argv_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", SECRET)
    _rec(["sh", "-c", f"echo {SECRET} >/dev/null; exit 0"], tmp_path)
    _rec(["cargo", "build", "--token", SECRET, "--manifest-path", "/nope"], tmp_path)
    text = (tmp_path / "builds.jsonl").read_text()
    assert SECRET not in text
    assert "--token" not in text


@pytest.mark.parametrize("cmd,kind", [
    (["cargo", "build", "--release"], "cargo build"),
    (["/usr/bin/cargo", "test", "-j4"], "cargo test"),
    (["cargo", "+nightly", "check"], "cargo check"),
    (["cargo", "clippy"], "cargo clippy"),
    (["cargo", "fmt"], "other"),
    (["npx", "tsc", "--noEmit"], "tsc"),
    (["tsc"], "tsc"),
    (["make"], "other"),
    ([], "other"),
])
def test_classify(cmd, kind):
    assert bm.classify_cmd(cmd) == kind


def test_unavailable_when_sccache_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    if bm.shutil.which("sccache"):
        pytest.skip("sccache in system dirs")
    _rec(["sh", "-c", "exit 0"], tmp_path)
    fin = _events(tmp_path)[-1]
    assert "sccache_stats" in fin["unavailable"]
    assert fin["sccache_stats"] is None


def test_sccache_stats_diff_parser_contract(tmp_path, monkeypatch):
    """PARSER-CONTRACT test: a fake sccache feeds canned JSON to the diff logic."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "n"
    fake = bindir / "sccache"
    fake.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}
        if [ $n -eq 1 ]; then echo '{{"stats":{{"compile_requests":10,"cache_hits":{{"counts":{{"Rust":4}}}},"x":"s"}}}}'
        else echo '{{"stats":{{"compile_requests":15,"cache_hits":{{"counts":{{"Rust":9}}}},"x":"s"}}}}'; fi
    """))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    state = tmp_path / "state"
    _rec(["sh", "-c", "exit 0"], state)
    fin = _events(state)[-1]
    assert fin["sccache_stats"] == {"compile_requests": 5, "cache_hits.counts.Rust": 5}
    assert "sccache_stats" not in fin["unavailable"]


def test_diff_stats_top_level_fallback():
    assert bm.diff_stats({"a": 1, "b": 2.5, "s": "x"}, {"a": 4, "b": 2.5}) == {"a": 3}


def test_sccache_hang_times_out(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "sccache"
    fake.write_text("#!/bin/sh\nsleep 20\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    t = time.monotonic()
    assert bm.sccache_snapshot(timeout=0.5) is None
    assert time.monotonic() - t < 5


# --- failure isolation / rotation ------------------------------------------

def test_unwritable_metrics_do_not_change_exit_code(tmp_path, capsys):
    (tmp_path / "builds.jsonl").mkdir()  # open() as a file must fail
    assert _rec(["sh", "-c", "exit 7"], tmp_path) == 7
    assert capsys.readouterr().err.count("build-metrics:") <= 1


def test_measurement_exception_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "read_proc_io_tree", lambda *a, **k: 1 / 0)
    assert _rec(["sh", "-c", "exit 3"], tmp_path) == 3


def test_rotation_at_size_limit(tmp_path):
    p = tmp_path / "builds.jsonl"
    p.write_text("x" * 200 + "\n")
    bm.append_event(tmp_path, {"event": "build_started"}, max_bytes=100)
    assert (tmp_path / "builds.jsonl.1").read_text().startswith("xxx")
    assert len(p.read_text().splitlines()) == 1
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_non_record_path_still_execs(tmp_path):
    script = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); "
        "from herdr_engineering.build_coord import run_gated; "
        f"sys.exit(run_gated(['sh','-c','exit 5'], slots=2, max_load=0, "
        f"state_dir={str(tmp_path)!r}))"
    )
    assert subprocess.run([sys.executable, "-c", script]).returncode == 5
    assert not (tmp_path / "builds.jsonl").exists()


# --- summarize --------------------------------------------------------------

def test_summarize(tmp_path):
    for s in ("exit 0", "exit 1", "exit 0"):
        _rec(["sh", "-c", s], tmp_path)
    s = bm.summarize(tmp_path / "builds.jsonl")
    assert s["builds"] == 3
    assert s["failed"] == 1
    k = s["by_cmd_kind"]["other"]
    assert k["count"] == 3 and k["p50_elapsed_s"] <= k["p95_elapsed_s"]
    assert "total_proc_io_write_bytes" in s
    assert isinstance(s["unavailable_seen"], list)


def test_summarize_missing_file(tmp_path):
    assert bm.summarize(tmp_path / "nope.jsonl")["builds"] == 0


# --- bench harness ----------------------------------------------------------

def test_bench_script_throwaway_crate(tmp_path):
    import shutil
    if not shutil.which("cargo"):
        pytest.skip("cargo not installed")
    crate = tmp_path / "c"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        '[package]\nname="c"\nversion="0.1.0"\nedition="2021"\n')
    (crate / "src/main.rs").write_text("fn main(){}\n")
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts/build_bench.py"), "--repeat", "2",
         "--dir", str(crate), "--", "cargo", "build", "--offline"],
        capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    table = json.loads(r.stdout)
    assert len(table["runs"]) == 2
    assert table["runs"][0]["exit_code"] == 0
    assert "unavailable" in table["runs"][0]
    assert table["runs"][0]["phase"] == "warm"


def test_bench_refuses_cold_outside_own_tempdir(tmp_path):
    crate = tmp_path / "c"
    crate.mkdir()
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts/build_bench.py"), "--cold",
         "--dir", str(crate), "--", "true"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "refus" in r.stderr.lower()
