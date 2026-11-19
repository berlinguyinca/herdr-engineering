"""WIP commit helper: real git repos with a real bare remote."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from herdr_engineering.handoff_controller.wip import WipResult, wip_commit


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path: Path, branch: str = "feat/x") -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.name", "T")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "commit.gpgsign", "false")
    (work / "a.txt").write_text("one\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")
    if branch != "main":
        git(work, "checkout", "-q", "-b", branch)
    return work


def remote_heads(tmp_path: Path) -> str:
    return git(tmp_path / "remote.git", "branch", "--list")


def test_clean_tree_no_commit(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    head = git(work, "rev-parse", "HEAD")
    result = wip_commit(work, push_allowed=True)
    assert result == WipResult(committed=False, sha=None, pushed=False, unpushed=(), untracked=())
    assert git(work, "rev-parse", "HEAD") == head


def test_tracked_change_committed_and_pushed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    cwd = os.getcwd()
    result = wip_commit(work, push_allowed=True)
    assert os.getcwd() == cwd
    assert result.committed and result.pushed and result.unpushed == ()
    assert result.sha == git(work, "rev-parse", "--short", "HEAD")
    assert git(work, "log", "-1", "--format=%s") == "wip: auto-handoff checkpoint"
    assert "feat/x" in remote_heads(tmp_path)
    assert git(tmp_path / "remote.git", "rev-parse", "--short", "feat/x") == result.sha


def test_push_not_allowed_records_unpushed_sha(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=False)
    assert result.committed and not result.pushed
    assert result.unpushed == (result.sha,)
    assert "feat/x" not in remote_heads(tmp_path)


def test_never_commits_on_main(tmp_path: Path) -> None:
    work = make_repo(tmp_path, branch="main")
    head = git(work, "rev-parse", "HEAD")
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and not result.pushed and result.sha is None
    assert git(work, "rev-parse", "HEAD") == head
    assert git(work, "status", "--porcelain") == "M a.txt"


def test_detached_head_is_treated_like_main(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "checkout", "-q", "--detach")
    (work / "a.txt").write_text("two\n")
    assert not wip_commit(work, push_allowed=True).committed


def test_untracked_files_not_committed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "a.txt").write_text("two\n")
    (work / "new.txt").write_text("n\n")
    result = wip_commit(work, push_allowed=True)
    assert result.committed and result.untracked == ("new.txt",)
    assert "new.txt" not in git(work, "show", "--name-only", "--format=", "HEAD")


def test_untracked_only_makes_no_commit(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    (work / "new.txt").write_text("n\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and result.untracked == ("new.txt",)


def test_failed_push_is_reported_not_raised(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert result.committed and not result.pushed and result.unpushed == (result.sha,)


def test_pushes_to_differently_named_upstream(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    git(work, "push", "-q", "origin", "feat/x:remote-name")
    git(work, "branch", "--set-upstream-to=origin/remote-name")
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert result.pushed
    assert git(tmp_path / "remote.git", "rev-parse", "--short", "remote-name") == result.sha


def test_rejecting_hook_reports_not_committed(tmp_path: Path) -> None:
    work = make_repo(tmp_path)
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (work / "a.txt").write_text("two\n")
    result = wip_commit(work, push_allowed=True)
    assert not result.committed and not result.pushed
