"""Default-on disk hygiene: the worktree reaper and the docker sweep.

The reaper never decides by path. For each repository it lists ``git worktree list`` and removes a
worktree (its in-tree ``target`` goes with it) only when git and PR state agree it is finished:

* PR merged or closed, tree clean, nothing unpushed; or
* no PR at all, idle for ``worktree_ttl_days`` (default 7), tree clean, fully pushed.

An open PR, an unknown PR state (gh missing, offline), recent activity, a running process, a
dirty tree, hidden (skip-worktree) edits or an unpushed commit all keep the worktree. The safety
checks are the same ``cleanup_command`` / ``scratch_safety`` code an agent hits, so the reaper
cannot do what the command refuses. It never runs ``git worktree prune``: registrations whose
directory is missing are reported, not deleted (they may sit on an unmounted volume).

An unreadable user config disables the destructive jobs (status ``config_unreadable``).

Config (``disk_hygiene`` in the herdr-eng config; everything optional, default on)::

    disk_hygiene: {enabled: true, repos: [], roots: [], worktree_ttl_days: 7,
                   min_idle_hours: 2, pr_source: gh,          # gh | git (offline, ancestry only)
                   worktrees: {enabled: true},
                   docker: {enabled: true, min_age_hours: 1,
                            builder_prune: false, builder_keep_storage: 5GB,   # opt-in, unlabelled
                            image_prune: false}}                               # opt-in, unlabelled

Opt out entirely with ``disk_hygiene.enabled: false`` in the USER config file
(``~/.config/herdr-engineering/config.yaml``); the scheduled jobs read exactly that file.
``HERDR_DISK_HYGIENE=0`` only affects interactive runs: a shell export never reaches a
launchd/systemd job. Repo-local config is never read here.

Only worktrees inside the agent roots (``cleanup_command.default_roots`` plus the user config's
``disk_hygiene.roots``, plus autospec's ``/tmp/wt-*``) are ever reaped; the user's own repos and
worktrees are not.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cleanup_command import (
    Refusal,
    _git,
    _origin_owner,
    checked_path,
    configured_roots,
    default_temp_worktree_parents,
    gh_pr_state,
    in_use_snapshot,
    remove_worktree,
    user_disk_hygiene_config,
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
    builder_prune: bool = False
    image_prune: bool = False
    config_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def load_policy(cfg: dict[str, Any] | None = None) -> Policy:
    if cfg is None:
        try:
            cfg = user_disk_hygiene_config()  # user-level file only
        except Refusal as exc:  # unreadable: fail CLOSED, the destructive jobs do not run
            return Policy(enabled=False, worktrees=False, docker=False, config_error=exc.message)
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
        builder_prune=bool(dk.get("builder_prune", False)),
        image_prune=bool(dk.get("image_prune", False)),
    )


def _repo_handle(common: Path) -> Path:
    """What ``git -C`` should be pointed at: the checkout for a normal repo, the git dir itself
    for bare and separate-git-dir layouts."""
    common = common.resolve()
    return common.parent if common.name == ".git" else common


def find_repos(roots: list[Path], temp_worktree_parents: list[Path] | None = None) -> list[Path]:
    """Repositories near the roots, plus the one behind every linked worktree found there (agent
    worktrees live in an agent root, their repo usually does not). Discovery only, never a
    removal rule. ``<parent>/wt-*`` directories of ``temp_worktree_parents`` are scanned too."""
    found: set[Path] = set()
    starts = [(Path(os.path.abspath(root)), 0) for root in roots]
    for parent in temp_worktree_parents or []:
        try:
            starts += [(Path(e.path), _DISCOVERY_DEPTH) for e in os.scandir(parent)
                       if e.name.startswith("wt-") and e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
    for start in starts:
        pending = [start]
        while pending:
            directory, depth = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            if any(e.name == ".git" and e.is_dir(follow_symlinks=False) for e in entries):
                found.add(directory)
                continue
            if any(e.name == ".git" and e.is_file(follow_symlinks=False) for e in entries):
                common = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
                if common.returncode == 0 and common.stdout.strip():
                    found.add(_repo_handle(Path(common.stdout.strip())))
                continue
            if depth < _DISCOVERY_DEPTH:
                pending += [(Path(e.path), depth + 1) for e in entries
                            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    return sorted(found)


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
        pr = gh_pr_state(wt, branch, _git(wt, "rev-parse", "HEAD").stdout.strip(),
                         _origin_owner(wt))
        if pr is None:
            return "unknown"
        return {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open", "NONE": "none"}.get(
            pr["state"], "unknown")
    tip = _git(wt, "rev-parse", "HEAD").stdout.strip()
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = _git(wt, "rev-parse", "--verify", "-q", ref).stdout.strip()
        if base and base != tip and _git(wt, "merge-base", "--is-ancestor", tip, base).returncode == 0:
            return "merged"
    return "none"


def reap_repo(repo: Path, policy: Policy, roots: list[Path], *, dry_run: bool, now: float,
              snapshot: Any, temp_worktree_parents: list[Path] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"repo": str(repo), "worktrees": []}
    entries = list_worktrees(repo)
    for item in entries[1:]:  # the first entry is the main checkout
        path: Path = item["path"]
        row: dict[str, Any] = {"path": str(path), "branch": item["branch"]}
        result["worktrees"].append(row)
        if item["prunable"]:  # directory missing: maybe an unmounted volume. Never pruned here.
            row.update(action="kept", reason="missing_directory")
            continue
        if item["locked"]:
            row.update(action="kept", reason="locked")
            continue
        try:
            path = checked_path(path, roots, temp_worktree_parents)
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
                            snapshot=snapshot)
            row.update(action="would_remove" if dry_run else "removed", reason=reason)
        except Refusal as exc:
            row.update(action="kept", reason=exc.code)
        except subprocess.TimeoutExpired:
            row.update(action="kept", reason="timeout")
        except OSError as exc:
            row.update(action="kept", reason=f"error: {exc}")
    return result


def reap_worktrees(policy: Policy, *, dry_run: bool = False, extra_repos: list[Path] | None = None,
                   extra_roots: list[str] | None = None, now: float | None = None,
                   temp_worktree_parents: list[Path] | None = None) -> dict[str, Any]:
    roots = configured_roots([*policy.roots, *(extra_roots or [])])
    parents = default_temp_worktree_parents() if temp_worktree_parents is None else temp_worktree_parents
    repos = [*policy.repos, *(extra_repos or [])] or find_repos(roots, parents)
    snapshot: Any = None
    if repos:
        try:
            snapshot = in_use_snapshot()
        except Refusal as exc:  # fail closed: nothing is removed, but the run still reports
            snapshot = exc
    now = time.time() if now is None else now
    report: dict[str, Any] = {"job": "worktrees", "dry_run": dry_run, "repos": []}
    for repo in repos:
        try:
            report["repos"].append(reap_repo(Path(repo), policy, roots, dry_run=dry_run, now=now,
                                             snapshot=snapshot, temp_worktree_parents=parents))
        except (Refusal, OSError, subprocess.SubprocessError) as exc:
            report["repos"].append({"repo": str(repo), "worktrees": [], "error": str(exc)})
    rows = [w for r in report["repos"] for w in r["worktrees"]]
    report["removed"] = sum(w["action"] in ("removed", "would_remove") for w in rows)
    report["kept"] = sum(w["action"] == "kept" for w in rows)
    report["status"] = "dry_run" if dry_run else "ok"
    return report


def run(policy: Policy, *, only: str | None = None, dry_run: bool = False,
        extra_repos: list[Path] | None = None, extra_roots: list[str] | None = None) -> dict[str, Any]:
    if policy.config_error:
        return {"status": "config_unreadable", "reason": policy.config_error}
    if not policy.enabled:
        return {"status": "disabled", "reason": "disk_hygiene.enabled is false (user config) or HERDR_DISK_HYGIENE=0"}
    jobs: list[dict[str, Any]] = []
    if policy.worktrees and only in (None, "worktrees"):
        try:
            jobs.append(reap_worktrees(policy, dry_run=dry_run, extra_repos=extra_repos,
                                       extra_roots=extra_roots))
        except (Refusal, OSError, subprocess.SubprocessError) as exc:
            jobs.append({"job": "worktrees", "status": "error", "reason": str(exc)})
    if policy.docker and only in (None, "docker"):
        from .docker_hygiene import sweep
        try:
            jobs.append(sweep(dry_run=dry_run, min_age_seconds=int(policy.docker_min_age_hours * 3600),
                              keep_storage=policy.builder_keep_storage, builder_prune=policy.builder_prune,
                              image_prune=policy.image_prune))
        except (OSError, subprocess.SubprocessError) as exc:
            jobs.append({"job": "docker", "status": "error", "reason": str(exc)})
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
        try:
            report = run(policy, only=args.only, dry_run=args.dry_run,
                         extra_repos=[Path(r).expanduser() for r in args.repo])
        except (Refusal, OSError, subprocess.SubprocessError) as exc:
            report = {"status": "error", "reason": str(exc)}
        if not args.dry_run:
            _write_status(report)  # always, so the file never shows a stale "last good" report
    print(json.dumps(report, indent=None if args.json else 2, default=str))
    return 1 if report.get("status") == "error" else 0

