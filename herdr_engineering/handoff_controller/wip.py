"""Checkpoint a worktree before handoff: commit tracked changes on a feature branch, push when allowed."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROTECTED_BRANCHES = frozenset({"main", "master"})
COMMIT_MESSAGE = "wip: auto-handoff checkpoint"
_AMBIENT_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE")


class WipReason(StrEnum):
    ON_MAIN = "on_main"  # main or master: nothing committed
    DETACHED = "detached"  # detached HEAD: nothing committed
    COMMIT_REJECTED = "commit_rejected"  # hook, identity or any other commit failure; `error` has the first stderr line
    PUSH_FAILED = "push_failed"
    PUSH_NOT_ALLOWED = "push_not_allowed"  # committed, or commits unpushed, and the caller did not allow a push
    GIT_ERROR = "git_error"  # a git command failed: the state is unknown, see `error`


@dataclass(frozen=True)
class WipResult:
    committed: bool
    sha: str | None
    pushed: bool
    unpushed: tuple[str, ...]
    untracked: tuple[str, ...] = ()
    reason: WipReason | None = None  # None: clean tree, or committed and pushed
    error: str | None = None  # set when `reason` is a failure; treat the repository state as unknown for GIT_ERROR


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_GIT_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(worktree: Path, timeout_s: float, *args: str) -> subprocess.CompletedProcess[str]:
    """Never raises: a timeout is returncode 124, a missing git or unusable directory 127."""
    cmd = ["git", "-C", str(worktree), *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                              env=_env(), check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout_s}s")
    except (OSError, ValueError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _describe(proc: subprocess.CompletedProcess[str]) -> str:
    stderr = proc.stderr.strip().splitlines() if isinstance(proc.stderr, str) else []
    first = stderr[0] if stderr else "no output"
    return f"git {' '.join(proc.args[3:5])} exit {proc.returncode}: {first}"


def _unpushed(worktree: Path, timeout_s: float) -> tuple[tuple[str, ...], str | None]:
    proc = _git(worktree, timeout_s, "rev-list", "--abbrev-commit", "HEAD", "--not", "--remotes")
    if proc.returncode != 0:
        return (), _describe(proc)
    return tuple(proc.stdout.split()), None


def _push(worktree: Path, branch: str, timeout_s: float) -> tuple[bool, str | None]:
    remote = _git(worktree, timeout_s, "config", f"branch.{branch}.remote").stdout.strip()
    merge = _git(worktree, timeout_s, "config", f"branch.{branch}.merge").stdout.strip()
    if remote and merge:
        target = merge.removeprefix("refs/heads/")
        if target in PROTECTED_BRANCHES:
            return False, f"upstream {remote}/{target} is protected"
        proc = _git(worktree, timeout_s, "push", remote, f"HEAD:{target}")
    else:
        proc = _git(worktree, timeout_s, "push", "origin", branch)
    return (True, None) if proc.returncode == 0 else (False, _describe(proc))


def wip_commit(worktree: Path, *, push_allowed: bool, timeout_s: float = 120.0) -> WipResult:
    """Commit tracked changes (never untracked files, never on main/master or a detached HEAD); push if allowed.

    `git add -u` adds tracked changes only, but anything the user already staged (new files included) is part of
    the commit. Never raises and never forces a push: a failure is in `reason` and `error`. A failing git command
    gives GIT_ERROR, never an empty-looking result.
    """
    def failure(proc: subprocess.CompletedProcess[str]) -> WipResult:
        return WipResult(False, None, False, (), (), WipReason.GIT_ERROR, _describe(proc))

    head = _git(worktree, timeout_s, "symbolic-ref", "--short", "-q", "HEAD")
    if head.returncode not in (0, 1):  # 1 with -q: HEAD is detached
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
    committed = False
    sha: str | None = None
    if status.stdout.strip():
        added = _git(worktree, timeout_s, "add", "-u")
        done = added if added.returncode != 0 else _git(worktree, timeout_s, "commit", "-q", "-m", COMMIT_MESSAGE)
        if done.returncode != 0:
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
    pushed, push_error = _push(worktree, branch, timeout_s)
    if not pushed:
        return WipResult(committed, sha, False, unpushed, untracked, WipReason.PUSH_FAILED, push_error)
    after, err = _unpushed(worktree, timeout_s)
    return WipResult(committed, sha, True, after, untracked, None, err)
