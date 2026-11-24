#!/usr/bin/env python3
"""Copy the per-repo handoff stores (`<repo>/.herdr/handoffs`) into the shared store, once per record.

Usage: handoff_import_legacy.py [REPO ...]   (no argument: the repository of the current directory)

Runs `herdr-handoff store import-legacy` against the shared store root the controller uses. A record the shared store
already has is skipped, so running it again imports nothing; nothing is ever deleted or changed in the old stores.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herdr_engineering.handoff_controller.store import import_legacy_args, shared_store_root  # noqa: E402


def _repo_of(path: Path) -> Path | None:
    done = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                          check=False)
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def main(argv: list[str], env: dict[str, str] | None = None, prefix: tuple[str, ...] = ("herdr-handoff",)) -> int:
    env = dict(os.environ if env is None else env)
    repos = [Path(a) for a in argv] or [_repo_of(Path.cwd()) or Path.cwd()]
    store = shared_store_root(env)
    failed = 0
    for repo in repos:
        done = subprocess.run([*prefix, *import_legacy_args(store, repo)], env=env, capture_output=True, text=True,
                              check=False)
        sys.stdout.write(f"{repo}: {done.stdout.strip() or done.stderr.strip()}\n")
        failed += done.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
