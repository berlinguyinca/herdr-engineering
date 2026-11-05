"""One read-only watcher pass: poll -> verify (D3) -> plan. No ssh, no events, no lock writes."""

from __future__ import annotations

import os
import random
from pathlib import Path

import yaml

from . import plan, poll, state, verify

DEFAULT_REPOS = (
    "berlinguyinca/autospec",
    "berlinguyinca/pi-engineering",
    "berlinguyinca/herdr-engineering",
)
DEFAULT_GIT_BASE = "https://github.com"
DEFAULT_LOCK = "~/.config/herdr-fleet/fleet.lock.yaml"


def git_base() -> str:
    return os.environ.get("HERDR_FLEET_WATCH_GIT_BASE", DEFAULT_GIT_BASE).rstrip("/")


def read_lock_repos(path: str | Path) -> dict[str, str]:
    """`repos:` of fleet.lock.yaml (name -> sha), read-only; {} when absent or unreadable."""
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {str(k): str(v) for k, v in repos.items() if isinstance(v, str)}


def baseline(saved: dict, lock_repos: dict[str, str], repos: list[str]) -> dict[str, str]:
    """Last converged SHA per repo: the state file wins, then the lock (keyed by repo name)."""
    out: dict[str, str] = {}
    for repo in repos:
        name = repo.split("/", 1)[-1]
        sha = saved["converged"].get(repo) or lock_repos.get(name) or lock_repos.get(repo)
        if sha:
            out[repo] = sha
    return out


def run_once(repos: list[str], *, directory: Path | None = None, now: float | None = None,
             fetch: poll.Fetch = poll.http_get, lock_path: str | Path = DEFAULT_LOCK,
             rng: random.Random | None = None, auth: str | None = None) -> dict:
    """Poll every repo once (honouring backoff), verify heads, return the report.

    Persists only the poll cache (etag, head, backoff) in the state file.
    """
    directory = directory or state.state_dir()
    now = poll.now_s() if now is None else now
    rng = rng or random.Random()
    report: dict = {"schema_version": 1, "dry_run": True, "repos": {}, "refused": {},
                    "rollout": None}
    with state.single_instance(directory):
        saved = state.load(directory)
        verified: dict[str, str] = {}
        for repo in repos:
            entry = saved["repos"].setdefault(repo, {})
            if int(entry.get("next_poll_at") or 0) > now and entry.get("degraded"):
                report["repos"][repo] = {"outcome": "backoff", "head": entry.get("head"),
                                         "retry_in_s": int(entry["next_poll_at"] - now)}
                continue
            result = poll.poll_repo(repo, entry, now=now, fetch=fetch, rng=rng, auth=auth)
            poll.apply_result(entry, result, now=now)
            report["repos"][repo] = {"outcome": result.outcome, "head": result.head,
                                     "wait_s": result.wait_s, **({"detail": result.detail} if result.detail else {})}
            if result.outcome == "error" or result.outcome == "rate_limited" or not result.head:
                continue
            verdict = verify.verify_published(
                result.head, api_head=result.head, url=f"{git_base()}/{repo}.git",
                cache=verify.cache_path(directory, repo))
            if verdict.ok:
                verified[repo] = result.head
            else:
                report["refused"][repo] = {"sha": result.head, "reason": verdict.reason,
                                           "detail": verdict.detail}
        base = baseline(saved, read_lock_repos(lock_path), repos)
        report["rollout"] = plan.plan_rollout(verified, base, saved["rollouts"])
        state.save(saved, directory)
    return report
