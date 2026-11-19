"""Reading CI for the CI tab off the render path: one background read at a time, never an exception, tokenless.

The branch and the remote come from `git` in the session's directory, the pull requests from `gh pr list` (a failure
only means no pull request is known), the pipelines from `WoodpeckerProvider`'s public reads, which send no credential.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..ci import WoodpeckerProvider
from .ci_tab import CiCard, CiState, build_cards

_GIT_S = 10.0
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
    """Runs `fetch` on a thread, one read at a time. `request` returns at once; `latest` is the last finished read."""

    def __init__(self, fetch: Callable[[Path], tuple[CiCard, ...]], clock: Callable[[], float] = time.time) -> None:
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self._latest: CiResult | None = None

    def request(self, cwd: Path) -> bool:
        """Start a read of `cwd` unless one is running; True when one was started."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._read, args=(cwd,), daemon=True, name="side-pane-ci").start()
        return True

    def _read(self, cwd: Path) -> None:
        try:
            result = CiResult(cwd, self._fetch(cwd), None, self._clock())
        except Exception as error:  # a read must never end the pane: whatever failed shows as "CI unavailable"
            card = CiCard("CI", "CI unavailable", CiState.UNAVAILABLE, (), None)
            result = CiResult(cwd, (card,), f"{type(error).__name__}: {error}", self._clock())
        with self._lock:
            self._latest, self._running = result, False

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
