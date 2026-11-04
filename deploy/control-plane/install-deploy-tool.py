#!/usr/bin/env python3
"""Install an explicitly selected committed deploy tool and fail-closed legacy routes.

No deployment is performed. --home and every --legacy path must be explicit.
Run only after the selected revision's deployment tests have passed.
"""
import argparse
import fcntl
import hashlib
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = b"# herdr-deploy canonical routing v1\n"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   stderr=subprocess.DEVNULL, text=True).strip()


def atomic_write(path, body, mode):
    fd, temporary = tempfile.mkstemp(prefix=".herdr-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(repo, ref, home, legacy):
    repo, home = repo.resolve(strict=True), home.resolve(strict=True)
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    tools = home / ".local/share/herdr-engineering/deploy-tools"
    tree = tools / commit
    canonical = tree / "deploy/control-plane/deploy.sh"
    launcher = home / ".local/bin/herdr-deploy"
    shim = ("#!/usr/bin/env bash\n" + MARKER.decode() +
            f"if [ ! -x {shlex.quote(str(launcher))} ]; then\n"
            "  echo 'canonical herdr-deploy unavailable; refusing legacy deployment' >&2\n"
            "  exit 69\nfi\n"
            f"exec {shlex.quote(str(launcher))} \"$@\"\n"
            "exit 69\n# Original entry point retained verbatim below (unreachable).\n").encode()
    # Validate the entire route set before modifying even the first legacy file.
    originals = {}
    for path in legacy:
        if (path.is_symlink() or not path.is_file()
                or path.resolve().is_relative_to(tools.resolve())
                or path.resolve() == launcher.resolve()):
            raise ValueError("unsafe legacy route")
        body = path.read_bytes()
        if MARKER in body and not body.startswith(shim):
            raise ValueError("unrecognized existing routing shim")
        originals[path] = (body, stat.S_IMODE(path.stat().st_mode))
    if launcher.exists() or launcher.is_symlink():
        if not launcher.is_symlink() or not launcher.resolve().is_relative_to(tools.resolve()):
            raise ValueError("launcher is not an owned canonical route")
    state = home / ".local/state/herdr-engineering/deploys"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(state / "rollout.lock", os.O_CREAT | os.O_RDWR, 0o600)
    compatibility_fd = None
    try:
        # Same immutable inode as deploy.sh; never unlink it, including on errors.
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Also exclude an upstream deploy that predates rollout.lock. Keep the
        # acquisition order identical to deploy.sh and never replace either inode.
        compatibility_fd = os.open(state / "deploy.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(compatibility_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if tree.exists():
            if (git(tree, "rev-parse", "HEAD") != commit
                    or git(tree, "status", "--porcelain", "--untracked-files=all")):
                raise ValueError("canonical tool worktree is not clean")
        else:
            tools.mkdir(parents=True, exist_ok=True, mode=0o700)
            git(repo, "worktree", "add", "--detach", str(tree), commit)
        if (not os.access(canonical, os.X_OK) or MARKER in canonical.read_bytes()
                or not (canonical.parent / "deploy-lock.sh").is_file()
                or not (canonical.parent / "deploy-state.py").is_file()):
            raise ValueError("selected revision lacks canonical deployment tooling")
        launcher.parent.mkdir(parents=True, exist_ok=True)
        # A unique temporary symlink prevents partially written launchers.
        with tempfile.TemporaryDirectory(prefix=".herdr-route-", dir=launcher.parent) as directory:
            link = Path(directory) / "herdr-deploy"
            link.symlink_to(canonical)
            os.replace(link, launcher)
        for path, (body, mode) in originals.items():
            if body.startswith(shim):
                continue
            digest = hashlib.sha256(body).hexdigest()
            backup = path.with_name(f"{path.name}.pre-herdr-routing-{digest}.bak")
            if backup.exists():
                if backup.read_bytes() != body:
                    raise ValueError("legacy backup mismatch")
            else:
                atomic_write(backup, body, mode & 0o700)
            atomic_write(path, shim + body, mode)
    finally:
        if compatibility_fd is not None:
            os.close(compatibility_fd)
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ref", required=True, help="approved, tested commit or ref")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        install(args.repo, args.ref, args.home, args.legacy)
    except BlockingIOError:
        print("deployment busy; installation refused (exit 75)", file=sys.stderr)
        return 75
    except (OSError, ValueError, subprocess.SubprocessError):
        print("deployment tool installation refused; check revision, clean tool tree and routes",
              file=sys.stderr)
        return 1
    print("canonical deployment tool installed; no deployment performed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
