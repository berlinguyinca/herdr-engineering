"""Review round 5 for ``herdr-eng cleanup`` / the reaper. Real git, real processes; ``gh`` is a
real executable on PATH that answers like the GitHub CLI. No mocks."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import (
    Refusal,
    _origin_owner,
    cleanup,
    configured_roots,
)
from herdr_engineering.disk_hygiene import load_policy

from .test_cleanup_command import Env, git
from .test_cleanup_round2 import SESSION, _scratch, _user_config, home  # noqa: F401
from .test_cleanup_round3 import claim_session


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _ignoring(env: Env, name: str, patterns: str) -> Path:
    wt = env.worktree(name)
    (wt / ".gitignore").write_text(patterns)
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-qm", "ignore")
    git(wt, "push", "-q", "origin", "HEAD")
    return wt


def _gh(directory: Path, monkeypatch: pytest.MonkeyPatch, wt: Path, state: str, *, side_effect: str = "") -> None:
    """A ``gh`` on PATH answering ``pr list`` for ``wt``'s head; ``side_effect`` (shell) runs first."""
    head = git(wt, "rev-parse", "HEAD").strip()
    branch = git(wt, "symbolic-ref", "--short", "HEAD").strip()
    reply = [{"state": state, "headRefOid": head, "headRefName": branch, "number": 1,
              "headRepositoryOwner": {"login": _origin_owner(wt)}}]
    directory.mkdir(exist_ok=True)
    shim = directory / "gh"
    shim.write_text(f"#!/bin/sh\n{side_effect}\ncat <<'EOF'\n{json.dumps(reply)}\nEOF\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


# --- 1: a collapsed directory is not rebuildable because of its name ----------------------------------

def test_ignored_secrets_inside_unignored_build_dirs_are_kept(env: Env) -> None:
    wt = _ignoring(env, "collapsed", "*.env\n*.sqlite\n")
    (wt / "dist").mkdir()
    (wt / "dist" / "prod.env").write_text("SECRET=1\n")
    (wt / "build" / "sub").mkdir(parents=True)
    (wt / "build" / "sub" / "local.sqlite").write_text("db")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / "dist" / "prod.env").exists() and (wt / "build" / "sub" / "local.sqlite").exists()


def test_a_directory_ignored_by_its_own_pattern_is_still_rebuildable(env: Env) -> None:
    wt = _ignoring(env, "ownpattern", "dist/\n")
    (wt / "dist").mkdir()
    (wt / "dist" / "bundle.js").write_text("x")
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


# --- 2 + 4 + 5: the CLI path with --merged-ok ------------------------------------------------------------

def test_cli_closed_unmerged_pr_does_not_excuse_unpushed_commits(env: Env, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    wt = env.worktree("closed")
    env.commit(wt)
    _gh(tmp_path / "bin", monkeypatch, wt, "CLOSED")
    with pytest.raises(Refusal, match="unpushed_commits"):
        cleanup("worktree", wt, roots=env.roots, merged_ok=True)
    assert wt.is_dir()


def test_cli_merged_pr_still_excuses_unpushed_commits(env: Env, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    wt = env.worktree("merged")
    env.commit(wt)
    _gh(tmp_path / "bin", monkeypatch, wt, "MERGED")
    assert cleanup("worktree", wt, roots=env.roots, merged_ok=True)["action"] == "removed"


def test_cli_commit_made_during_the_pr_lookup_is_refused(env: Env, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    wt = env.worktree("racing")
    env.commit(wt)
    late = f"git -C {wt} -c user.email=a@b -c user.name=n commit -q --allow-empty -m late"
    _gh(tmp_path / "bin", monkeypatch, wt, "MERGED", side_effect=late)
    with pytest.raises(Refusal, match="changed"):
        cleanup("worktree", wt, roots=env.roots, merged_ok=True)
    assert wt.is_dir()


def test_ignored_file_written_during_the_pr_lookup_is_kept(env: Env, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    wt = _ignoring(env, "lateenv", ".env\n")
    env.commit(wt)
    _gh(tmp_path / "bin", monkeypatch, wt, "MERGED", side_effect=f"echo SECRET=1 > {wt}/.env")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots, merged_ok=True)
    assert (wt / ".env").read_text() == "SECRET=1\n"


# --- 3 + 10: roots are validated and read once ---------------------------------------------------------------

@pytest.mark.parametrize("roots", ['"~/agent-scratch"', '[""]', '["work"]', "[1]"])
def test_bad_roots_fail_closed_on_the_cli_path(env: Env, home: Path, roots: str,  # noqa: F811
                                               capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    from herdr_engineering.cleanup_command import main_args
    wt = env.worktree("rootless")
    _user_config(home, f"disk_hygiene:\n  roots: {roots}\n")

    class A:
        print_run_args = False
        scope = "worktree"
        path = str(wt)
        dry_run = True
        merged_ok = False
        json = True
    assert main_args(A()) == 1  # type: ignore[arg-type]
    assert json.loads(capsys.readouterr().out)["refused"] == "config_unreadable"
    assert wt.is_dir()


@pytest.mark.parametrize("roots", ["~/x", [""], ["work"]])
def test_bad_roots_fail_closed_in_the_reaper(roots: object) -> None:
    policy = load_policy({"roots": roots})
    assert policy.config_error and not (policy.enabled or policy.worktrees)


def test_configured_roots_does_no_config_io_and_lists_each_root_once(home: Path) -> None:  # noqa: F811
    _user_config(home, "disk_hygiene: [unclosed\n")  # would raise if it were read
    roots = configured_roots(["/agent/a"])
    assert roots.count(Path("/agent/a")) == 1


# --- 6: a recycled pid is not our session ---------------------------------------------------------------------

def test_session_file_of_a_recycled_pid_is_not_ours(tmp_path: Path, home: Path) -> None:  # noqa: F811
    root, _, session = _scratch(tmp_path)
    live = tmp_path / "projects"
    live.mkdir()
    (live / f"{SESSION}.jsonl").write_text("{}")
    from .test_cleanup_round3 import process_started
    claim_session(home, SESSION, started_ms=(process_started(os.getppid()) - 86400) * 1000)  # a day older
    with pytest.raises(Refusal, match="live_session"):
        cleanup("tmp", session / "scratchpad", roots=[], temp_roots=[tmp_path], scratch_root=root,
                live_roots=[live])


# --- 7: the user's global core.excludesFile is honoured ------------------------------------------------------

def test_global_excludes_file_is_honoured_and_user_can_list_it_as_rebuildable(
        env: Env, home: Path) -> None:  # noqa: F811
    (home / ".gitignore_global").write_text(".DS_Store\n")
    (home / ".gitconfig").write_text("[core]\n\texcludesFile = ~/.gitignore_global\n")
    wt = env.worktree("finder")
    (wt / ".DS_Store").write_text("x")
    with pytest.raises(Refusal, match="ignored_files"):  # ignored, not 'untracked and dirty'
        cleanup("worktree", wt, roots=env.roots)
    _user_config(home, 'disk_hygiene:\n  rebuildable: [".DS_Store"]\n')
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


def test_git_wrapper_honours_only_the_excludes_file(env: Env, home: Path) -> None:  # noqa: F811
    (home / ".gitconfig").write_text("[status]\n\tshowUntrackedFiles = no\n")
    wt = _ignoring(env, "stillstrict", ".env\n")
    (wt / ".env").write_text("S=1\n")
    (wt / "dist").mkdir()
    (wt / "dist" / "x").write_text("x")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)


# --- 8 + 9: one process listing, one ancestor walk ------------------------------------------------------------

def test_ancestors_match_the_real_process_tree() -> None:
    from herdr_engineering.cleanup_command import _ancestors
    found = _ancestors()
    assert os.getppid() in found and os.getpid() not in found
    assert subprocess.run(["ps", "-o", "ppid=", "-p", str(os.getppid())], capture_output=True,
                          text=True).stdout.strip() in {str(p) for p in found} | {"0", "1"}
