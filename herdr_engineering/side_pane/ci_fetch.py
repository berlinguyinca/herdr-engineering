"""Reading CI for the CI tab off the render path: one background read at a time, never an exception, tokenless.

The branch and the remote come from `git` in the session's directory, the pull requests from `gh pr list` run with the
user's own `gh` login (their CLI session; a failure only means no pull request is known), the pipelines from
`WoodpeckerProvider`'s public reads, which send no credential. No Woodpecker token is ever used. PRs are listed for the
focused session's directory and branch only, so a chain that spans repositories shows the current repository's PRs.

A read that runs past `CI_DEADLINE_S` is given up: the worker is free again, the tab shows "CI unavailable" and keeps the
last good cards, marked stale.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..ci import WoodpeckerProvider
from .ci_tab import CiCard, CiState, build_cards

_GIT_S = 10.0
CI_DEADLINE_S = 90.0  # a read (git, gh and Woodpecker together) that takes longer is abandoned
_REMOTE = re.compile(r"^(?:[\w.+-]+://(?:[^/@]+@)?[^/:]+(?::\d+)?/|[\w.-]+@[^:/]+:)([^/]+/[^/]+?)(?:\.git)?/?$")


def repo_slug(remote_url: str) -> str | None:
    """`org/repo` of an ssh or https remote url; None for a local path or anything else."""
    found = _REMOTE.match(remote_url.strip())
    return found.group(1) if found else None


def _out(args: list[str], cwd: Path) -> str | None:
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=_GIT_S, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def fetch_cards(cwd: Path) -> tuple[CiCard, ...]:
    """The CI cards of the branch checked out in `cwd`; none when it is no repository, detached, or has no usable remote."""
    branch = _out(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote = _out(["git", "remote", "get-url", "origin"], cwd)
    slug = repo_slug(remote) if remote else None
    if not branch or branch == "HEAD" or not slug:
        return ()
    listed = _out(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,title,headRefName,state,mergedAt,url"], cwd)
    try:
        pulls = json.loads(listed) if listed else []
    except ValueError:
        pulls = []
    return build_cards(slug, [branch], WoodpeckerProvider(), [p for p in pulls if isinstance(p, dict)])


@dataclass(frozen=True)
class CiResult:
    cwd: Path
    cards: tuple[CiCard, ...]
    error: str | None
    at: float


class CiWorker:
    """Runs `fetch` on a thread, one read at a time. `request` returns at once; `latest` is the last finished read.
    A read still running `deadline_s` after it started is abandoned: its late result is ignored."""

    def __init__(self, fetch: Callable[[Path], tuple[CiCard, ...]], clock: Callable[[], float] = time.time,
                 deadline_s: float = CI_DEADLINE_S) -> None:
        self._fetch = fetch
        self._clock = clock
        self._deadline_s = deadline_s
        self._lock = threading.Lock()
        self._running = False
        self._token = 0
        self._started = 0.0
        self._latest: CiResult | None = None
        self._good: CiResult | None = None

    def request(self, cwd: Path) -> bool:
        """Start a read of `cwd` unless one is running within its deadline; True when one was started."""
        with self._lock:
            if self._running:
                if self._clock() - self._started < self._deadline_s:
                    return False
                self._publish(cwd, TimeoutError(f"timed out after {self._deadline_s:g}s"))
            self._running, self._started = True, self._clock()
            self._token += 1
            token = self._token
        threading.Thread(target=self._read, args=(cwd, token), daemon=True, name="side-pane-ci").start()
        return True

    def _publish(self, cwd: Path, error: Exception) -> None:
        """Called with the lock held: the result of a failed or abandoned read, keeping the last good cards stale."""
        card = CiCard("CI", "CI unavailable", CiState.UNAVAILABLE, (), None)
        kept = tuple(replace(c, stale=True) for c in self._good.cards) if self._good is not None and self._good.cwd == cwd else ()
        self._latest = CiResult(cwd, (card, *kept), f"{type(error).__name__}: {error}", self._clock())

    def _read(self, cwd: Path, token: int) -> None:
        try:
            cards, error = self._fetch(cwd), None
        except Exception as caught:  # a read must never end the pane: whatever failed shows as "CI unavailable"
            cards, error = (), caught
        with self._lock:
            if token != self._token:
                return  # abandoned past its deadline: a newer read owns the result
            if error is None:
                self._latest = self._good = CiResult(cwd, cards, None, self._clock())
            else:
                self._publish(cwd, error)
            self._running = False

    def latest(self) -> CiResult | None:
        with self._lock:
            return self._latest


class CiSchedule:
    """Asks the worker for a read at most once per `interval_s`, and at once when the session's directory changes."""

    def __init__(self, worker: CiWorker, interval_s: float) -> None:
        self._worker = worker
        self._interval_s = interval_s
        self._cwd: Path | None = None
        self._last: float | None = None

    def maybe_request(self, cwd: Path, now: float) -> bool:
        same = cwd == self._cwd
        if same and self._last is not None and 0 <= now - self._last < self._interval_s:
            return False
        self._cwd, self._last = cwd, now
        self._worker.request(cwd)
        return True
