"""The fabric host agent's per-run resource collector, driven by recorded ``ps`` listings and a recorded cgroup v2 tree.

No mocks: the data sources are recorded fixtures (tests/fixtures/resource_runs), working directories are real
directories, and delivery is real HTTP to a real ControlPlaneServer on a loopback port.
"""
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering import resource_runs_collector as rc
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.host_metrics import agent_roots, count_agents, parse_ps_rows

FIX = Path(__file__).parent / "fixtures" / "resource_runs"
T = 1_762_250_000.0          # 2025-11-04T09:53:20Z
KIB = 1024


def ps_of(*names):
    return [(FIX / name).read_text() for name in names]


class Clock:
    def __init__(self, t=T):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def cp():
    svr = ControlPlaneServer(ControlPlaneRepo())
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    posted = []
    state = {"up": True}

    def post(envelope):
        posted.append(envelope)
        if not state["up"]:
            return None
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(envelope).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def profiles(project):
        with urllib.request.urlopen(f"{base}/api/v1/resource-profiles?project={project}") as r:
            return json.loads(r.read())["profiles"]

    yield post, profiles, posted, state
    svr.shutdown()


def collector(tmp_path, post, listings, clock, cwds, **kw):
    feed = iter(listings)
    return rc.RunCollector(host="mac-1", state_path=tmp_path / "state" / "resource-runs.json", post=post,
                           ps=lambda: next(feed), cwd_of=cwds.get, project_of=lambda cwd: Path(cwd).name,
                           clock=clock, linux=kw.pop("linux", False), **kw)


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("text,seconds", [("0:10.00", 10.0), ("117:26.55", 117 * 60 + 26.55), ("01:40", 100.0),
                                          ("10:00", 600.0), ("1:02:03", 3723.0), ("05-15:12:19", 5 * 86400 + 54739),
                                          ("00:00:10", 10.0), ("3-01:02:03", 3 * 86400 + 3723)])
def test_ps_durations_parse_on_macos_and_linux(text, seconds):
    assert rc.parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "abc", "10", "1:2:3:4", "-1:00", "1:00.1234567"])
def test_bad_durations_are_unknown(text):
    assert rc.parse_duration(text) is None


def test_ps_rows_carry_numbers_and_the_process_name_only():
    rows = rc.parse_ps_runs(ps_of("macos_ps_1.txt")[0])
    chrome = next(r for r in rows if r["pid"] == 810)
    assert chrome["command"] == "Google Chrome" and chrome["rss_b"] == 250000 * KIB
    assert set(chrome) == {"pid", "ppid", "rss_b", "cpu_s", "elapsed_s", "command"}
    assert rc.parse_ps_runs("garbage\n 1 2 x 0:01 0:01 a\n") == []
    # argv and environment are never asked for
    assert "args" not in rc.PS_RUN_ARGV[-1] and "command" not in rc.PS_RUN_ARGV[-1]
    assert rc.PS_RUN_ARGV[-1].endswith("comm=")


def test_agent_roots_is_the_host_samplers_agent_count():
    rows = rc.parse_ps_runs(ps_of("macos_ps_1.txt")[0])
    roots = agent_roots(rows, ("pi", "claude", "codex"))
    assert sorted(r["pid"] for r in roots) == [600, 700]      # 602 is claude under claude: the same agent
    assert count_agents(rows, ("pi", "claude", "codex")) == 2
    assert count_agents(parse_ps_rows("1 0 root 0 0 launchd\n2 1 u 0 0 claude\n3 2 u 0 0 claude\n"),
                        ("claude",)) == 1


# --------------------------------------------------------------------------- macOS: process stats

def test_macos_runs_from_recorded_ps_reach_the_control_plane(tmp_path, cp):
    post, profiles, posted, _ = cp
    claude_dir, codex_dir = tmp_path / "herdr-engineering", tmp_path / "pi-engineering"
    claude_dir.mkdir(), codex_dir.mkdir()
    (claude_dir / "a.txt").write_bytes(b"x" * 1000)
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt", "macos_ps_3.txt"), clock,
                  {600: str(claude_dir), 700: str(codex_dir)})
    assert c.tick() == [] and posted == []
    assert len(c.sessions) == 2
    clock.t = T + 30
    (claude_dir / "sub").mkdir()
    (claude_dir / "sub" / "b.bin").write_bytes(b"y" * 5000)
    [codex] = c.tick()                                     # 700 is gone: its run is reported
    assert codex["agent_kind"] == "codex" and codex["project"] == "pi-engineering"
    assert codex["peak_memory_bytes"] == 50000 * KIB and codex["cpu_seconds"] == 1.0
    assert codex["started_at"] == "2025-11-04T09:53:00+00:00" and codex["ended_at"] == "2025-11-04T09:53:20+00:00"
    assert codex["disk_growth_bytes"] == 0 and codex["exit_reason"] == "exited" and codex["hit_limit"] is False
    clock.t = T + 60
    [claude] = c.tick()                                    # 600 is gone; pid 700 is a NEW codex session
    assert claude["agent_kind"] == "claude" and claude["host"] == "mac-1"
    assert claude["peak_memory_bytes"] == (300000 + 1000 + 700000) * KIB     # the tree's peak, tick 2
    assert claude["cpu_seconds"] == pytest.approx(25 + 5 + 0.5 + 30)        # every process seen, exited ones too
    assert claude["disk_growth_bytes"] == 5000
    assert claude["started_at"] == "2025-11-04T09:51:40+00:00" and claude["ended_at"] == "2025-11-04T09:53:50+00:00"
    assert [p["pid"] for p in c.sessions.values()] == [700]
    assert [e["entity_id"] for e in posted] == [codex["session_id"], claude["session_id"]]
    assert c.outbox == [] and c.sent == [codex["session_id"], claude["session_id"]]
    [prof] = profiles("herdr-engineering")
    assert prof["count"] == 1 and prof["metrics"]["peak_memory_bytes"]["max"] == 1001000 * KIB


def test_no_duplicate_after_a_restart(tmp_path, cp):
    post, profiles, posted, _ = cp
    work = tmp_path / "pi-engineering"
    work.mkdir()
    clock = Clock()
    first = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {600: str(work), 700: str(work)})
    first.tick()
    clock.t = T + 30
    [codex] = first.tick()
    # the agent restarts and sees the same listing again, then the next one: nothing is reported twice
    clock.t = T + 31
    second = collector(tmp_path, post, ps_of("macos_ps_2.txt", "macos_ps_3.txt"), clock, {600: str(work), 700: str(work)})
    assert set(second.sessions) == set(first.sessions) and second.sent == [codex["session_id"]]
    assert second.tick() == []
    clock.t = T + 60
    [claude] = second.tick()
    assert claude["session_id"] != codex["session_id"]
    assert [e["entity_id"] for e in posted] == [codex["session_id"], claude["session_id"]]
    assert {p["agent_kind"]: p["count"] for p in profiles("pi-engineering")} == {"claude": 1, "codex": 1}


def test_a_run_queued_while_the_control_plane_is_down_is_delivered_once_after_a_restart(tmp_path, cp):
    post, profiles, posted, state = cp
    work = tmp_path / "pi-engineering"
    work.mkdir()
    clock = Clock()
    state["up"] = False
    first = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {600: str(work), 700: str(work)})
    first.tick()
    clock.t = T + 30
    [codex] = first.tick()
    assert len(first.outbox) == 1 and first.sent == []      # queued and saved, not lost
    state["up"] = True
    second = collector(tmp_path, post, ps_of("macos_ps_2.txt"), clock, {600: str(work)})
    assert [e["entity_id"] for e in second.outbox] == [codex["session_id"]]
    second.tick()
    assert second.outbox == [] and second.sent == [codex["session_id"]]
    # a crash after delivery but before saving replays the same event: the server keeps one row
    assert post(posted[-1]) == 200
    assert [p["count"] for p in profiles("pi-engineering")] == [1]


def test_a_reused_pid_is_a_new_session_and_ends_the_old_one(tmp_path, cp):
    post, _, posted, _ = cp
    work = tmp_path / "pi-engineering"
    work.mkdir()
    first = ps_of("macos_ps_1.txt")[0]
    # 30 s later pid 700 is listed again, as codex, but started 5 s ago: a different process
    reused = (first.replace("  700     1  50000   0:01.00    00:20", "  700     1  20000   0:00.10    00:05")
              .replace("  600   500 400000   0:10.00    01:40", "  600   500 400000   0:10.00    02:10"))
    assert reused != first
    clock = Clock()
    c = collector(tmp_path, post, [first, reused], clock, {600: str(work), 700: str(work)})
    c.tick()
    [old] = [sid for sid, s in c.sessions.items() if s["pid"] == 700]
    clock.t = T + 30
    [ended] = c.tick()
    assert ended["session_id"] == old and ended["agent_kind"] == "codex"
    [new] = [sid for sid, s in c.sessions.items() if s["pid"] == 700]
    assert new != old and c.sessions[new]["start"] == T + 25
    assert [e["entity_id"] for e in posted] == [old]


def test_a_delivered_run_restored_from_stale_state_is_not_reported_again(tmp_path, cp):
    post, _, posted, _ = cp
    work = tmp_path / "pi-engineering"
    work.mkdir()
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {600: str(work), 700: str(work)})
    c.tick()
    [codex] = [sid for sid, s in c.sessions.items() if s["pid"] == 700]
    c.sent.append(codex)          # e.g. an older state file restored after the run was already delivered
    clock.t = T + 30
    assert c.tick() == [] and posted == []


def test_a_failed_ps_ends_nothing(tmp_path, cp):
    post, _, posted, _ = cp
    work = tmp_path / "w"
    work.mkdir()
    clock = Clock()
    c = collector(tmp_path, post, [ps_of("macos_ps_1.txt")[0], None, ""], clock, {600: str(work), 700: str(work)})
    c.tick()
    assert c.tick() == [] and c.tick() == []
    assert len(c.sessions) == 2 and posted == []


def test_a_session_without_a_known_project_is_never_reported(tmp_path, cp):
    post, _, posted, _ = cp
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {})   # cwd unknown
    c.tick()
    clock.t = T + 30
    assert c.tick() == [] and posted == []


def test_a_project_name_the_control_plane_refuses_is_never_reported(tmp_path, cp):
    post, _, posted, _ = cp
    odd = tmp_path / "has space"
    odd.mkdir()
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {700: str(odd)})
    c.tick()
    clock.t = T + 30
    assert c.tick() == [] and posted == []


def test_a_refused_event_is_dropped_and_a_transient_failure_keeps_the_queue(tmp_path):
    answers = iter([503, None, 400])
    calls = []

    def post(envelope):
        calls.append(envelope["entity_id"])
        return next(answers)
    work = tmp_path / "w"
    work.mkdir()
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {700: str(work)})
    c.tick()
    clock.t = T + 30
    c.tick()
    assert len(c.outbox) == 1                 # 503: kept
    assert c.flush() == 0 and len(c.outbox) == 1   # unreachable: kept
    assert c.flush() == 0 and c.outbox == [] and c.sent == []   # 400: dropped, never retried
    assert len(calls) == 3


def test_the_state_file_is_private(tmp_path, cp):
    post, *_ = cp
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt"), clock, {})
    c.tick()
    assert c.state_path.stat().st_mode & 0o777 == 0o600


def test_a_corrupt_state_file_starts_fresh(tmp_path, cp):
    post, *_ = cp
    path = tmp_path / "state" / "resource-runs.json"
    path.parent.mkdir()
    for text in ("{not json", json.dumps({"version": 99}), json.dumps([1, 2])):
        path.write_text(text)
        c = collector(tmp_path, post, [], Clock(), {})
        assert c.sessions == {} and c.outbox == [] and c.sent == []


# --------------------------------------------------------------------------- Linux: cgroup counters

def _cgroup(root, which):
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(FIX / "linux" / which, root)


def test_linux_runs_use_the_sessions_cgroup_and_survive_it_vanishing(tmp_path, cp):
    post, profiles, posted, _ = cp
    work = tmp_path / "herdr-engineering"
    work.mkdir()
    cg_root = tmp_path / "cgroup"
    _cgroup(cg_root, "cgroup_1")
    clock = Clock()
    c = collector(tmp_path, post, ps_of("linux_ps_1.txt", "linux_ps_2.txt", "linux_ps_2b.txt", "linux_ps_3.txt"),
                  clock, {600: str(work)}, linux=True, proc_root=FIX / "linux" / "proc", cgroup_root=cg_root)
    c.tick()
    [session] = c.sessions.values()
    assert session["agent_kind"] == "pi" and session["cgroup"] == "/user.slice/agent-600.scope"
    clock.t = T + 30
    _cgroup(cg_root, "cgroup_2")             # the kernel OOM-killed a process in it and it hit memory.max
    c.tick()
    clock.t = T + 60
    shutil.rmtree(cg_root)                   # the cgroup is gone while the process is still listed
    assert c.tick() == []
    clock.t = T + 90
    [run] = c.tick()
    assert run["peak_memory_bytes"] == 3 * 1024 ** 3 and run["cpu_seconds"] == 120.0   # cgroup, not ps
    assert run["exit_reason"] == "oom" and run["hit_limit"] is True
    assert run["ended_at"] == "2025-11-04T09:54:20+00:00"
    assert [e["entity_id"] for e in posted] == [run["session_id"]]
    [prof] = profiles("herdr-engineering")
    assert prof["agent_kind"] == "pi" and prof["metrics"]["cpu_seconds"]["max"] == 120.0


def test_linux_a_shared_cgroup_falls_back_to_process_stats(tmp_path, cp):
    post, *_ = cp
    work = tmp_path / "herdr-engineering"
    work.mkdir()
    cg_root = tmp_path / "cgroup"
    _cgroup(cg_root, "cgroup_1")
    (cg_root / "user.slice/agent-600.scope/cgroup.procs").write_text("600\n601\n650\n")   # sshd too
    clock = Clock()
    c = collector(tmp_path, post, ps_of("linux_ps_1.txt", "linux_ps_2.txt", "linux_ps_3.txt"), clock,
                  {600: str(work)}, linux=True, proc_root=FIX / "linux" / "proc", cgroup_root=cg_root)
    c.tick()
    clock.t = T + 30
    c.tick()
    clock.t = T + 60
    [run] = c.tick()
    assert run["peak_memory_bytes"] == 500000 * KIB and run["cpu_seconds"] == 80.0   # ps: 60 + 20
    assert run["exit_reason"] == "exited" and run["hit_limit"] is False


def test_linux_without_cgroup_v2_uses_process_stats(tmp_path, cp):
    post, *_ = cp
    work = tmp_path / "herdr-engineering"
    work.mkdir()
    clock = Clock()
    c = collector(tmp_path, post, ps_of("linux_ps_1.txt", "linux_ps_3.txt"), clock, {600: str(work)}, linux=True,
                  proc_root=tmp_path / "no-proc", cgroup_root=tmp_path / "no-cgroup")
    c.tick()
    clock.t = T + 30
    [run] = c.tick()
    assert run["cpu_seconds"] == 15.0 and run["peak_memory_bytes"] == 500000 * KIB


def test_cgroup_paths_are_v2_and_never_escape_the_root(tmp_path):
    proc = tmp_path / "proc"
    for pid, text in ((1, "0::/\n"), (2, "12:memory:/x\n"), (3, "0::/../../etc\n"), (4, "0::/a.slice/b.scope\n")):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cgroup").write_text(text)
    assert [rc.cgroup_of(pid, proc) for pid in (1, 2, 3, 4, 5)] == [None, None, None, "/a.slice/b.scope", None]


# --------------------------------------------------------------------------- bounds

def test_disk_walk_is_bounded_by_entries_and_time(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}").write_bytes(b"z" * 10)
    far = time.monotonic() + 60
    assert rc.dir_bytes(str(tmp_path), far) == 50
    assert rc.dir_bytes(str(tmp_path), far, max_entries=3) is None
    assert rc.dir_bytes(str(tmp_path), time.monotonic() - 1) is None
    assert rc.dir_bytes(str(tmp_path / "gone"), far) is None and rc.dir_bytes(None, far) is None


def test_disk_walk_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"q" * 1000)
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / "link").symlink_to(outside, target_is_directory=True)
    (inside / "small").write_bytes(b"s" * 7)
    assert rc.dir_bytes(str(inside), time.monotonic() + 60) == 7


def test_an_exhausted_disk_budget_reports_growth_as_unknown(tmp_path, cp):
    post, *_ = cp
    work = tmp_path / "w"
    work.mkdir()
    clock = Clock()
    c = collector(tmp_path, post, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock, {700: str(work)},
                  disk_budget_s=-1)
    c.tick()
    clock.t = T + 30
    [run] = c.tick()
    assert run["disk_growth_bytes"] is None


def test_sessions_processes_queue_and_delivered_ids_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "MAX_SESSIONS", 1)
    monkeypatch.setattr(rc, "MAX_OUTBOX", 1)
    monkeypatch.setattr(rc, "MAX_TREE", 2)
    monkeypatch.setattr(rc, "MAX_SENT", 2)
    assert rc.process_tree(600, {600: [601, 602], 601: [603]}) == [600, 601]
    work = tmp_path / "w"
    work.mkdir()
    clock = Clock()
    c = collector(tmp_path, lambda e: None, ps_of("macos_ps_1.txt", "macos_ps_2.txt"), clock,
                  {600: str(work), 700: str(work)})
    c.tick()
    [session] = c.sessions.values()          # two agents, one slot
    assert len(session["cpu"]) <= 2
    c.outbox.append({"entity_id": "run-old"})
    c.sessions["run-x"] = {**session, "pid": 700, "agent_kind": "codex", "start": T - 20}
    clock.t = T + 30
    c.tick()
    assert [e["entity_id"] for e in c.outbox] == ["run-x"]     # full: the oldest was dropped
    c.post = lambda e: 200
    c.sent = ["a", "b"]
    assert c.flush() == 1 and c.sent == ["b", "run-x"]


def test_default_project_is_the_main_checkout_for_every_worktree(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    main = tmp_path / "herdr-engineering"
    main.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run([git, "init", "-q", str(main)], check=True, env=env)
    subprocess.run([git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
    tree = tmp_path / ".worktrees" / "feature-x"
    subprocess.run([git, "-C", str(main), "worktree", "add", "-q", str(tree)], check=True, env=env)
    assert rc.default_project_of(str(main)) == "herdr-engineering"
    assert rc.default_project_of(str(tree)) == "herdr-engineering"
    plain = tmp_path / "scratch-dir"
    plain.mkdir()
    assert rc.default_project_of(str(plain)) == "scratch-dir"
