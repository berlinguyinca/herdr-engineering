"""Review round 4 for ``herdr-eng cleanup`` / the reaper. Real git, real processes, real kernel
``/proc`` on Linux. No mocks."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, cleanup, remove_worktree
from herdr_engineering.disk_hygiene import Policy as HygienePolicy
from herdr_engineering.disk_hygiene import (
    PrState,
    load_policy,
    main_args,
    reap_worktrees,
)

from .test_cleanup_command import Env, git
from .test_cleanup_round2 import SESSION, _scratch, _user_config, home  # noqa: F401
from .test_cleanup_round3 import claim_session

DAY = 86400


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _with_env_file(env: Env, name: str) -> Path:
    wt = env.worktree(name)
    (wt / ".gitignore").write_text(".env\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-qm", "ignore")
    git(wt, "push", "-q", "origin", "HEAD")
    (wt / ".env").write_text("SECRET=1\n")
    return wt


# --- 1 + 6: git calls are hardened ------------------------------------------------------------------

def test_hidden_untracked_files_config_does_not_hide_ignored_secrets(env: Env, home: Path) -> None:  # noqa: F811
    wt = _with_env_file(env, "secrets")
    git(env.repo, "config", "status.showUntrackedFiles", "no")  # repo-shared config
    (home / ".gitconfig").write_text("[status]\n\tshowUntrackedFiles = no\n")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / ".env").read_text() == "SECRET=1\n"


def test_exported_git_dir_cannot_redirect_the_checks(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    wt = env.worktree("dirty-one")
    (wt / "x").write_text("x")
    other = env.worktree("clean-other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)
    assert wt.is_dir() and other.is_dir()


# --- 2: rebuildable is validated on the CLI path too --------------------------------------------------

def test_scalar_rebuildable_fails_closed_on_the_cli_path(env: Env, home: Path) -> None:  # noqa: F811
    wt = _with_env_file(env, "scalar")
    _user_config(home, 'disk_hygiene:\n  rebuildable: "*.log"\n')
    with pytest.raises(Refusal, match="config_unreadable"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / ".env").exists()


# --- 3: the PR head must still be the HEAD being removed ----------------------------------------------

def test_commit_made_after_the_pr_lookup_is_not_excused(env: Env) -> None:
    wt = env.worktree("late")
    looked_up = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True,
                               text=True, check=True).stdout.strip()
    env.commit(wt)  # someone commits while gh was being asked
    with pytest.raises(Refusal, match="unpushed_commits"):
        remove_worktree(wt, dry_run=True, merged_ok=True, pr_head=looked_up)
    assert wt.is_dir()


def test_moved_head_is_refused_between_inspection_and_removal(env: Env) -> None:
    from herdr_engineering.cleanup_command import worktree_local_checks
    wt = env.worktree("moved")
    facts = worktree_local_checks(wt, ())
    env.commit(wt)
    git(wt, "push", "-q", "origin", "HEAD")
    with pytest.raises(Refusal, match="changed"):
        remove_worktree(wt, dry_run=True, merged_ok=False, prechecked=facts)


# --- 4: unknown keys fail closed ------------------------------------------------------------------------

@pytest.mark.parametrize("cfg", [{"enable": False}, {"worktrees": {"enabeld": False}},
                                 {"docker": {"enable": False}}, {"docker": {"builder_keep_storage": 5}},
                                 {"docker": {"builder_keep_storage": "lots"}}])
def test_misspelled_keys_fail_closed(cfg: dict) -> None:
    policy = load_policy(cfg)
    assert policy.config_error and not (policy.enabled or policy.worktrees or policy.docker)


def test_known_keys_still_load() -> None:
    policy = load_policy({"docker": {"enabled": True, "builder_prune": True, "builder_keep_storage": "10GB"},
                          "rebuildable": ["*.cache"], "worktrees": {"enabled": False}})
    assert not policy.config_error and policy.docker and not policy.worktrees
    assert policy.rebuildable == ("*.cache",)


# --- 5: own-session identity is an ancestor process, not an env var ----------------------------------------

def test_forged_session_env_var_does_not_exempt_a_live_session(tmp_path: Path, home: Path,  # noqa: F811
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, session = _scratch(tmp_path)
    live = tmp_path / "projects"
    live.mkdir()
    (live / f"{SESSION}.jsonl").write_text("{}")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    with pytest.raises(Refusal, match="live_session"):
        cleanup("tmp", session / "scratchpad", roots=[], temp_roots=[tmp_path], scratch_root=root,
                live_roots=[live])
    # a sessions file for a pid that is NOT our ancestor is no identity either
    stranger = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "sessions" / f"{stranger.pid}.json").write_text(
            json.dumps({"pid": stranger.pid, "sessionId": SESSION}))
        with pytest.raises(Refusal, match="live_session"):
            cleanup("tmp", session / "scratchpad", roots=[], temp_roots=[tmp_path], scratch_root=root,
                    live_roots=[live])
    finally:
        stranger.kill()
        stranger.wait()
    claim_session(home, SESSION)
    out = cleanup("tmp", session / "scratchpad", roots=[], temp_roots=[tmp_path], scratch_root=root,
                  live_roots=[live])
    assert out["action"] == "removed"


# --- 7: one local pass per removable worktree -------------------------------------------------------------

def test_reaper_inspects_a_removable_worktree_once(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env.worktree("once")
    real = shutil.which("git")
    assert real
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "git-calls.log"
    wrapper = bindir / "git"  # a recording pass-through to the real git
    wrapper.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexec {real} "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    for tool in ("ps", "lsof"):
        if found := shutil.which(tool):
            (bindir / tool).symlink_to(found)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    out = reap_worktrees(HygienePolicy(pr_source="git", min_idle_hours=0, ttl_days=0), extra_repos=[env.repo],
                         extra_roots=[str(env.roots[0])], now=time.time() + DAY)
    assert [w["action"] for r in out["repos"] for w in r["worktrees"]] == ["removed"]
    assert sum("--ignored" in line for line in log.read_text().splitlines()) == 1


# --- 8: a broken config is loud ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["run", "status"])
def test_broken_config_exits_nonzero_and_says_why(home: Path, capsys: pytest.CaptureFixture[str],  # noqa: F811
                                                  monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    _user_config(home, "disk_hygiene:\n  enable: false\n")
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(home / "state"))

    class A:
        disk_hygiene_command = command
        only = None
        dry_run = True
        json = True
        repo: list[str] = []
        ttl_days = None
        pr_source = None
    assert main_args(A()) == 1  # type: ignore[arg-type]
    out = json.loads(capsys.readouterr().out)
    assert "unknown key" in (out.get("config_error") or out.get("reason"))


# --- 9: only a MERGED PR excuses unpushed commits -----------------------------------------------------------

def test_closed_unmerged_pr_does_not_excuse_unpushed_commits() -> None:
    from herdr_engineering.disk_hygiene import unpushed_ok
    assert unpushed_ok(PrState.MERGED)
    for state in (PrState.CLOSED, PrState.OPEN, PrState.NONE, PrState.UNKNOWN):
        assert not unpushed_ok(state)


# --- 10: the /proc fallback against the real kernel -----------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real Linux /proc")
def test_real_proc_fallback_sees_a_process_inside_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from herdr_engineering.cleanup_command import in_use_snapshot
    target = tmp_path / "busy"
    target.mkdir()
    free = tmp_path / "free"
    free.mkdir()
    holder = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], cwd=target)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("ps", "git"):  # lsof stays hidden
        if found := shutil.which(tool):
            (bindir / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(bindir))
    try:
        assert shutil.which("lsof") is None
        snapshot = in_use_snapshot()
        assert snapshot.uses(target) and not snapshot.uses(free)
        with pytest.raises(Refusal, match="in_use"):
            cleanup("tmp", target, roots=[], temp_roots=[tmp_path], scratch_root=tmp_path / "claude-0")
        assert target.is_dir()
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.skipif(Path("/proc").is_dir(), reason="a real /proc is a valid source")
def test_no_lsof_and_no_proc_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from herdr_engineering.cleanup_command import in_use_snapshot
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(Refusal, match="in_use"):
        in_use_snapshot()
