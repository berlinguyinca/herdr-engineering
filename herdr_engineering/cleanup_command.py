"""``herdr-eng cleanup``: the one sanctioned way for an agent to delete what it created.

Agents are blocked from raw ``rm -rf`` / ``git worktree remove`` / ``docker rm``. This command
is the allow-listable replacement, so it must never be a bypass:

* only paths inside agent-owned roots (``default_roots`` plus ``disk_hygiene.roots`` from the
  USER-level config file only: no CLI flag, no env var, never repo-local config; ``worktree``
  also accepts ``/tmp/wt-*``, where autospec makes its worktrees), never a root itself, never
  ``..``, never through a symlink, only entries owned by the current uid;
* ``tmp`` only under temp roots, never a git repo/bare repo/worktree or a directory containing
  one, and under the Claude scratch root only strictly inside a live-checked ``<session>`` dir;
* ``worktree`` refuses repo main checkouts, dirty trees, skip-worktree/assume-unchanged edits
  and commits that exist on no remote (``--merged-ok`` relaxes the last one only when the work
  is merged or its PR is closed);
* process, hidden-index and scratch-session checks reuse ``scratch_safety`` / ``scratch_reaper``
  (one safety contract, not two);
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
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .scratch_safety import Snapshot

EPHEMERAL_LABEL = "herdr.ephemeral=1"


class Scope(StrEnum):
    WORKTREE = "worktree"
    TARGET = "target"
    DOCKER = "docker"
    TMP = "tmp"


SCOPES = tuple(scope.value for scope in Scope)
_MAIN_REFS = ("origin/HEAD", "origin/main", "origin/master")
GIT_TIMEOUT = 60
TEMP_WORKTREE_PREFIX = "wt-"


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def scratch_root() -> Path:
    """Claude's scratch root, resolved exactly as ``scratch_reaper`` does (``/tmp/claude-<uid>``)."""
    from .scratch_reaper import default_root
    return default_root()


def default_roots(home: Path | None = None) -> list[Path]:
    """Agent-created locations only. ``~/IdeaProjects`` and ``~/work`` hold the user's own repos."""
    home = home or Path.home()
    return [home / ".local" / "share" / "worktrees", home / ".cache" / "herdr-engineering",
            scratch_root()]


def default_temp_roots() -> list[Path]:
    """Where scratch files may live; ``tmp`` scope is confined to these."""
    return [Path(tempfile.gettempdir()), Path("/tmp"), scratch_root()]


def default_temp_worktree_parents() -> list[Path]:
    """Directories whose ``wt-*`` children are agent worktrees (autospec uses ``/tmp/wt-<branch>``)."""
    return [Path("/tmp")]


def user_config_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "herdr-engineering" / "config.yaml"


def user_disk_hygiene_config(home: Path | None = None) -> dict[str, Any]:
    """``disk_hygiene`` from the user-level config file only (repo-local config is untrusted).

    An unreadable file raises ``Refusal('config_unreadable')``: destructive jobs fail CLOSED
    instead of reading a broken opt-out as "enabled"."""
    path = user_config_path(home)
    if not path.is_file():
        return {}
    try:
        from .config import load_config
        section = load_config([path]).get("disk_hygiene") or {}
    except Exception as exc:  # noqa: BLE001 - any failure to read the user's opt-outs
        raise Refusal("config_unreadable", f"{path} cannot be read ({exc}); fix it first") from exc
    if not isinstance(section, dict):
        raise Refusal("config_unreadable", f"{path}: disk_hygiene must be a mapping")
    return section


def configured_roots(extra: list[str] | None = None, home: Path | None = None) -> list[Path]:
    """Defaults + ``disk_hygiene.roots`` of the user config. ``extra`` is for in-process callers."""
    roots = default_roots(home)
    roots += [Path(r).expanduser() for r in user_disk_hygiene_config(home).get("roots", []) or []]
    roots += [Path(r).expanduser() for r in extra or []]
    return roots


def _git(cwd: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Every git call goes through the hardened ``scratch_safety.git`` runner: ``GIT_*`` stripped,
    no user/system config, no fsmonitor or hooks, ``LC_ALL=C``. A ``GIT_DIR`` in the environment
    or ``status.showUntrackedFiles=no`` in a config must not change what a check sees."""
    from .scratch_safety import git as safe_git
    return safe_git(cwd, *args, timeout=GIT_TIMEOUT if timeout is None else timeout)


def config_strings(value: Any, name: str) -> tuple[str, ...]:
    """A config list of strings; anything else (a scalar string would split into characters) is invalid."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(value)


def _bases(paths: list[Path]) -> list[Path]:
    """Each path as spelled and canonicalised (macOS ``/tmp`` is ``/private/tmp``)."""
    found: list[Path] = []
    for p in paths:
        for base in (Path(os.path.abspath(p)), Path(os.path.realpath(p))):
            if base not in found:
                found.append(base)
    return found


def relative_parts(path: Path, base: Path) -> tuple[str, ...] | None:
    """``path`` below ``base`` as the parts of ``path``'s own spelling, or None. Ancestors are
    matched by device/inode, not by text: on case-insensitive volumes (default macOS APFS)
    ``/TMP/Claude-501`` and ``/tmp/claude-501`` are the same directory."""
    try:
        base_stat = base.lstat()
    except OSError:
        return None
    names: list[str] = []
    current = path
    while True:
        try:
            if os.path.samestat(current.lstat(), base_stat):
                return tuple(reversed(names))
        except OSError:
            pass
        if current.parent == current:
            return None
        names.append(current.name)
        current = current.parent


def _verified(target: Path, depth: int) -> Path:
    current = target
    for _ in range(depth):
        if current.is_symlink():
            raise Refusal("symlink", f"{current} is a symlink; refusing to follow it")
        current = current.parent
    try:
        owner = target.lstat().st_uid
    except FileNotFoundError:
        raise Refusal("not_found", f"{target} does not exist") from None
    if owner != os.getuid():
        raise Refusal("not_owned", f"{target} is not owned by uid {os.getuid()}")
    return target


def checked_path(path: Path | str, roots: list[Path],
                 temp_worktree_parents: list[Path] | None = None) -> Path:
    """Return the absolute path, or raise ``Refusal``. Never follows a symlink.

    ``temp_worktree_parents``: ``<parent>/wt-*`` children are acceptable too (worktree scope)."""
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise Refusal("outside_roots", f"{raw}: '..' is not allowed")
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    target = Path(os.path.abspath(raw))
    canonical = Path(os.path.realpath(target.parent)) / target.name  # symlinks above the roots
    for candidate in dict.fromkeys((target, canonical)):
        for base in _bases(roots):
            parts = relative_parts(candidate, base)
            if parts == ():
                raise Refusal("is_root", f"{candidate} is a configured root, not removable")
            if parts:
                return _verified(candidate, len(parts))
        for parent in _bases(temp_worktree_parents or []):
            if candidate.name.startswith(TEMP_WORKTREE_PREFIX) and relative_parts(candidate, parent) == (candidate.name,):
                return _verified(candidate, 1)
    raise Refusal("outside_roots", f"{target} is outside the agent-owned roots")


def _ancestors() -> frozenset[int]:
    """This process's ancestors. The shell that ran ``cd / && herdr-eng cleanup --path X`` names X
    in its own command line; that is the caller, not a user of X."""
    found: set[int] = set()
    pid = os.getppid()
    while pid > 1 and pid not in found and len(found) < 64:
        found.add(pid)
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=15, check=False).stdout.strip()
        pid = int(out) if out.isdigit() else 0
    return frozenset(found)


def _argument_lines(ignore: frozenset[int]) -> tuple[str, ...]:
    """Command lines of other processes (they name paths too), best effort."""
    from .scratch_safety import _comparison
    try:
        listing = subprocess.run(["ps", "-ww", "-axo", "pid=,args="], capture_output=True, text=True,
                                 timeout=15, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    lines = []
    for line in listing.splitlines():
        pieces = line.strip().split(None, 1)
        if len(pieces) == 2 and pieces[0].isdigit() and int(pieces[0]) not in ignore | {os.getpid()}:
            lines.append(_comparison(pieces[1]))
    return tuple(lines)


def in_use_snapshot() -> Snapshot:
    """The ``scratch_safety`` process snapshot (open files, cwds, argv). Where ``lsof`` is
    unavailable (Linux without it) it falls back to the open files and working directories in
    ``/proc`` plus the ``ps`` command lines; with neither source it fails CLOSED:
    ``Refusal('in_use')``."""
    from .scratch_safety import InspectionUnavailable, Snapshot, _comparison, process_snapshot
    ancestors: frozenset[int] = frozenset()
    try:
        ancestors = _ancestors()
        return process_snapshot(ancestors)
    except (InspectionUnavailable, OSError, ValueError, subprocess.SubprocessError) as exc:
        proc = Path("/proc")
        if not proc.is_dir():
            raise Refusal("in_use", f"cannot verify running processes: {exc}") from exc
        from .build_cleanup import _process_paths
        try:
            paths = _process_paths(proc)
        except (OSError, ValueError) as inner:
            raise Refusal("in_use", f"cannot verify running processes: {inner}") from inner
        return Snapshot(tuple(_comparison(str(p)) for p in paths),
                        _argument_lines(ancestors), ())


def _not_in_use(path: Path, snapshot: Snapshot | Refusal | None = None) -> None:
    if isinstance(snapshot, Refusal):
        raise snapshot
    snap = snapshot or in_use_snapshot()
    if any(snap.uses(p) for p in dict.fromkeys((path, Path(os.path.realpath(path))))):
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
    if not gitdir or not common:
        raise Refusal("not_a_worktree", f"{path}: git did not report its directories")
    if Path(gitdir).resolve() == Path(common).resolve():
        raise Refusal("main_checkout", f"{path} is a repo main checkout; refusing")
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    return {"branch": branch, "common": common,
            "head": _git(path, "rev-parse", "HEAD").stdout.strip()}


REBUILDABLE = ("target", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
               ".ruff_cache", ".mypy_cache", ".tox", ".nox", "*.egg-info", "dist", "build",
               "coverage", ".coverage", "htmlcov", ".gradle", ".next", ".turbo", ".cache")


def is_dirty(path: Path) -> bool:
    status = _git(path, "status", "--porcelain", "--untracked-files=all")
    return status.returncode != 0 or bool(status.stdout.strip())


def foreign_ignored(path: Path, rebuildable: tuple[str, ...] = REBUILDABLE) -> list[str]:
    """Ignored-but-present entries that are not plain rebuildable output.

    Git collapses a wholly ignored directory to one entry, so the last component is the thing
    that is ignored; only that name is matched against the (glob) rebuildable list. A matching
    directory is still looked inside: a nested repository (an editable checkout in ``.venv``,
    a vendored clone in ``build``) or an unverifiable tree makes it foreign."""
    from fnmatch import fnmatchcase

    from .scratch_safety import inspect_tree
    status = _git(path, "status", "--porcelain", "--untracked-files=normal", "--ignored=traditional")
    if status.returncode != 0:
        return ["<unknown>"]
    found = []
    for line in status.stdout.splitlines():
        if not line.startswith("!! "):
            continue
        entry = line[3:].rstrip("/")
        if not any(fnmatchcase(entry.rsplit("/", 1)[-1], pattern) for pattern in rebuildable):
            found.append(entry)
            continue
        full = path / entry
        if full.is_symlink() or not full.is_dir():
            continue
        try:
            if inspect_tree(full, accounting_only=True).repositories:
                found.append(entry)
        except OSError:
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


def worktree_local_checks(path: Path, rebuildable: tuple[str, ...] | None = None) -> dict[str, Any]:
    """The cheap, local, no-network refusals. ``rebuildable``: the user's extra patterns (the
    caller reads the config once; None reads it here)."""
    from .scratch_safety import hidden_index_reason
    facts = worktree_facts(path)
    if is_dirty(path):
        raise Refusal("dirty", f"{path} has uncommitted or untracked changes")
    if reason := hidden_index_reason(path):
        raise Refusal("hidden_index_state",
                      f"{path} has skip-worktree/assume-unchanged entries whose edits git hides ({reason})")
    if rebuildable is None:
        try:
            rebuildable = config_strings(user_disk_hygiene_config().get("rebuildable"), "rebuildable")
        except ValueError as exc:
            raise Refusal("config_unreadable", f"disk_hygiene.{exc}") from exc
    extra = rebuildable
    if ignored := foreign_ignored(path, (*REBUILDABLE, *extra)):
        raise Refusal("ignored_files",
                      f"{path} has ignored files that are not plain rebuildable output: {', '.join(ignored[:5])}")
    return facts


def check_worktree_removable(path: Path, *, merged_ok: bool,
                             snapshot: Snapshot | Refusal | None = None,
                             rebuildable: tuple[str, ...] | None = None,
                             pr_head: str | None = None,
                             prechecked: dict[str, Any] | None = None) -> dict[str, Any]:
    """``pr_head``: the head SHA of a merged/closed PR the caller already looked up; it only
    excuses unpushed commits when it equals the HEAD being removed NOW (None looks the PR up here
    when ``merged_ok``). ``prechecked``: the facts from ``worktree_local_checks``; then only HEAD
    and cleanliness are re-checked. The in-use check runs last, right before any removal."""
    if prechecked is None:
        facts = worktree_local_checks(path, rebuildable)
    else:
        facts = dict(prechecked)
        head = _git(path, "rev-parse", "HEAD").stdout.strip()
        if head != facts["head"]:
            raise Refusal("changed", f"{path}: HEAD moved since it was inspected")
        if is_dirty(path):
            raise Refusal("dirty", f"{path} has uncommitted or untracked changes")
    if unpushed(path):
        merged = False
        if merged_ok:
            if pr_head is None:
                pr = gh_pr_state(path, facts["branch"], facts["head"], _origin_owner(path))
                pr_head = pr["head"] if pr and pr["state"] in ("MERGED", "CLOSED") else ""
            merged = patch_merged(path) or pr_head == facts["head"]
        if not merged:
            raise Refusal("unpushed_commits",
                          f"{path} has commits that are on no remote and not merged (unpushed)")
    _not_in_use(path, snapshot)
    return facts


def remove_worktree(path: Path, *, dry_run: bool, merged_ok: bool,
                    snapshot: Snapshot | Refusal | None = None,
                    rebuildable: tuple[str, ...] | None = None,
                    pr_head: str | None = None,
                    prechecked: dict[str, Any] | None = None) -> dict[str, Any]:
    facts = check_worktree_removable(path, merged_ok=merged_ok, snapshot=snapshot,
                                     rebuildable=rebuildable, pr_head=pr_head, prechecked=prechecked)
    if not dry_run:
        # Run from the common git dir: works for normal, bare and separate-git-dir repositories
        # alike, and `worktree remove` drops its own registration (no repo-wide `worktree prune`).
        done = _git(Path(facts["common"]), "worktree", "remove", str(path), timeout=600)
        if done.returncode != 0:
            raise Refusal("git_refused", done.stderr.strip() or "git worktree remove failed")
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


_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _refuse_git(path: Path) -> None:
    """``tmp`` never deletes a repo (normal, bare, separate git dir), a worktree, something
    inside one, or a directory containing one. Detection is by content, via git and
    ``scratch_safety.inspect_tree``."""
    from .scratch_safety import git as safe_git
    from .scratch_safety import inspect_tree
    is_dir = path.is_dir() and not path.is_symlink()
    probe = safe_git(path if is_dir else path.parent, "rev-parse", "--git-dir")
    if probe.returncode == 0:
        raise Refusal("git_repository", f"{path} is or is inside a git repository; use --scope worktree")
    if probe.returncode != 128 or "not a git repository" not in probe.stderr:
        raise Refusal("git_repository", f"{path}: cannot verify it is outside a git repository")
    if not is_dir:
        return
    try:
        info = inspect_tree(path)  # also: foreign owner, read-only dirs, special files
    except OSError as exc:
        raise Refusal("unverifiable", f"{path} cannot be inspected for repositories: {exc}") from exc
    if info.repositories:
        raise Refusal("git_repository", f"{path} contains a git repository; use --scope worktree")
    if info.keep:
        raise Refusal("keep_marker", f"{path} contains a .keep marker")


def own_sessions(home: Path | None = None) -> frozenset[str]:
    """Session ids of the Claude processes this process descends from. Claude records
    ``<pid> -> sessionId`` in ``~/.claude/sessions/<pid>.json``; the pid must be one of OUR
    ancestors, so an environment variable (inherited, or typed by hand) never makes a session
    "own"."""
    directory = (home or Path.home()) / ".claude" / "sessions"
    found: set[str] = set()
    for pid in _ancestors():
        try:
            data = json.loads((directory / f"{pid}.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("pid") == pid and isinstance(data.get("sessionId"), str):
            found.add(data["sessionId"].lower())
    return frozenset(found)


def _guard_scratch(path: Path, scratch: Path, live_roots: list[Path], now: float) -> None:
    """Under Claude's scratch root only strictly inside a ``<session>`` dir, never the root, a
    ``<project>`` dir or the session dir itself. ``.keep`` protects. The caller's own session
    (see ``own_sessions``) is free to clean up after itself; any other session must pass
    ``scratch_reaper``'s policy: not live and idle for the session TTL."""
    from .scratch_reaper import Policy, _live_ids
    from .scratch_safety import inspect_tree, keep_ancestor
    policy = Policy()
    own = own_sessions()
    for base in _bases([scratch]):
        parts = relative_parts(path, base)
        if parts is None:
            continue
        index = 0 if parts and _UUID.fullmatch(parts[0].lower()) else 1 if len(parts) > 1 and _UUID.fullmatch(parts[1].lower()) else None
        if index is None or len(parts) <= index + 1:
            raise Refusal("scratch_structure",
                          f"{path} is the scratch root, a project or a session directory; only paths inside a session are removable")
        if keep_ancestor(path, path.parents[len(parts) - 1]):
            raise Refusal("keep_marker", f"{path} is under a .keep marker")
        session = parts[index].lower()
        if session not in own:
            if session in _live_ids(live_roots, now - policy.live_seconds):
                raise Refusal("live_session", f"session {session} is still live")
            session_dir = path.parents[len(parts) - index - 2]
            if inspect_tree(session_dir, accounting_only=True).newest > now - policy.session_ttl_seconds:
                raise Refusal("session_recent",
                              f"session {session} was active within {policy.session_ttl_seconds / 3600:.0f} h")
        return


def _remove_tmp(path: Path, *, dry_run: bool, scratch: Path, live_roots: list[Path],
                now: float) -> dict[str, Any]:
    _guard_scratch(path, scratch, live_roots, now)
    _refuse_git(path)
    _not_in_use(path)
    if not dry_run:
        _rmtree(path)
    return {}


def _remove_docker(name: str, *, dry_run: bool) -> dict[str, Any]:
    from .docker_hygiene import remove_ephemeral_container
    return remove_ephemeral_container(name, dry_run=dry_run)


def cleanup(scope: Scope | str, path: Path | str | None, *, roots: list[Path], dry_run: bool = False,
            merged_ok: bool = False, state_dir: Path | None = None,
            temp_roots: list[Path] | None = None,
            temp_worktree_parents: list[Path] | None = None,
            scratch_root: Path | None = None,
            live_roots: list[Path] | None = None, now: float | None = None) -> dict[str, Any]:
    """Remove one thing; returns a report dict or raises ``Refusal`` (never a raw timeout/OSError)."""
    try:
        return _cleanup(scope, path, roots=roots, dry_run=dry_run, merged_ok=merged_ok,
                        state_dir=state_dir, temp_roots=temp_roots,
                        temp_worktree_parents=temp_worktree_parents, scratch=scratch_root,
                        live_roots=live_roots, now=time.time() if now is None else now)
    except subprocess.TimeoutExpired as exc:
        raise Refusal("timeout", f"{' '.join(map(str, exc.cmd))[:120]} timed out; nothing more was done") from exc
    except OSError as exc:
        raise Refusal("error", f"{exc}") from exc


def _cleanup(scope: Scope | str, path: Path | str | None, *, roots: list[Path], dry_run: bool,
             merged_ok: bool, state_dir: Path | None, temp_roots: list[Path] | None,
             temp_worktree_parents: list[Path] | None, scratch: Path | None,
             live_roots: list[Path] | None, now: float) -> dict[str, Any]:
    try:
        kind = Scope(scope)
    except ValueError:
        raise Refusal("bad_scope", f"scope must be one of {', '.join(SCOPES)}") from None
    if kind is Scope.DOCKER:
        if not path:
            raise Refusal("missing_path", "docker scope needs --path <container name or id>")
        extra = _remove_docker(str(path), dry_run=dry_run)
        return {"scope": scope, "path": str(path), "dry_run": dry_run,
                "action": "would_remove" if dry_run else "removed", **extra}
    if not path:
        raise Refusal("missing_path", f"{scope} scope needs --path")
    temp = default_temp_roots() if temp_roots is None else temp_roots
    parents = default_temp_worktree_parents() if temp_worktree_parents is None else temp_worktree_parents
    target = checked_path(path, temp if kind is Scope.TMP else roots,
                          parents if kind is Scope.WORKTREE else None)
    if kind is Scope.WORKTREE:
        extra = remove_worktree(target, dry_run=dry_run, merged_ok=merged_ok)
    elif kind is Scope.TARGET:
        extra = _remove_target(target, dry_run=dry_run, state_dir=state_dir)
    else:
        from .scratch_reaper import default_live_roots
        extra = _remove_tmp(target, dry_run=dry_run,
                            scratch=scratch_root() if scratch is None else scratch,
                            live_roots=default_live_roots() if live_roots is None else live_roots,
                            now=now)
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
