"""Checkpoint a worktree before handoff: commit tracked changes on a feature branch, push when allowed."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROTECTED_BRANCHES = frozenset({"main", "master"})
COMMIT_MESSAGE = "wip: auto-handoff checkpoint"
_GIT_TIMEOUT_S = 120


@dataclass(frozen=True)
class WipResult:
    committed: bool
    sha: str | None
    pushed: bool
    unpushed: tuple[str, ...]
    untracked: tuple[str, ...] = ()


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True,
                          timeout=_GIT_TIMEOUT_S, stdin=subprocess.DEVNULL, env=_env())


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _lines(proc: subprocess.CompletedProcess[str]) -> tuple[str, ...]:
    return tuple(line for line in proc.stdout.splitlines() if line) if proc.returncode == 0 else ()


def _unpushed(worktree: Path) -> tuple[str, ...]:
    return _lines(_git(worktree, "rev-list", "--abbrev-commit", "HEAD", "--not", "--remotes"))


def _push(worktree: Path, branch: str) -> bool:
    remote = _git(worktree, "config", f"branch.{branch}.remote").stdout.strip()
    merge = _git(worktree, "config", f"branch.{branch}.merge").stdout.strip()
    if remote and merge:
        target = merge.removeprefix("refs/heads/")
        if target in PROTECTED_BRANCHES:
            return False
        proc = _git(worktree, "push", remote, f"HEAD:{target}")
    else:
        proc = _git(worktree, "push", "origin", branch)
    return proc.returncode == 0


def wip_commit(worktree: Path, *, push_allowed: bool) -> WipResult:
    """Commit tracked changes (never untracked files, never on main/master or a detached HEAD); push if allowed.

    A rejected commit or push is reported in the result, never raised.
    """
    branch = _git(worktree, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    untracked = _lines(_git(worktree, "ls-files", "--others", "--exclude-standard"))
    if not branch or branch in PROTECTED_BRANCHES:
        return WipResult(False, None, False, _unpushed(worktree), untracked)
    committed = False
    sha: str | None = None
    if _lines(_git(worktree, "status", "--porcelain", "--untracked-files=no")):
        _git(worktree, "add", "-u")
        if _git(worktree, "commit", "-q", "-m", COMMIT_MESSAGE).returncode == 0:
            committed = True
            sha = _git(worktree, "rev-parse", "--short", "HEAD").stdout.strip()
    unpushed = _unpushed(worktree)
    pushed = False
    if push_allowed and unpushed:
        pushed = _push(worktree, branch)
        if pushed:
            unpushed = _unpushed(worktree)
    return WipResult(committed, sha, pushed, unpushed, untracked)
