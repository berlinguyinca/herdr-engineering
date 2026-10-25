"""Read a fixed commit's blobs with git, never the live working tree.

Enumerating ``git ls-tree`` and reading blobs with ``git cat-file`` means the
snapshot really is the commit: a dirty or mid-edit worktree cannot leak in,
symlinks (mode 120000) and submodules (160000) are skipped by construction, and
two worktrees on the same SHA read identical bytes. Git runs with a scrubbed
environment and with hooks / fsmonitor disabled, because ``.git/config`` of an
analysed repository is attacker-controlled.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

_GIT_FLAGS = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
              "-c", "protocol.ext.allow=never", "--no-optional-locks"]
REGULAR_MODES = {"100644", "100755"}


class GitSourceError(RuntimeError):
    pass


def clean_env(home: str = "/nonexistent") -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0"}


def run_git(repo: str, *args: str, stdin: bytes | None = None, timeout: float = 60.0) -> bytes:
    cmd = ["git", *_GIT_FLAGS, "-C", repo, *args]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout,
                              env=clean_env(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise GitSourceError("git timed out") from exc
    if proc.returncode != 0:
        raise GitSourceError(proc.stderr.decode("utf-8", "replace").strip()[:300] or "git failed")
    return proc.stdout


@dataclass
class TreeEntry:
    path: str
    sha: str
    size: int


@dataclass
class CommitTree:
    commit_sha: str
    entries: list[TreeEntry] = field(default_factory=list)
    skipped_symlinks: int = 0
    skipped_submodules: int = 0


def resolve_commit(repo: str, rev: str) -> str:
    if rev.startswith("-"):
        raise GitSourceError("invalid revision")
    out = run_git(repo, "rev-parse", "--verify", "--end-of-options", rev + "^{commit}")
    return out.decode().strip()


def list_tree(repo: str, commit_sha: str) -> CommitTree:
    raw = run_git(repo, "ls-tree", "-r", "-z", "--long", commit_sha, timeout=120)
    tree = CommitTree(commit_sha=commit_sha)
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        try:
            mode, kind, sha, size = meta.decode().split()
            name = path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue  # a path that is not valid UTF-8 cannot be a stable source link
        if mode == "120000":
            tree.skipped_symlinks += 1
        elif mode == "160000" or kind == "commit":
            tree.skipped_submodules += 1
        elif mode in REGULAR_MODES and kind == "blob":
            tree.entries.append(TreeEntry(name, sha, int(size) if size.isdigit() else 0))
    tree.entries.sort(key=lambda e: e.path)
    return tree


def read_blobs(repo: str, shas: list[str], *, max_bytes: int) -> dict[str, bytes | None]:
    """Blob contents by sha; oversize blobs map to None (git still bounds the stream)."""
    if not shas:
        return {}
    out: dict[str, bytes | None] = {}
    proc = subprocess.run(["git", *_GIT_FLAGS, "-C", repo, "cat-file", "--batch"],
                          input=("\n".join(shas) + "\n").encode(), capture_output=True,
                          env=clean_env(), timeout=300, check=False)
    if proc.returncode != 0:
        raise GitSourceError(proc.stderr.decode("utf-8", "replace").strip()[:300] or "cat-file failed")
    buf, pos = proc.stdout, 0
    while pos < len(buf):
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].split()
        pos = nl + 1
        if len(header) == 2 and header[1] == b"missing":
            out[header[0].decode()] = None
            continue
        sha, size = header[0].decode(), int(header[2])
        data = buf[pos:pos + size]
        pos += size + 1
        out[sha] = data if size <= max_bytes else None
    return out


def worktree_identity(path: str) -> dict:
    """Same scheme as pi-engineering src/runtime/isolation/WorktreeIdentity.ts."""
    import hashlib
    common = run_git(path, "rev-parse", "--git-common-dir").decode().strip()
    top = run_git(path, "rev-parse", "--show-toplevel").decode().strip()
    common_real = os.path.realpath(os.path.join(path, common) if not os.path.isabs(common) else common)
    top_real = os.path.realpath(top)
    digest = lambda s: hashlib.sha256(s.encode()).hexdigest()[:24]  # noqa: E731
    return {"repo_id": digest(common_real), "worktree_id": digest(common_real + "\0" + top_real),
            "root": top_real}


def origin_url(path: str) -> str | None:
    try:
        return run_git(path, "config", "--get", "remote.origin.url").decode().strip() or None
    except GitSourceError:
        return None
