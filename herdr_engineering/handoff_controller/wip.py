"""Checkpoint a worktree before handoff: commit tracked changes on a feature branch, push when allowed."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .proc import TIMED_OUT, run_group

PROTECTED_BRANCHES = frozenset({"main", "master"})
COMMIT_MESSAGE = "wip: auto-handoff checkpoint"


class WipReason(StrEnum):
    ON_MAIN = "on_main"  # main or master: nothing committed
    DETACHED = "detached"  # detached HEAD: nothing committed
    COMMIT_REJECTED = "commit_rejected"  # hook, identity or any other commit failure; `error` has the first stderr line
    PUSH_FAILED = "push_failed"
    PUSH_NOT_ALLOWED = "push_not_allowed"  # committed, or commits unpushed, and the caller did not allow a push
    GIT_ERROR = "git_error"  # a git command failed: the state is unknown, see `error`
    MERGE_IN_PROGRESS = "merge_in_progress"  # a merge, cherry-pick, revert or rebase is unfinished: nothing staged
    NOTHING_STAGEABLE = "nothing_stageable"  # tracked changes the status shows, but `git add -u` stages none (a dirty submodule)


@dataclass(frozen=True)
class WipResult:
    committed: bool
    sha: str | None
    pushed: bool
    unpushed: tuple[str, ...]
    untracked: tuple[str, ...] = ()
    reason: WipReason | None = None  # None: clean tree, or committed and pushed
    error: str | None = None  # set when `reason` is a failure; treat the repository state as unknown for GIT_ERROR


_TIMED_OUT = TIMED_OUT
_UNFINISHED = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _git(worktree: Path, timeout_s: float, *args: str) -> subprocess.CompletedProcess[str]:
    """Never raises: a timeout is returncode 124 (the whole process group is killed), a missing git 127."""
    return run_group(["git", "-C", str(worktree), *args], timeout_s)


def _timed_out(proc: subprocess.CompletedProcess[str]) -> bool:
    return proc.returncode == _TIMED_OUT


def _describe(proc: subprocess.CompletedProcess[str]) -> str:
    stderr = proc.stderr.strip().splitlines() if isinstance(proc.stderr, str) else []
    first = stderr[0] if stderr else "no output"
    return f"git {' '.join(proc.args[3:5])} exit {proc.returncode}: {first}"


def _unpushed(worktree: Path, timeout_s: float) -> tuple[tuple[str, ...], str | None]:
    proc = _git(worktree, timeout_s, "rev-list", "--abbrev-commit", "HEAD", "--not", "--remotes")
    if proc.returncode != 0:
        return (), _describe(proc)
    return tuple(proc.stdout.split()), None


def _config(worktree: Path, timeout_s: float, key: str) -> tuple[str, str | None]:
    """(value, error): an unset key is ("", None), a failed lookup is ("", description)."""
    proc = _git(worktree, timeout_s, "config", "--get", key)
    if proc.returncode == 0:
        return proc.stdout.strip(), None
    if proc.returncode == 1 and not proc.stdout.strip():
        return "", None
    return "", _describe(proc)


def _push(worktree: Path, branch: str, timeout_s: float) -> tuple[WipReason | None, str | None]:
    """(None, None) when pushed; otherwise why not. A failed config lookup never skips the protected-upstream check."""
    remote, err = _config(worktree, timeout_s, f"branch.{branch}.remote")
    if err:
        return WipReason.GIT_ERROR, err
    merge, err = _config(worktree, timeout_s, f"branch.{branch}.merge")
    if err:
        return WipReason.GIT_ERROR, err
    if remote and merge:
        target = merge.removeprefix("refs/heads/")
        if target in PROTECTED_BRANCHES:
            return WipReason.PUSH_FAILED, f"upstream {remote}/{target} is protected"
        proc = _git(worktree, timeout_s, "push", remote, f"HEAD:{target}")
    else:
        proc = _git(worktree, timeout_s, "push", "origin", branch)
    if proc.returncode == 0:
        return None, None
    return (WipReason.GIT_ERROR if _timed_out(proc) else WipReason.PUSH_FAILED), _describe(proc)


def _lock_note(worktree: Path, timeout_s: float) -> str:
    gitdir = _git(worktree, timeout_s, "rev-parse", "--git-dir")
    if gitdir.returncode != 0:
        return "index.lock state unknown"
    lock = (worktree / gitdir.stdout.strip() / "index.lock")  # absolute when git prints one
    return "index.lock exists (not removed)" if lock.exists() else "no index.lock"


def _unfinished(worktree: Path, timeout_s: float) -> bool | None:
    """True while a merge, cherry-pick, revert or rebase is unfinished or an index entry is unmerged; None when git cannot say."""
    gitdir = _git(worktree, timeout_s, "rev-parse", "--absolute-git-dir")
    unmerged = _git(worktree, timeout_s, "ls-files", "-u")
    if gitdir.returncode != 0 or unmerged.returncode != 0:
        return None
    return bool(unmerged.stdout.strip()) or any((Path(gitdir.stdout.strip()) / name).exists() for name in _UNFINISHED)


def wip_commit(worktree: Path, *, push_allowed: bool, timeout_s: float = 120.0) -> WipResult:
    """Commit tracked changes (never untracked files, never on main/master or a detached HEAD); push if allowed.

    `git add -u` adds tracked changes only, but anything the user already staged (new files included) is part of
    the commit. Never raises and never forces a push: a failure is in `reason` and `error`. `reason` is None only
    when every git call the result depends on succeeded; a failing or timed-out call gives GIT_ERROR (state
    unknown), never an empty-looking result. A stale `.git/index.lock` is reported, never removed.
    """
    def failure(proc: subprocess.CompletedProcess[str]) -> WipResult:
        return WipResult(False, None, False, (), (), WipReason.GIT_ERROR, _describe(proc))

    head = _git(worktree, timeout_s, "symbolic-ref", "--short", "-q", "HEAD")
    if head.returncode not in (0, 1):  # 1 with -q: HEAD is detached
        if "not a git repository" in head.stderr:  # no repository: nothing to commit, nothing to push
            return WipResult(False, None, False, ())
        return failure(head)
    branch = head.stdout.strip()
    others = _git(worktree, timeout_s, "ls-files", "--others", "--exclude-standard", "-z")
    if others.returncode != 0:
        return failure(others)
    untracked = tuple(name for name in others.stdout.split("\0") if name)
    unpushed, err = _unpushed(worktree, timeout_s)
    if err:
        return WipResult(False, None, False, (), untracked, WipReason.GIT_ERROR, err)
    if not branch or branch in PROTECTED_BRANCHES:
        return WipResult(False, None, False, unpushed, untracked, WipReason.DETACHED if not branch else WipReason.ON_MAIN)
    status = _git(worktree, timeout_s, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return failure(status)
    unfinished = _unfinished(worktree, timeout_s)
    if unfinished is None:
        return WipResult(False, None, False, unpushed, untracked, WipReason.GIT_ERROR, "git rev-parse/ls-files failed")
    if unfinished:  # `git add -u` would stage conflicted files and commit them with their markers
        return WipResult(False, None, False, unpushed, untracked, WipReason.MERGE_IN_PROGRESS)
    committed = False
    sha: str | None = None
    if status.stdout.strip():
        before = _git(worktree, timeout_s, "rev-parse", "HEAD")
        added = _git(worktree, timeout_s, "add", "-u")
        if added.returncode == 0:
            staged = _git(worktree, timeout_s, "diff", "--cached", "--quiet")  # 0: nothing staged, 1: something is
            if staged.returncode == 0:
                return WipResult(False, None, False, unpushed, untracked, WipReason.NOTHING_STAGEABLE)
            if staged.returncode != 1:
                return failure(staged)
        done = added if added.returncode != 0 else _git(worktree, timeout_s, "commit", "-q", "-m", COMMIT_MESSAGE)
        after = _git(worktree, timeout_s, "rev-parse", "HEAD")
        moved = after.returncode == 0 and (before.returncode != 0 or after.stdout != before.stdout)
        if done.returncode != 0:
            if _timed_out(done):
                error = f"{_describe(done)}; {_lock_note(worktree, timeout_s)}"
                sha = _git(worktree, timeout_s, "rev-parse", "--short", "HEAD").stdout.strip() if moved else None
                return WipResult(moved, sha or None, False, unpushed, untracked, WipReason.GIT_ERROR, error)
            return WipResult(False, None, False, unpushed, untracked, WipReason.COMMIT_REJECTED, _describe(done))
        committed = True
        rev = _git(worktree, timeout_s, "rev-parse", "--short", "HEAD")
        if rev.returncode != 0:
            return WipResult(True, None, False, unpushed, untracked, WipReason.GIT_ERROR, _describe(rev))
        sha = rev.stdout.strip()
        unpushed, err = _unpushed(worktree, timeout_s)
        if err:
            return WipResult(True, sha, False, (), untracked, WipReason.GIT_ERROR, err)
    if not unpushed:
        return WipResult(committed, sha, False, (), untracked)
    if not push_allowed:
        return WipResult(committed, sha, False, unpushed, untracked, WipReason.PUSH_NOT_ALLOWED)
    reason, push_error = _push(worktree, branch, timeout_s)
    if reason is not None:
        remaining, err = _unpushed(worktree, timeout_s)
        if err:  # the state after the refused push is unknown: not a good local commit
            return WipResult(committed, sha, False, unpushed, untracked, WipReason.GIT_ERROR, f"{push_error}; {err}")
        return WipResult(committed, sha, False, remaining, untracked, reason, push_error)
    after_push, err = _unpushed(worktree, timeout_s)
    if err:
        return WipResult(committed, sha, True, (), untracked, WipReason.GIT_ERROR, err)
    return WipResult(committed, sha, True, after_push, untracked)
