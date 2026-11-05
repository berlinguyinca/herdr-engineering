"""GitHub poller (decisions D1, D2): conditional GET of `branches/main` per repo.

A `304` costs no primary rate limit. `403`/`429` and network errors back off; the schedule is a
pure function so it is unit-testable without sleeping. Read-only: only GET, never a write scope.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

DEFAULT_API = "https://api.github.com"
INTERVAL_S = 120
JITTER_S = 20
LOW_REMAINING = 100
LOW_REMAINING_INTERVAL_S = 600
BACKOFF_MIN_S = 120
BACKOFF_CAP_S = 900
HTTP_TIMEOUT_S = 15
_SHA = re.compile(r"^[0-9a-f]{40}$")

Outcome = Literal["new", "changed", "unchanged", "rate_limited", "error"]


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]   # lower-cased names
    body: bytes


Fetch = Callable[[str, Mapping[str, str]], Response]


def api_base() -> str:
    return os.environ.get("HERDR_FLEET_WATCH_API", DEFAULT_API).rstrip("/")


def token() -> str | None:
    """A read token from the environment, else `gh auth token`; None means anonymous."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def http_get(url: str, headers: Mapping[str, str]) -> Response:
    """Real GET. urllib raises on 304/4xx, which are ordinary answers here."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, {k.lower(): v for k, v in err.headers.items()}, err.read())


def backoff_seconds(*, failures: int, headers: Mapping[str, str] | None = None, now: float,
                    jitter: float = 0.0) -> int:
    """How long to stay away after a rate-limit or network failure (D2).

    `Retry-After`, then `x-ratelimit-reset`, each capped at 15 min; otherwise exponential
    2 min -> 15 min. `jitter` (0..1) widens only the exponential schedule.
    """
    headers = headers or {}
    retry_after = headers.get("retry-after", "")
    if retry_after.isdigit():
        return min(int(retry_after), BACKOFF_CAP_S)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return min(max(int(reset) - int(now), 0), BACKOFF_CAP_S)
    base = min(BACKOFF_MIN_S * (2 ** max(failures - 1, 0)), BACKOFF_CAP_S)
    return min(int(base * (1 + 0.25 * jitter)), BACKOFF_CAP_S)


def next_interval(headers: Mapping[str, str], rng: random.Random) -> int:
    remaining = headers.get("x-ratelimit-remaining", "")
    if remaining.isdigit() and int(remaining) < LOW_REMAINING:
        return LOW_REMAINING_INTERVAL_S
    return INTERVAL_S + rng.randint(-JITTER_S, JITTER_S)


@dataclass(frozen=True)
class PollResult:
    repo: str
    outcome: Outcome
    head: str | None
    etag: str | None
    wait_s: int            # seconds until this repo should be polled again
    detail: str = ""


def poll_repo(repo: str, entry: Mapping[str, object], *, now: float, fetch: Fetch = http_get,
              rng: random.Random | None = None, auth: str | None = None) -> PollResult:
    """One conditional GET. `entry` is this repo's saved poll state (etag, head, failures)."""
    rng = rng or random.Random()
    etag = entry.get("etag")
    prior_head = entry.get("head")
    failures = int(entry.get("failures") or 0)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "herdr-fleet-watch"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    if etag:
        headers["If-None-Match"] = str(etag)
    try:
        resp = fetch(f"{api_base()}/repos/{repo}/branches/main", headers)
    except (OSError, ValueError) as exc:
        wait = backoff_seconds(failures=failures + 1, now=now, jitter=rng.random())
        return PollResult(repo, "error", prior_head, etag, wait, f"network: {exc}"[:200])
    if resp.status == 304:
        return PollResult(repo, "unchanged", prior_head, etag, next_interval(resp.headers, rng))
    if resp.status in (403, 429):
        wait = backoff_seconds(failures=failures + 1, headers=resp.headers, now=now,
                               jitter=rng.random())
        return PollResult(repo, "rate_limited", prior_head, etag, wait, f"http {resp.status}")
    if resp.status != 200:
        wait = backoff_seconds(failures=failures + 1, now=now, jitter=rng.random())
        return PollResult(repo, "error", prior_head, etag, wait, f"http {resp.status}")
    try:
        head = json.loads(resp.body)["commit"]["sha"]
    except (ValueError, KeyError, TypeError):
        head = None
    if not isinstance(head, str) or not _SHA.match(head):
        wait = backoff_seconds(failures=failures + 1, now=now, jitter=rng.random())
        return PollResult(repo, "error", prior_head, etag, wait, "malformed branch response")
    outcome: Outcome = "new" if prior_head is None else ("changed" if head != prior_head else "unchanged")
    return PollResult(repo, outcome, head, resp.headers.get("etag", etag), next_interval(resp.headers, rng))


def apply_result(entry: dict, result: PollResult, *, now: float) -> dict:
    """Fold a poll result into the repo's saved state (returns the same dict)."""
    ok = result.outcome in ("new", "changed", "unchanged")
    entry["head"] = result.head
    entry["etag"] = result.etag
    entry["last_polled"] = int(now)
    entry["failures"] = 0 if ok else int(entry.get("failures") or 0) + 1
    entry["next_poll_at"] = int(now) + result.wait_s
    entry["last_outcome"] = result.outcome
    entry["degraded"] = not ok
    return entry


def now_s() -> float:
    return time.time()
