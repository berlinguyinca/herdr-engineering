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
import re
import subprocess
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from .cleanup_command import (
    Refusal,
    _git,
    _origin_owner,
    absolute_roots,
    checked_path,
    config_strings,
    configured_roots,
    default_temp_worktree_parents,
    gh_pr_state,
    remove_worktree,
    reset_excludes_cache,
    user_disk_hygiene_config,
    worktree_quick_facts,
)

_DISCOVERY_DEPTH = 2


class PrSource(StrEnum):
    GH = "gh"
    GIT = "git"  # offline: local ancestry only


class PrState(StrEnum):
    MERGED = "merged"
    CLOSED = "closed"
    OPEN = "open"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    worktrees: bool = True
    docker: bool = True
    ttl_days: float = 7.0
    min_idle_hours: float = 2.0
    pr_source: PrSource = PrSource.GH
    repos: tuple[Path, ...] = ()
    roots: tuple[str, ...] = ()
    docker_min_age_hours: float = 1.0
    builder_keep_storage: str = "5GB"
    builder_prune: bool = False
    image_prune: bool = False
    rebuildable: tuple[str, ...] = ()
    config_error: str = ""


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} must be a non-negative number, got {value!r}")
    return float(value)


_strings = config_strings


_TOP_KEYS = frozenset({"enabled", "repos", "roots", "worktree_ttl_days", "min_idle_hours", "pr_source",
                       "worktrees", "docker", "rebuildable"})
_SECTION_KEYS = {"worktrees": frozenset({"enabled"}),
                 "docker": frozenset({"enabled", "min_age_hours", "builder_prune", "builder_keep_storage",
                                      "image_prune"})}
_STORAGE = re.compile(r"\d+(?:\.\d+)?\s*(?:[KMGT]i?B)", re.IGNORECASE)


def _known(section: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """A misspelled opt-out (``enable: false``) must not silently leave the job on."""
    if unknown := sorted(str(key) for key in section.keys() - allowed):
        raise ValueError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """``docker: false`` is an opt-out; ``docker: {enabled: false}`` too; anything else odd fails."""
    value = cfg.get(name)
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"enabled": value}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping or true/false, got {value!r}")
    _known(value, _SECTION_KEYS[name], name)
    return value


def _parse_policy(cfg: dict[str, Any]) -> Policy:
    _known(cfg, _TOP_KEYS, "disk_hygiene")
    wt, dk = _section(cfg, "worktrees"), _section(cfg, "docker")
    storage = dk.get("builder_keep_storage", "5GB")
    if not isinstance(storage, str) or not _STORAGE.fullmatch(storage.strip()):
        raise ValueError(f"docker.builder_keep_storage must look like 5GB, got {storage!r}")
    off = os.environ.get("HERDR_DISK_HYGIENE", "1").strip().lower() in ("0", "false", "no", "off")
    return Policy(
        enabled=_flag(cfg.get("enabled", True), "enabled") and not off,
        worktrees=_flag(wt.get("enabled", True), "worktrees.enabled"),
        docker=_flag(dk.get("enabled", True), "docker.enabled"),
        ttl_days=_number(cfg.get("worktree_ttl_days", 7), "worktree_ttl_days"),
        min_idle_hours=_number(cfg.get("min_idle_hours", 2), "min_idle_hours"),
        pr_source=PrSource(cfg.get("pr_source", "gh")),
        repos=tuple(Path(r).expanduser() for r in _strings(cfg.get("repos"), "repos")),
        roots=absolute_roots(cfg.get("roots")),
        docker_min_age_hours=_number(dk.get("min_age_hours", 1), "docker.min_age_hours"),
        builder_keep_storage=storage.strip(),
        builder_prune=_flag(dk.get("builder_prune", False), "docker.builder_prune"),
        image_prune=_flag(dk.get("image_prune", False), "docker.image_prune"),
        rebuildable=_strings(cfg.get("rebuildable"), "rebuildable"),
    )


def load_policy(cfg: dict[str, Any] | None = None) -> Policy:
    """Strictly typed. Any unreadable or malformed value fails CLOSED (``config_error`` set, every
    destructive job off) instead of reading a broken opt-out as "enabled"."""
    try:
        return _parse_policy(user_disk_hygiene_config() if cfg is None else cfg)
    except Refusal as exc:
        message = exc.message
    except (ValueError, TypeError, AttributeError) as exc:
        message = f"disk_hygiene config is invalid: {exc}"
    return Policy(enabled=False, worktrees=False, docker=False, config_error=message)


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


def pr_state(repo: Path, wt: Path, branch: str, head: str, source: PrSource) -> PrState:
    """merged | closed | open | none | unknown, from gh or from local git ancestry."""
    if not branch:
        return PrState.NONE
    if source == PrSource.GH:
        pr = gh_pr_state(wt, branch, head, _origin_owner(wt))
        if pr is None:
            return PrState.UNKNOWN
        return {"MERGED": PrState.MERGED, "CLOSED": PrState.CLOSED, "OPEN": PrState.OPEN,
                "NONE": PrState.NONE}.get(pr["state"], PrState.UNKNOWN)
    tip = head
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = _git(wt, "rev-parse", "--verify", "-q", ref).stdout.strip()
        if base and base != tip and _git(wt, "merge-base", "--is-ancestor", tip, base).returncode == 0:
            return PrState.MERGED
    return PrState.NONE


def unpushed_ok(state: PrState) -> bool:
    """Only a MERGED PR excuses commits that are on no remote. A CLOSED one may have been closed
    to be reworked (and its branch deleted): those commits then live only here."""
    return state is PrState.MERGED


def reap_repo(repo: Path, policy: Policy, roots: list[Path], *, dry_run: bool, now: float,
              temp_worktree_parents: list[Path] | None = None) -> dict[str, Any]:
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
            facts = worktree_quick_facts(path, policy.rebuildable)  # every local refusal, before any network call
            state = pr_state(repo, path, item["branch"], facts["head"], policy.pr_source)
            row["pr"] = state.value
            if state in (PrState.OPEN, PrState.UNKNOWN):
                row.update(action="kept", reason=f"pr_{state}")
                continue
            if state is PrState.NONE and idle < policy.ttl_days * 86400:
                row.update(action="kept", reason="orphan_within_ttl")
                continue
            reason = f"pr_{state}" if state is not PrState.NONE else "orphan_ttl"
            merged = unpushed_ok(state)
            # no shared snapshot: the process check inside remove_worktree is taken fresh, last.
            # HEAD and the local state are re-checked after the lookup; the PR head must still be this HEAD.
            remove_worktree(path, dry_run=dry_run, merged_ok=merged, rebuildable=policy.rebuildable,
                            pr_head=facts["head"] if merged else None, prechecked=facts)
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
                   temp_worktree_parents: list[Path] | None = None,
                   scratch_root: Path | None = None) -> dict[str, Any]:
    reset_excludes_cache()
    roots = configured_roots([*policy.roots, *(extra_roots or [])], scratch=scratch_root)
    parents = default_temp_worktree_parents() if temp_worktree_parents is None else temp_worktree_parents
    repos = [*policy.repos, *(extra_repos or [])] or find_repos(roots, parents)
    now = time.time() if now is None else now
    report: dict[str, Any] = {"job": "worktrees", "dry_run": dry_run, "repos": []}
    for repo in repos:
        try:
            report["repos"].append(reap_repo(Path(repo), policy, roots, dry_run=dry_run, now=now,
                                             temp_worktree_parents=parents))
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
        cp.add_argument("--pr-source", choices=[s.value for s in PrSource])
        cp.set_defaults(func=main_args)


def main_args(args: argparse.Namespace) -> int:
    policy = load_policy()
    overrides = {k: v for k, v in (("ttl_days", args.ttl_days), ("pr_source", args.pr_source))
                 if v is not None}
    if overrides:
        if "pr_source" in overrides:
            overrides["pr_source"] = PrSource(overrides["pr_source"])
        policy = replace(policy, **overrides)
    if args.disk_hygiene_command == "status":
        report = {"policy": {"enabled": policy.enabled, "worktrees": policy.worktrees,
                             "docker": policy.docker, "ttl_days": policy.ttl_days,
                             "pr_source": policy.pr_source}}
        if policy.config_error:
            report["status"] = "config_unreadable"
            report["config_error"] = policy.config_error
    else:
        try:
            report = run(policy, only=args.only, dry_run=args.dry_run,
                         extra_repos=[Path(r).expanduser() for r in args.repo])
        except (Refusal, OSError, subprocess.SubprocessError) as exc:
            report = {"status": "error", "reason": str(exc)}
        if not args.dry_run:
            _write_status(report)  # always, so the file never shows a stale "last good" report
    print(json.dumps(report, indent=None if args.json else 2, default=str))
    return 1 if report.get("status") in ("error", "config_unreadable") else 0

