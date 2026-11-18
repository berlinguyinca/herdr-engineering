"""Default-on disk hygiene: the worktree reaper and the docker sweep.

The reaper never decides by path. For each repository it lists ``git worktree list`` and removes a
worktree (its in-tree ``target`` goes with it) only when git and PR state agree it is finished:

* PR merged or closed, tree clean, nothing unpushed; or
* no PR at all, idle for ``worktree_ttl_days`` (default 7), tree clean, fully pushed.

An open PR, an unknown PR state (gh missing, offline), recent activity, a running process, a
dirty tree or an unpushed commit all keep the worktree. The safety checks are the same
``cleanup_command`` code an agent hits, so the reaper cannot do what the command refuses.

Config (``disk_hygiene`` in the herdr-eng config; everything optional, default on)::

    disk_hygiene: {enabled: true, repos: [], roots: [], worktree_ttl_days: 7,
                   min_idle_hours: 2, pr_source: gh,          # gh | git (offline, ancestry only)
                   worktrees: {enabled: true}, docker: {enabled: true, min_age_hours: 1,
                                                        builder_keep_storage: 5GB}}

Opt out entirely with ``enabled: false`` or ``HERDR_DISK_HYGIENE=0``.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cleanup_command import (
    Refusal,
    _git,
    busy_paths,
    checked_path,
    configured_roots,
    gh_pr_state,
    remove_worktree,
)

_DISCOVERY_DEPTH = 2


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    worktrees: bool = True
    docker: bool = True
    ttl_days: float = 7.0
    min_idle_hours: float = 2.0
    pr_source: str = "gh"
    repos: tuple[Path, ...] = ()
    roots: tuple[str, ...] = ()
    docker_min_age_hours: float = 1.0
    builder_keep_storage: str = "5GB"
    extra: dict[str, Any] = field(default_factory=dict)


def load_policy(cfg: dict[str, Any] | None = None) -> Policy:
    if cfg is None:
        try:
            from .config import load_config
            cfg = load_config().get("disk_hygiene") or {}
        except Exception:  # noqa: BLE001 - unreadable config means defaults, i.e. on
            cfg = {}
    wt, dk = cfg.get("worktrees") or {}, cfg.get("docker") or {}
    off = os.environ.get("HERDR_DISK_HYGIENE", "1").strip().lower() in ("0", "false", "no", "off")
    return Policy(
        enabled=bool(cfg.get("enabled", True)) and not off,
        worktrees=bool(wt.get("enabled", True)),
        docker=bool(dk.get("enabled", True)),
        ttl_days=float(cfg.get("worktree_ttl_days", 7)),
        min_idle_hours=float(cfg.get("min_idle_hours", 2)),
        pr_source=str(cfg.get("pr_source", "gh")),
        repos=tuple(Path(r).expanduser() for r in cfg.get("repos", [])),
        roots=tuple(str(r) for r in cfg.get("roots", [])),
        docker_min_age_hours=float(dk.get("min_age_hours", 1)),
        builder_keep_storage=str(dk.get("builder_keep_storage", "5GB")),
    )


def find_repos(roots: list[Path]) -> list[Path]:
    """Main checkouts (a ``.git`` directory) near the roots. Discovery only, never a removal rule."""
    found: list[Path] = []
    for root in roots:
        pending = [(Path(os.path.abspath(root)), 0)]
        while pending:
            directory, depth = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            if any(e.name == ".git" and e.is_dir(follow_symlinks=False) for e in entries):
                found.append(directory)
                continue
            if depth < _DISCOVERY_DEPTH:
                pending += [(Path(e.path), depth + 1) for e in entries
                            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    return sorted(set(found))


def list_worktrees(repo: Path) -> list[dict[str, Any]]:
    out = _git(repo, "worktree", "list", "--porcelain")
    if out.returncode != 0:
        return []
    items: list[dict[str, Any]] = []
    for block in out.stdout.strip().split("\n\n"):
        item: dict[str, Any] = {"locked": False, "prunable": False, "branch": ""}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                item["path"] = Path(value)
            elif key == "branch":
                item["branch"] = value.removeprefix("refs/heads/")
            elif key in ("locked", "prunable"):
                item[key] = True
            elif key == "HEAD":
                item["head"] = value
        if "path" in item:
            items.append(item)
    return items


def last_activity(path: Path) -> float:
    """Newest of: HEAD commit time, git HEAD/index mtime, worktree directory mtime."""
    times = [path.stat().st_mtime]
    gitdir = Path(_git(path, "rev-parse", "--absolute-git-dir").stdout.strip() or path)
    for name in ("HEAD", "index"):
        try:
            times.append((gitdir / name).stat().st_mtime)
        except OSError:
            pass
    commit = _git(path, "log", "-1", "--format=%ct").stdout.strip()
    if commit.isdigit():
        times.append(float(commit))
    return max(times)


def pr_state(repo: Path, wt: Path, branch: str, source: str) -> str:
    """merged | closed | open | none | unknown, from gh or from local git ancestry."""
    if not branch:
        return "none"
    if source == "gh":
        pr = gh_pr_state(repo, branch)
        if pr is None:
            return "unknown"
        return {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open", "NONE": "none"}.get(
            pr["state"], "unknown")
    tip = _git(wt, "rev-parse", "HEAD").stdout.strip()
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = _git(repo, "rev-parse", "--verify", "-q", ref).stdout.strip()
        if base and base != tip and _git(repo, "merge-base", "--is-ancestor", tip, base).returncode == 0:
            return "merged"
    return "none"


def reap_repo(repo: Path, policy: Policy, roots: list[Path], *, dry_run: bool, now: float,
              busy: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {"repo": str(repo), "worktrees": []}
    entries = list_worktrees(repo)
    for item in entries[1:]:  # the first entry is the main checkout
        path: Path = item["path"]
        row: dict[str, Any] = {"path": str(path), "branch": item["branch"]}
        result["worktrees"].append(row)
        if item["prunable"]:
            if not dry_run:
                _git(repo, "worktree", "prune")
            row.update(action="pruned" if not dry_run else "would_prune", reason="missing_directory")
            continue
        if item["locked"]:
            row.update(action="kept", reason="locked")
            continue
        try:
            path = checked_path(path, roots)
            idle = now - last_activity(path)
            if idle < policy.min_idle_hours * 3600:
                row.update(action="kept", reason="recent_activity")
                continue
            state = pr_state(repo, path, item["branch"], policy.pr_source)
            row["pr"] = state
            if state in ("open", "unknown"):
                row.update(action="kept", reason=f"pr_{state}")
                continue
            if state == "none" and idle < policy.ttl_days * 86400:
                row.update(action="kept", reason="orphan_within_ttl")
                continue
            reason = f"pr_{state}" if state != "none" else "orphan_ttl"
            remove_worktree(path, dry_run=dry_run, merged_ok=state in ("merged", "closed"),
                            busy=busy)
            row.update(action="would_remove" if dry_run else "removed", reason=reason)
        except Refusal as exc:
            row.update(action="kept", reason=exc.code)
        except OSError as exc:
            row.update(action="kept", reason=f"error: {exc}")
    return result


def reap_worktrees(policy: Policy, *, dry_run: bool = False, extra_repos: list[Path] | None = None,
                   extra_roots: list[str] | None = None, now: float | None = None) -> dict[str, Any]:
    roots = configured_roots([*policy.roots, *(extra_roots or [])])
    repos = [*policy.repos, *(extra_repos or [])] or find_repos(roots)
    busy = busy_paths() if repos else []
    now = time.time() if now is None else now
    report: dict[str, Any] = {"job": "worktrees", "dry_run": dry_run, "repos": []}
    for repo in repos:
        report["repos"].append(reap_repo(Path(repo), policy, roots, dry_run=dry_run, now=now, busy=busy))
    rows = [w for r in report["repos"] for w in r["worktrees"]]
    report["removed"] = sum(w["action"] in ("removed", "would_remove") for w in rows)
    report["kept"] = sum(w["action"] == "kept" for w in rows)
    report["status"] = "dry_run" if dry_run else "ok"
    return report


def run(policy: Policy, *, only: str | None = None, dry_run: bool = False,
        extra_repos: list[Path] | None = None, extra_roots: list[str] | None = None) -> dict[str, Any]:
    if not policy.enabled:
        return {"status": "disabled", "reason": "disk_hygiene.enabled is false or HERDR_DISK_HYGIENE=0"}
    jobs: list[dict[str, Any]] = []
    if policy.worktrees and only in (None, "worktrees"):
        jobs.append(reap_worktrees(policy, dry_run=dry_run, extra_repos=extra_repos,
                                   extra_roots=extra_roots))
    if policy.docker and only in (None, "docker"):
        from .docker_hygiene import sweep
        jobs.append(sweep(dry_run=dry_run, min_age_seconds=int(policy.docker_min_age_hours * 3600),
                          keep_storage=policy.builder_keep_storage))
    return {"status": "dry_run" if dry_run else "ok", "jobs": jobs}


def _write_status(report: dict[str, Any]) -> None:
    try:
        from .build_coord import _default_state_dir
        state = _default_state_dir()
        state.mkdir(parents=True, exist_ok=True)
        (state / "disk-hygiene.json").write_text(json.dumps({**report, "finished": time.time()}))
    except OSError:
        pass


def register(sub: Any) -> None:
    p = sub.add_parser("disk-hygiene", help="reap finished worktrees and ephemeral docker leftovers")
    actions = p.add_subparsers(dest="disk_hygiene_command", required=True)
    for name in ("run", "status"):
        cp = actions.add_parser(name)
        cp.add_argument("--only", choices=("worktrees", "docker"))
        cp.add_argument("--dry-run", action="store_true")
        cp.add_argument("--json", action="store_true")
        cp.add_argument("--repo", action="append", default=[], help="repository main checkout (repeatable)")
        cp.add_argument("--root", action="append", default=[])
        cp.add_argument("--ttl-days", type=float)
        cp.add_argument("--pr-source", choices=("gh", "git"))
        cp.set_defaults(func=main_args)


def main_args(args: argparse.Namespace) -> int:
    policy = load_policy()
    overrides = {k: v for k, v in (("ttl_days", args.ttl_days), ("pr_source", args.pr_source)) if v}
    if overrides:
        from dataclasses import replace
        policy = replace(policy, **overrides)
    if args.disk_hygiene_command == "status":
        report = {"policy": {"enabled": policy.enabled, "worktrees": policy.worktrees,
                             "docker": policy.docker, "ttl_days": policy.ttl_days,
                             "pr_source": policy.pr_source}}
    else:
        report = run(policy, only=args.only, dry_run=args.dry_run,
                     extra_repos=[Path(r).expanduser() for r in args.repo], extra_roots=args.root)
        if not args.dry_run:
            _write_status(report)
    print(json.dumps(report, indent=None if args.json else 2, default=str))
    return 0

