"""``herdr-eng cleanup``: the one sanctioned way for an agent to delete what it created.

Agents are blocked from raw ``rm -rf`` / ``git worktree remove`` / ``docker rm``. This command
is the allow-listable replacement, so it must never be a bypass:

* only paths inside agent-owned roots (``default_roots`` plus ``disk_hygiene.roots`` from the
  USER-level config file only: no CLI flag, no env var, never repo-local config), never a root
  itself, never ``..``, never through a symlink, only entries owned by the current uid;
* ``tmp`` only under temp roots, and never a git repo/worktree or a directory containing one;
* ``worktree`` refuses repo main checkouts, dirty trees and commits that exist on no remote
  (``--merged-ok`` relaxes the last one only when the work is merged or its PR is closed);
* ``target`` accepts only a Cargo ``target`` with build markers and no tracked files;
* ``docker`` removes only stopped containers labelled ``herdr.ephemeral=1``.

Removal reuses the symlink-safe walk of ``shutil.rmtree`` and the Cargo recognition and lease
rules of ``build_cleanup``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EPHEMERAL_LABEL = "herdr.ephemeral=1"
SCOPES = ("worktree", "target", "docker", "tmp")
_MAIN_REFS = ("origin/HEAD", "origin/main", "origin/master")


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def default_roots(home: Path | None = None) -> list[Path]:
    """Agent-created locations only. ``~/IdeaProjects`` and ``~/work`` hold the user's own repos."""
    home = home or Path.home()
    return [home / ".local" / "share" / "worktrees", home / ".cache" / "herdr-engineering",
            Path(tempfile.gettempdir()) / f"claude-{os.getuid()}"]


def default_temp_roots() -> list[Path]:
    """Where scratch files may live; ``tmp`` scope is confined to these."""
    return [Path(tempfile.gettempdir()), Path("/tmp"), Path(tempfile.gettempdir()) / f"claude-{os.getuid()}"]


def user_config_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "herdr-engineering" / "config.yaml"


def user_disk_hygiene_config(home: Path | None = None) -> dict[str, Any]:
    """``disk_hygiene`` from the user-level config file only (repo-local config is untrusted)."""
    path = user_config_path(home)
    if not path.is_file():
        return {}
    try:
        from .config import load_config
        section = load_config([path]).get("disk_hygiene") or {}
    except Exception:  # noqa: BLE001 - a broken config must not widen or break the command
        return {}
    return section if isinstance(section, dict) else {}


def configured_roots(extra: list[str] | None = None, home: Path | None = None) -> list[Path]:
    """Defaults + ``disk_hygiene.roots`` of the user config. ``extra`` is for in-process callers."""
    roots = default_roots(home)
    roots += [Path(r).expanduser() for r in user_disk_hygiene_config(home).get("roots", []) or []]
    roots += [Path(r).expanduser() for r in extra or []]
    return roots


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


def checked_path(path: Path | str, roots: list[Path]) -> Path:
    """Return the absolute path, or raise ``Refusal``. Never follows a symlink."""
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise Refusal("outside_roots", f"{raw}: '..' is not allowed")
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    target = Path(os.path.abspath(raw))
    for root in roots:
        base = Path(os.path.abspath(root))
        if target == base:
            raise Refusal("is_root", f"{target} is a configured root, not removable")
        if not target.is_relative_to(base):
            continue
        current = base
        for part in target.relative_to(base).parts:
            current = current / part
            if current.is_symlink():
                raise Refusal("symlink", f"{current} is a symlink; refusing to follow it")
        try:
            owner = target.lstat().st_uid
        except FileNotFoundError:
            raise Refusal("not_found", f"{target} does not exist") from None
        if owner != os.getuid():
            raise Refusal("not_owned", f"{target} is not owned by uid {os.getuid()}")
        return target
    raise Refusal("outside_roots", f"{target} is outside the agent-owned roots")


def busy_paths(timeout: float = 60) -> list[Path]:
    """Working directories / executables / open files of this user's processes. Fails CLOSED:
    any inspection failure or timeout raises ``Refusal('in_use')``."""
    if Path("/proc").is_dir():
        from .build_cleanup import _process_paths
        try:
            return _process_paths()
        except OSError as exc:
            raise Refusal("in_use", f"cannot verify running processes: {exc}") from exc
    try:
        out = subprocess.run(["lsof", "-nP", "-u", str(os.getuid()), "-Fn"],
                             capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("in_use", f"cannot verify running processes (lsof): {exc}") from exc
    paths = [Path(line[1:]) for line in out.splitlines() if line.startswith("n/")]
    if not paths:
        raise Refusal("in_use", "cannot verify running processes: lsof returned nothing")
    return paths


def _not_in_use(path: Path, busy: list[Path] | None = None) -> None:
    real = path.resolve()
    if any(p == real or p.is_relative_to(real) for p in (busy_paths() if busy is None else busy)):
        raise Refusal("in_use", f"a running process is using {path}")


def _origin_owner(repo: Path) -> str:
    url = _git(repo, "remote", "get-url", "origin").stdout.strip()
    match = re.search(r"[:/]([^/:]+)/[^/]+?(?:\.git)?/?$", url)
    return match.group(1).lower() if match else ""


def select_pr(prs: list[dict[str, Any]], branch: str, head: str, owner: str) -> dict[str, str]:
    """The newest PR whose head branch, head SHA and head repo owner all match, else NONE."""
    mine = [p for p in prs
            if p.get("headRefName") == branch and head and p.get("headRefOid") == head
            and str((p.get("headRepositoryOwner") or {}).get("login", "")).lower() == owner != ""]
    if not mine:
        return {"state": "NONE", "head": ""}
    mine.sort(key=lambda p: p.get("number", 0), reverse=True)
    return {"state": str(mine[0].get("state", "")).upper(), "head": str(mine[0].get("headRefOid", ""))}


def gh_pr_state(repo: Path, branch: str, head: str, owner: str) -> dict[str, str] | None:
    """PR for exactly this branch, head SHA and origin owner from a real ``gh`` call.

    None means unknown (gh missing/failing): callers keep the worktree. A branch name reused by
    another PR, or a fork PR, does not match and counts as NONE.
    """
    if not branch or shutil.which("gh") is None:
        return None
    try:
        proc = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all",
                               "--json", "state,headRefOid,headRefName,headRepositoryOwner,number",
                               "--limit", "30"],
                              cwd=repo, capture_output=True, text=True, timeout=45, check=False,
                              env={**os.environ, "GH_PROMPT_DISABLED": "1"})
        if proc.returncode != 0:
            return None  # not a GitHub repo, not authenticated, offline: unknown, not "no PR"
        prs = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(prs, list):
        return None
    return select_pr(prs, branch, head, owner)


def worktree_facts(path: Path) -> dict[str, Any]:
    """Git facts for a linked worktree; raises ``Refusal`` for non-worktrees and main checkouts."""
    top = _git(path, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != path.resolve():
        raise Refusal("not_a_worktree", f"{path} is not a git worktree root (not a git worktree)")
    gitdir = _git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if Path(gitdir).resolve() == Path(common).resolve():
        raise Refusal("main_checkout", f"{path} is a repo main checkout; refusing")
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    main = Path(common).resolve().parent
    return {"branch": branch, "common": common, "main": main,
            "head": _git(path, "rev-parse", "HEAD").stdout.strip()}


REBUILDABLE = ("target", "node_modules", ".venv", "__pycache__")


def is_dirty(path: Path) -> bool:
    status = _git(path, "status", "--porcelain")
    return status.returncode != 0 or bool(status.stdout.strip())


def foreign_ignored(path: Path, rebuildable: tuple[str, ...] = REBUILDABLE) -> list[str]:
    """Ignored-but-present entries that are not well-known rebuildable output (``.env``, DBs...)."""
    status = _git(path, "status", "--porcelain", "--ignored")
    if status.returncode != 0:
        return ["<unknown>"]
    found = []
    for line in status.stdout.splitlines():
        if line.startswith("!! "):
            entry = line[3:].rstrip("/")
            if not any(part in rebuildable for part in entry.split("/")):
                found.append(entry)
    return found


def unpushed(path: Path) -> list[str]:
    """Commits reachable from HEAD but from no remote-tracking ref."""
    out = _git(path, "rev-list", "HEAD", "--not", "--remotes")
    if out.returncode != 0:
        return ["<unknown>"]
    return out.stdout.split()


def patch_merged(path: Path) -> bool:
    """Every local commit has a patch-equivalent commit on the remote default branch."""
    for ref in _MAIN_REFS:
        if _git(path, "rev-parse", "--verify", "-q", ref).returncode != 0:
            continue
        cherry = _git(path, "cherry", ref, "HEAD")
        if cherry.returncode == 0 and not any(line.startswith("+") for line in cherry.stdout.splitlines()):
            return True
    return False


def check_worktree_removable(path: Path, *, merged_ok: bool,
                             busy: list[Path] | None = None) -> dict[str, Any]:
    facts = worktree_facts(path)
    if is_dirty(path):
        raise Refusal("dirty", f"{path} has uncommitted or untracked changes")
    extra = tuple(user_disk_hygiene_config().get("rebuildable", []) or [])
    if ignored := foreign_ignored(path, (*REBUILDABLE, *extra)):
        raise Refusal("ignored_files",
                      f"{path} has ignored files that are not rebuildable output: {', '.join(ignored[:5])}")
    if unpushed(path):
        merged = False
        if merged_ok:
            pr = gh_pr_state(facts["main"], facts["branch"], facts["head"],
                             _origin_owner(facts["main"]))
            merged = patch_merged(path) or bool(pr and pr["state"] in ("MERGED", "CLOSED")
                                                and pr["head"] == facts["head"])
        if not merged:
            raise Refusal("unpushed_commits",
                          f"{path} has commits that are on no remote and not merged (unpushed)")
    _not_in_use(path, busy)
    return facts


def remove_worktree(path: Path, *, dry_run: bool, merged_ok: bool,
                    busy: list[Path] | None = None) -> dict[str, Any]:
    facts = check_worktree_removable(path, merged_ok=merged_ok, busy=busy)
    if not dry_run:
        done = _git(facts["main"], "worktree", "remove", str(path), timeout=600)
        if done.returncode != 0:
            raise Refusal("git_refused", done.stderr.strip() or "git worktree remove failed")
        _git(facts["main"], "worktree", "prune")
    return {"branch": facts["branch"]}


def _remove_target(path: Path, *, dry_run: bool, state_dir: Path | None) -> dict[str, Any]:
    from .build_cleanup import _lease_block, _recognized
    if not _recognized(path):
        raise Refusal("not_cargo_target", f"{path} is not a cargo target directory")
    tracked = _git(path.parent, "ls-files", "--", "target")
    if tracked.returncode == 0 and tracked.stdout.strip():
        raise Refusal("tracked_files", f"{path} contains tracked files")
    if state_dir is not None and (reason := _lease_block(state_dir, path)):
        raise Refusal(reason, f"{path} is protected by the build registry")
    _not_in_use(path)
    if not dry_run:
        _rmtree(path)
    return {}


def _rmtree(path: Path) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise Refusal("unsafe_platform", "this platform's rmtree is not symlink-attack safe")
    if stat.S_ISDIR(path.lstat().st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


_GIT_WALK_CAP = 200_000


def _refuse_git(path: Path, stop: list[Path]) -> None:
    """``tmp`` never deletes a repo/worktree, or something inside or containing one."""
    for parent in (path, *path.parents):
        if any(parent == s for s in stop):
            break
        if (parent / ".git").exists() or (parent / ".git").is_symlink():
            raise Refusal("git_repository", f"{path} is or is inside a git repository; use --scope worktree")
    seen = 0
    for _, dirs, files in os.walk(path, followlinks=False):
        seen += len(dirs) + len(files)
        if ".git" in dirs or ".git" in files:
            raise Refusal("git_repository", f"{path} contains a git repository; use --scope worktree")
        if seen > _GIT_WALK_CAP:
            raise Refusal("too_large", f"{path} is too large to verify it holds no git repository")


def _remove_tmp(path: Path, *, dry_run: bool, temp_roots: list[Path]) -> dict[str, Any]:
    _refuse_git(path, temp_roots)
    _not_in_use(path)
    if not dry_run:
        _rmtree(path)
    return {}


def _remove_docker(name: str, *, dry_run: bool) -> dict[str, Any]:
    from .docker_hygiene import remove_ephemeral_container
    return remove_ephemeral_container(name, dry_run=dry_run)


def cleanup(scope: str, path: Path | str | None, *, roots: list[Path], dry_run: bool = False,
            merged_ok: bool = False, state_dir: Path | None = None,
            temp_roots: list[Path] | None = None) -> dict[str, Any]:
    """Remove one thing; returns a report dict or raises ``Refusal``."""
    if scope not in SCOPES:
        raise Refusal("bad_scope", f"scope must be one of {', '.join(SCOPES)}")
    if scope == "docker":
        if not path:
            raise Refusal("missing_path", "docker scope needs --path <container name or id>")
        extra = _remove_docker(str(path), dry_run=dry_run)
        return {"scope": scope, "path": str(path), "dry_run": dry_run,
                "action": "would_remove" if dry_run else "removed", **extra}
    if not path:
        raise Refusal("missing_path", f"{scope} scope needs --path")
    temp = default_temp_roots() if temp_roots is None else temp_roots
    target = checked_path(path, temp if scope == "tmp" else roots)
    if scope == "worktree":
        extra = remove_worktree(target, dry_run=dry_run, merged_ok=merged_ok)
    elif scope == "target":
        extra = _remove_target(target, dry_run=dry_run, state_dir=state_dir)
    else:
        extra = _remove_tmp(target, dry_run=dry_run, temp_roots=temp)
    return {"scope": scope, "path": str(target), "dry_run": dry_run,
            "action": "would_remove" if dry_run else "removed", **extra}


def register(sub: Any) -> None:
    p = sub.add_parser("cleanup", help="remove a worktree/target/container/scratch dir you created")
    _arguments(p)
    p.set_defaults(func=lambda args: main_args(args))


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scope", choices=SCOPES)
    p.add_argument("--print-run-args", action="store_true",
                   help="print the docker run arguments that make a container reapable")
    p.add_argument("--path", help="path (container name or id for docker)")
    p.add_argument("--merged-ok", action="store_true",
                   help="worktree: allow commits on no remote when merged or the PR is closed")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")


def main_args(args: argparse.Namespace) -> int:
    if args.print_run_args:
        from .docker_hygiene import run_args
        print(" ".join(run_args()))
        return 0
    if not args.scope:
        print("cleanup: --scope is required", file=sys.stderr)
        return 2
    try:
        from .build_coord import _default_state_dir
        out = cleanup(args.scope, args.path, roots=configured_roots(), dry_run=args.dry_run,
                      merged_ok=args.merged_ok, state_dir=_default_state_dir())
    except Refusal as exc:
        if args.json:
            print(json.dumps({"scope": args.scope, "path": args.path, "refused": exc.code,
                              "message": exc.message}))
        else:
            print(f"refused: {exc}", file=sys.stderr)
        return 2 if exc.code == "missing_path" else 1
    if args.json:
        print(json.dumps(out))
    else:
        print(f"{out['action']}: {out['scope']} {out['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-eng cleanup")
    _arguments(parser)
    return main_args(parser.parse_args(argv))
