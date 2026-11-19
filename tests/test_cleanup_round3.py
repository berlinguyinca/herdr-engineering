"""Review round 3 for ``herdr-eng cleanup`` / the reaper. Real git, real processes, real
directory trees. No mocks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, cleanup
from herdr_engineering.disk_hygiene import Policy, load_policy, main_args, reap_worktrees, run

from .test_cleanup_command import Env, git
from .test_cleanup_round2 import SESSION, _holder, _scratch, _user_config, git_shim, home  # noqa: F401, F811

DAY = 86400


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def process_started(pid: int) -> float:
    """Start time of a live process, epoch seconds (what ``ps`` reports)."""
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C"}, check=True).stdout.strip()
    return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))


def claim_session(home: Path, session: str, *, started_ms: float | None = None) -> None:  # noqa: F811
    """What Claude Code does: ``~/.claude/sessions/<pid>.json`` for its own pid. Our parent
    process is a real ancestor, so it stands in for the Claude process."""
    pid = os.getppid()
    directory = home / ".claude" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    started = process_started(pid) * 1000 + 2000 if started_ms is None else started_ms
    (directory / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": session, "startedAt": started}))


def _tmp_cleanup(tmp_path: Path, target: Path, root: Path, *, live: Path | None = None,
                 now: float | None = None) -> dict:
    live_dir = live or tmp_path / "no-sessions"
    live_dir.mkdir(exist_ok=True)
    return cleanup("tmp", target, roots=[], temp_roots=[tmp_path], scratch_root=root,
                   live_roots=[live_dir], now=now)


# --- 1: case-insensitive filesystems -----------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(str(Path("/tmp")).upper()), reason="case-sensitive filesystem")
def test_scratch_guard_applies_to_a_differently_cased_root(tmp_path: Path) -> None:
    root, project, session = _scratch(tmp_path)
    live = tmp_path / "projects"
    live.mkdir()
    (live / f"{SESSION}.jsonl").write_text("{}")
    upper = tmp_path / root.name.upper()
    for victim in (upper, upper / project.name.upper(), upper / project.name / SESSION.upper()):
        assert victim.exists()
        with pytest.raises(Refusal, match="scratch_structure|is_root"):
            _tmp_cleanup(tmp_path, victim, root, live=live)
    with pytest.raises(Refusal, match="live_session"):
        _tmp_cleanup(tmp_path, upper / project.name / SESSION.upper() / "scratchpad", root, live=live)
    assert (session / "scratchpad" / "a.txt").exists()


# --- 2: own session vs other sessions ------------------------------------------------------------

def test_agent_may_clean_inside_its_own_live_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     home: Path) -> None:  # noqa: F811
    root, _, session = _scratch(tmp_path)
    live = tmp_path / "projects"
    live.mkdir()
    (live / f"{SESSION}.jsonl").write_text("{}")  # fresh journal: live
    out_dir = session / "scratchpad" / "build-out"
    out_dir.mkdir()
    with pytest.raises(Refusal, match="live_session"):
        _tmp_cleanup(tmp_path, out_dir, root, live=live)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)  # an env var is not an identity
    with pytest.raises(Refusal, match="live_session"):
        _tmp_cleanup(tmp_path, out_dir, root, live=live)
    claim_session(home, SESSION)
    assert _tmp_cleanup(tmp_path, out_dir, root, live=live)["action"] == "removed"
    with pytest.raises(Refusal, match="scratch_structure"):  # still never the session itself
        _tmp_cleanup(tmp_path, session, root, live=live)


def test_other_sessions_need_the_reaper_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, session = _scratch(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)  # ignored: not backed by an ancestor process
    target = session / "scratchpad"
    with pytest.raises(Refusal, match="session_recent"):  # not live, but idle < 48 h
        _tmp_cleanup(tmp_path, target, root)
    assert target.exists()
    assert _tmp_cleanup(tmp_path, target, root, now=time.time() + 3 * DAY)["action"] == "removed"


# --- 3: nested repositories inside "rebuildable" directories -------------------------------------

def test_nested_unpushed_repo_in_venv_blocks_removal(env: Env) -> None:
    wt = env.worktree("nested")
    (wt / ".gitignore").write_text(".venv\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-qm", "ignore")
    git(wt, "push", "-q", "origin", "HEAD")
    pkg = wt / ".venv" / "src" / "pkg"
    pkg.mkdir(parents=True)
    git(pkg, "init", "-q")
    (pkg / "f").write_text("x")
    git(pkg, "add", "f")
    git(pkg, "commit", "-qm", "local fix")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (pkg / "f").exists()
    shutil.rmtree(pkg / ".git")  # a plain rebuildable directory is fine
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


# --- 4: strict opt-out parsing ---------------------------------------------------------------------

@pytest.mark.parametrize("cfg,field", [({"docker": False}, "docker"), ({"worktrees": False}, "worktrees"),
                                       ({"docker": {"enabled": False}}, "docker")])
def test_scalar_false_opts_out(cfg: dict, field: str) -> None:
    assert getattr(load_policy(cfg), field) is False
    assert load_policy({"docker": True}).docker is True


@pytest.mark.parametrize("cfg", [{"enabled": "false"}, {"worktree_ttl_days": "7d"}, {"pr_source": "github"},
                                 {"docker": "no"}, {"repos": "x"}, {"worktree_ttl_days": -1},
                                 {"min_idle_hours": True}])
def test_malformed_values_fail_closed_not_open(cfg: dict) -> None:
    policy = load_policy(cfg)
    assert policy.config_error and not (policy.enabled or policy.worktrees or policy.docker)
    assert run(policy)["status"] == "config_unreadable"


# --- 5: in-use is re-checked right before each removal ---------------------------------------------

def test_reaper_rechecks_in_use_per_removal(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env.worktree("a-first")
    second = env.worktree("b-second")
    pidfile = tmp_path / "holder.pid"
    marker = tmp_path / "spawned"
    # while the reaper inspects the FIRST worktree, a process starts inside the SECOND
    git_shim(tmp_path / "shim", monkeypatch,
             f"if [ ! -e {marker} ]; then touch {marker}; "
             f"(cd {second} && exec sleep 120) >/dev/null 2>&1 </dev/null & echo $! > {pidfile}; fi")
    try:
        out = reap_worktrees(Policy(pr_source="git", min_idle_hours=0, ttl_days=0), extra_repos=[env.repo],
                             extra_roots=[str(env.roots[0])], now=time.time() + DAY)
    finally:
        if pidfile.exists():
            os.kill(int(pidfile.read_text()), 9)
    rows = {Path(w["path"]).name: w for r in out["repos"] for w in r["worktrees"]}
    assert rows["b-second"]["action"] == "kept" and rows["b-second"]["reason"] == "in_use"
    assert second.exists()


# --- 7: read-only directories are verified before anything is deleted ------------------------------

def test_tmp_scope_refuses_readonly_subdir_before_deleting(tmp_path: Path) -> None:
    root = tmp_path / "scratch-parent"
    victim = root / "gomod"
    (victim / "pkg" / "mod").mkdir(parents=True)
    (victim / "pkg" / "mod" / "f").write_text("x")
    (victim / "writable.txt").write_text("w")
    os.chmod(victim / "pkg" / "mod", 0o555)
    try:
        with pytest.raises(Refusal, match="unverifiable"):
            cleanup("tmp", victim, roots=[], temp_roots=[root], scratch_root=tmp_path / "claude-0")
        assert (victim / "writable.txt").exists() and (victim / "pkg" / "mod" / "f").exists()
    finally:
        os.chmod(victim / "pkg" / "mod", 0o755)


# --- 8: --ttl-days 0 ---------------------------------------------------------------------------------

def test_ttl_days_zero_is_honoured(env: Env, home: Path, capsys: pytest.CaptureFixture[str],  # noqa: F811
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    env.worktree("orphan")
    _user_config(home, f"disk_hygiene:\n  roots: ['{env.roots[0]}']\n  repos: ['{env.repo}']\n"
                       "  pr_source: git\n  min_idle_hours: 0\n  docker: {enabled: false}\n")

    class A:
        disk_hygiene_command = "run"
        only = "worktrees"
        dry_run = True
        json = True
        repo: list[str] = []
        ttl_days = 0.0
        pr_source = None
    assert main_args(A()) == 0  # type: ignore[arg-type]
    rows = [w for r in json.loads(capsys.readouterr().out)["jobs"][0]["repos"] for w in r["worktrees"]]
    assert rows and rows[0]["action"] == "would_remove"


# --- 9: cheap local refusals come before the network PR lookup ----------------------------------------

def test_dirty_worktree_refused_before_any_pr_lookup(env: Env, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    wt = env.worktree("dirty-first")
    (wt / "x").write_text("x")
    git_bin = Path(shutil.which("git") or "git")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").symlink_to(git_bin)
    for tool in ("ps", "lsof"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(bindir))  # real environment without gh
    out = reap_worktrees(Policy(pr_source="gh", min_idle_hours=0), extra_repos=[env.repo],
                         extra_roots=[str(env.roots[0])], now=time.time() + DAY)
    rows = [w for r in out["repos"] for w in r["worktrees"]]
    assert rows[0]["reason"] == "dirty"  # not pr_unknown
