import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE, REPO_ID, git, write  # noqa: E402

from herdr_engineering.architecture import analyze, gitsource, runner  # noqa: E402


def snap(repo, rev="HEAD", host="h1", **kw):
    return analyze.analyze_commit(str(repo), rev, host, REPO_ID, **kw)


def test_dirty_worktree_never_leaks_into_snapshot(repo):
    clean = snap(repo)
    write(repo, {"app/core.py": "def uncommitted(): pass\n", "app/new.py": "x = 1\n"})
    dirty = snap(repo)
    assert clean["snapshot"]["graph_hash"] == dirty["snapshot"]["graph_hash"]
    assert not [n for n in dirty["graph"]["nodes"] if "uncommitted" in n["node_id"] or n["node_id"] == "module:app.new"]


def test_same_commit_two_worktrees_same_graph_hash_different_snapshot_ids(repo, tmp_path):
    wt = tmp_path / "wt2"
    git(repo, "worktree", "add", "-q", "-b", "other", str(wt))
    a, b = snap(repo), snap(wt)
    assert a["snapshot"]["graph_hash"] == b["snapshot"]["graph_hash"]
    assert a["snapshot"]["worktree_id"] != b["snapshot"]["worktree_id"]
    assert a["snapshot"]["snapshot_id"] != b["snapshot"]["snapshot_id"]


def test_parallel_worktrees_on_different_commits_are_isolated_and_correct(repo, tmp_path):
    wt = tmp_path / "wt2"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    write(wt, {"app/extra.py": "def only_in_feature(): pass\n"})
    git(wt, "add", "-A"); git(wt, "commit", "-q", "-m", "feature")
    results = {}

    def go(name, path):
        results[name] = snap(path)

    ts = [threading.Thread(target=go, args=a) for a in (("main", repo), ("feat", wt)) * 3]
    [t.start() for t in ts]; [t.join() for t in ts]
    ids = lambda r: {n["node_id"] for n in r["graph"]["nodes"]}  # noqa: E731
    assert "function:app.extra.only_in_feature" in ids(results["feat"])
    assert "function:app.extra.only_in_feature" not in ids(results["main"])
    assert results["main"]["snapshot"]["commit_sha"] != results["feat"]["snapshot"]["commit_sha"]


def test_branch_name_is_not_part_of_identity(repo):
    a = snap(repo)["snapshot"]
    git(repo, "branch", "renamed")
    assert snap(repo, "renamed")["snapshot"]["snapshot_id"] == a["snapshot_id"]


def test_config_change_changes_snapshot_id_not_graph_hash_when_output_equal(repo):
    a, b = snap(repo)["snapshot"], snap(repo, max_files=19_999)["snapshot"]
    assert a["config_hash"] != b["config_hash"] and a["snapshot_id"] != b["snapshot_id"]


def test_malicious_repo_symlinks_submodules_and_config_hooks(repo, tmp_path):
    marker = tmp_path / "PWNED"
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'hunter2'\n")
    os.symlink(secret, repo / "leak.py")                       # symlink to a file outside the repo
    os.symlink("/etc", repo / "etc_link")
    write(repo, {"evil.py": f"import os\nos.system('touch {marker}')\nraise SystemExit\n",
                 "conftest.py": f"open({str(marker)!r}, 'w')\n"})
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "evil")
    # attacker-controlled .git/config: hooks, fsmonitor, a custom pager
    with open(repo / ".git" / "config", "a") as f:
        f.write(f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = {tmp_path}\n\tpager = touch {marker}\n")
    (tmp_path / "post-checkout").write_text(f"#!/bin/sh\ntouch {marker}\n")
    out = snap(repo)
    assert not marker.exists(), "analysed code or repo config executed"
    ids = {n["node_id"] for n in out["graph"]["nodes"]}
    assert "module:evil" in ids and "module:leak" not in ids and "hunter2" not in str(out)
    assert any(d["code"] == "skipped_symlinks" and d["count"] == 2 for d in out["graph"]["diagnostics"])


def test_path_traversal_names_cannot_become_node_paths(repo):
    out = snap(repo)
    for n in out["graph"]["nodes"]:
        p = n["relative_path"] or ""
        assert not p.startswith("/") and ".." not in p.split("/")


def test_revision_argument_injection_refused(repo):
    with pytest.raises(gitsource.GitSourceError):
        gitsource.resolve_commit(str(repo), "--output=/tmp/x")


def test_oversize_file_skipped_and_marked_partial(repo):
    write(repo, {"big.py": "x = 1\n" * 50_000})
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "big")
    out = snap(repo, max_file_bytes=1000)
    assert out["snapshot"]["completeness"] == "partial"
    assert "module:big" not in {n["node_id"] for n in out["graph"]["nodes"]}


# ---------------------------------------------------------------- sandboxed runner
def test_runner_end_to_end_matches_in_process(repo):
    iso = runner.run_isolated(str(repo), "HEAD", "h1", REPO_ID)
    assert iso["snapshot"]["graph_hash"] == snap(repo)["snapshot"]["graph_hash"]
    assert isinstance(iso["sandbox"]["network_isolated"], bool)


def _fake_worker(tmp_path, body):
    p = tmp_path / "w.py"
    p.write_text(body)
    return str(p)


def test_runner_wall_clock_timeout_kills_child_tree(repo, tmp_path, monkeypatch):
    marker = tmp_path / "still-alive"
    monkeypatch.setattr(runner, "_WORKER", _fake_worker(tmp_path, f"""
import subprocess, time, sys
code = "import time,pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).touch()"
subprocess.Popen([sys.executable, "-c", code])
time.sleep(60)
"""))
    t = time.time()
    with pytest.raises(runner.AnalysisError, match="wall-clock"):
        runner.run_isolated(str(repo), "HEAD", "h", REPO_ID, limits=runner.Limits(wall_seconds=0.5))
    assert time.time() - t < 30   # the worker sleeps 60s; generous so a loaded host does not flake
    time.sleep(2.5)
    assert not marker.exists(), "grandchild survived the kill"


def test_runner_cancellation(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_WORKER", _fake_worker(tmp_path, "import time; time.sleep(60)"))
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    with pytest.raises(runner.AnalysisCancelled):
        runner.run_isolated(str(repo), "HEAD", "h", REPO_ID, cancel=ev, limits=runner.Limits(wall_seconds=30))


def test_runner_memory_limit_and_scrubbed_env(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "leak-me")
    monkeypatch.setattr(runner, "_WORKER", _fake_worker(tmp_path, "x = bytearray(1 << 30)"))
    with pytest.raises(runner.AnalysisError):
        runner.run_isolated(str(repo), "HEAD", "h", REPO_ID, limits=runner.Limits(memory_bytes=256 * 1024**2))
    monkeypatch.setattr(runner, "_WORKER", _fake_worker(tmp_path, "import os,sys,json;sys.stdout.write(json.dumps(dict(os.environ)))"))
    env = runner.run_isolated(str(repo), "HEAD", "h", REPO_ID)
    env.pop("sandbox")
    assert "SECRET_TOKEN" not in env and set(env) <= {"PATH", "HOME", "LC_ALL", "GIT_CONFIG_NOSYSTEM",
                                                      "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "LANG", "PWD"}


def test_runner_output_cap(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_OUTPUT", 1000)
    monkeypatch.setattr(runner, "_WORKER", _fake_worker(tmp_path, "print('{\"a\":\"' + 'x'*5000 + '\"}')"))
    with pytest.raises(runner.AnalysisError, match="size limit"):
        runner.run_isolated(str(repo), "HEAD", "h", REPO_ID)


# ------------------------------------------------------------------ the sandbox really cuts the network
needs_sandbox = pytest.mark.skipif(runner._mechanism() is None, reason="no unprivileged network sandbox on this host")


@needs_sandbox
def test_sandbox_blocks_a_connection_to_a_listener_the_parent_can_reach(tmp_path):
    import socket
    import subprocess
    import sys
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    port = srv.getsockname()[1]
    probe = f"import socket,sys\ntry:\n socket.create_connection(('127.0.0.1',{port}),timeout=2)\nexcept OSError: sys.exit(7)\n"
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0          # reachable without the sandbox
    prefix, mech = runner._sandbox_prefix(str(tmp_path))
    assert mech and subprocess.run([*prefix, sys.executable, "-c", probe]).returncode == 7
    srv.close()


@needs_sandbox
def test_bwrap_makes_everything_but_scratch_and_cache_read_only(repo, tmp_path):
    if runner._mechanism() != "bwrap":
        pytest.skip("read-only filesystem is a bwrap property")
    import subprocess
    import sys
    scratch, cache = tmp_path / "s", tmp_path / "c"
    scratch.mkdir(); cache.mkdir()
    prefix, _ = runner._sandbox_prefix(str(scratch), (str(cache),))
    def can_write(path):
        code = f"import pathlib,sys\ntry:\n pathlib.Path({str(path)!r}).write_text('x')\nexcept OSError: sys.exit(9)\n"
        return subprocess.run([*prefix, sys.executable, "-c", code]).returncode == 0
    assert can_write(scratch / "f") and can_write(cache / "f")
    assert not can_write(repo / "planted") and not can_write(tmp_path / "elsewhere")


def test_sandbox_report_matches_what_ran(repo):
    res = runner.run_isolated(str(repo), "HEAD", "h", REPO_ID)
    sb = res["sandbox"]
    assert sb["network_isolated"] == (runner._mechanism() is not None)
    assert sb["mechanism"] == runner._mechanism() and sb["read_only_filesystem"] == (sb["mechanism"] == "bwrap")


@needs_sandbox
def test_analysis_with_a_cache_works_inside_the_sandbox_and_fills_the_cache(repo, tmp_path):
    cache = tmp_path / "cache"
    first = runner.run_isolated(str(repo), "HEAD", "h", REPO_ID, config={"cache_dir": str(cache)})
    assert first["sandbox"]["network_isolated"] and any(cache.iterdir())
    second = runner.run_isolated(str(repo), "HEAD", "h", REPO_ID, config={"cache_dir": str(cache)})
    assert second["snapshot"]["graph_hash"] == first["snapshot"]["graph_hash"]
    assert second["snapshot"]["reuse"]["reused"] > 0
