"""Review round 7 for ``herdr-eng cleanup`` / the reaper. Real git, real files, real processes."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import (
    Refusal,
    check_worktree_removable,
    cleanup,
    default_roots,
    default_temp_roots,
    in_use_snapshot,
    settle_worktree,
    worktree_quick_facts,
)
from herdr_engineering.disk_hygiene import Policy, reap_worktrees
from herdr_engineering.scratch_reaper import default_root

from .test_cleanup_command import Env, git
from .test_cleanup_round2 import home  # noqa: F401
from .test_cleanup_round5 import _ignoring


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _nested_repo(directory: Path) -> None:
    directory.mkdir()
    subprocess.run(["git", "init", "-q", str(directory)], check=True)
    (directory / "f").write_text("local work")
    git(directory, "add", "f")
    git(directory, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-qm", "only here")


# --- 1 + 2: junk is a regular FILE, never a directory or a link --------------------------------------------

def test_an_ignored_junk_named_directory_holding_a_repository_is_kept(env: Env, home: Path) -> None:  # noqa: F811
    wt = _ignoring(env, "ignoredjunkdir", "._*\n")
    _nested_repo(wt / "._work")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / "._work" / "f").exists()


def test_an_untracked_junk_named_directory_makes_the_tree_dirty(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("untrackedjunkdir")
    _nested_repo(wt / "._work")
    for dry in (True, False):
        with pytest.raises(Refusal, match="dirty"):
            cleanup("worktree", wt, roots=env.roots, dry_run=dry)
    assert (wt / "._work" / "f").exists()


def test_a_junk_named_symlink_is_not_junk(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("junklink")
    (wt / ".DS_Store").symlink_to(env.repo / "a.txt")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)
    assert (env.repo / "a.txt").exists()


# --- 3 + 4: one scratch root, resolved like Claude Code, passed as a parameter ---------------------------

def test_the_scratch_root_follows_claude_code_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    assert default_root() == Path(os.path.realpath(f"/tmp/claude-{os.getuid()}"))
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(tmp_path))
    (tmp_path / f"claude-{os.getuid()}").mkdir()  # only a real, user-owned directory is honoured
    assert default_root() == Path(os.path.realpath(tmp_path)) / f"claude-{os.getuid()}"


def test_roots_and_temp_roots_take_the_scratch_root_as_a_parameter(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    assert scratch in default_roots(tmp_path, scratch=scratch)
    assert scratch in default_temp_roots(scratch=scratch)
    assert Path(f"/tmp/claude-{os.getuid()}") not in default_roots(tmp_path, scratch=scratch)


def test_the_reaper_takes_the_scratch_root_as_a_parameter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
    scratch = tmp_path / "scratch"
    env = Env(scratch, root=scratch)  # the repo and its worktree both live in the scratch root
    wt = env.worktree("done")
    out = reap_worktrees(Policy(pr_source="git", min_idle_hours=0, ttl_days=0), now=time.time() + 86400,
                         temp_worktree_parents=[], scratch_root=scratch)
    assert [(w["action"], w["reason"]) for r in out["repos"] for w in r["worktrees"]] == \
        [("removed", "orphan_ttl")]
    assert not wt.exists()


# --- 5: the /proc location is a parameter, so fail-closed is testable on Linux too ---------------------------

def test_no_lsof_and_no_proc_fails_closed_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no ps, no lsof
    with pytest.raises(Refusal, match="in_use"):
        in_use_snapshot(proc=tmp_path / "no-such-proc")


# --- 6: the expensive walk runs once -------------------------------------------------------------------------

def test_quick_facts_does_not_walk_rebuildable_trees_but_settle_does(env: Env, home: Path) -> None:  # noqa: F811
    wt = _ignoring(env, "walk", "build/\n")
    _nested_repo(wt / "build")
    facts = worktree_quick_facts(wt, ())  # names only: no inspect_tree walk
    with pytest.raises(Refusal, match="ignored_files"):
        settle_worktree(wt, facts, ())
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)


# --- 8: an empty head never counts as a merged PR head --------------------------------------------------------

def test_an_empty_head_is_never_excused_by_an_empty_pr_head(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("emptyhead")
    env.commit(wt)  # unpushed
    facts = {"head": "", "branch": "emptyhead", "common": str(env.repo / ".git")}
    with pytest.raises(Refusal, match="unpushed_commits"):
        check_worktree_removable(wt, merged_ok=True, pr_head="", prechecked=facts)
