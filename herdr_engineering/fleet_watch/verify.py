"""D3: only published history is rollable.

(a) the SHA came from the GitHub API as the head of `main` (the caller passes the API head), and
(b) after fetching `origin/main` into the watcher's own bare cache clone,
    `git merge-base --is-ancestor <sha> origin/main` succeeds.
A stray unpushed commit, or a SHA that a force-push removed from `main`, is refused.
The same check is meant for the lock-writing path (spec section 7) in a later phase.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT_S = 120

#: Why a SHA was refused; each becomes a `source.refused` reason in the event phase.
REASONS = ("malformed_sha", "not_api_head", "fetch_failed", "unknown_commit", "not_on_origin_main")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None
    detail: str = ""


def _git(cache: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cache), *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S, env=_clean_env())


def _clean_env() -> dict[str, str]:
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def cache_path(directory: Path, repo: str) -> Path:
    return directory / "cache" / (repo.replace("/", "__") + ".git")


def fetch_main(cache: Path, url: str) -> Verdict:
    """Fetch the remote's `main` into `refs/remotes/origin/main` of the bare cache clone."""
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        init = subprocess.run(["git", "init", "-q", "--bare", str(cache)], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S, env=_clean_env())
        if init.returncode:
            return Verdict(False, "fetch_failed", init.stderr.strip()[:200])
    # `--force` so a force-pushed main replaces the ref; the commit stays in the object store,
    # which is exactly why is-ancestor (not mere existence) is the test.
    got = _git(cache, "fetch", "-q", "--no-tags", "--force", url,
               "+refs/heads/main:refs/remotes/origin/main")
    if got.returncode:
        return Verdict(False, "fetch_failed", got.stderr.strip()[:200])
    return Verdict(True)


def verify_published(sha: str, *, api_head: str | None, cache: Path, url: str,
                     require_tip: bool = False) -> Verdict:
    """Accept `sha` only if it is the API-reported head and an ancestor of fetched origin/main.

    `api_head` is required (None is refused). With `require_tip` the fetched origin/main tip must
    also equal `sha`, which makes the API-head check non-circular: the head the poll saw is compared
    with what git independently reports now, and a main that moved in between is refused (the next
    pass picks up the newer head).
    """
    if not _SHA.fullmatch(sha or ""):
        return Verdict(False, "malformed_sha")
    if api_head is None or sha != api_head:
        return Verdict(False, "not_api_head", f"api head is {api_head}")
    fetched = fetch_main(cache, url)
    if not fetched.ok:
        return fetched
    if _git(cache, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
        return Verdict(False, "unknown_commit")
    if _git(cache, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main").returncode:
        return Verdict(False, "not_on_origin_main")
    if require_tip:
        tip = _git(cache, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        if tip != sha:
            return Verdict(False, "not_api_head", f"origin/main is {tip}")
    return Verdict(True)
