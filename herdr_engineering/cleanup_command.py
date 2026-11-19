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
  is patch-merged or its PR is MERGED; a closed PR does not count);
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
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .scratch_safety import Snapshot

_START_SLACK_SECONDS = 120  # Claude writes startedAt a moment after the process starts
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
    """Claude's scratch root, resolved exactly as ``scratch_reaper`` does: ``claude-<uid>`` under a
    validated ``$CLAUDE_CODE_TMPDIR``, else ``/tmp/claude-<uid>``."""
    from .scratch_reaper import default_root
    return default_root()


def guard_roots(scratch: Path) -> list[Path]:
    """Every location a live session may sit in: the resolved root, ``claude-<uid>`` under every fixed
    temp root, and the raw ``$CLAUDE_CODE_TMPDIR`` one even when it was rejected as a deletion root."""
    from .scratch_reaper import system_temp_roots
    roots = [scratch, *(temp / f"claude-{os.getuid()}" for temp in system_temp_roots())]
    if base := os.environ.get("CLAUDE_CODE_TMPDIR"):
        roots.append(Path(base) / f"claude-{os.getuid()}")
    return list(dict.fromkeys(roots))


def default_roots(home: Path | None = None, scratch: Path | None = None) -> list[Path]:
    """Agent-created locations only. ``~/IdeaProjects`` and ``~/work`` hold the user's own repos.
    ``scratch``: the one resolved scratch root (None resolves it here)."""
    home = home or Path.home()
    return [home / ".local" / "share" / "worktrees", home / ".cache" / "herdr-engineering",
            scratch or scratch_root()]


def default_temp_roots(scratch: Path | None = None) -> list[Path]:
    """Where scratch files may live; ``tmp`` scope is confined to these."""
    from .scratch_reaper import system_temp_roots
    return [*system_temp_roots(), scratch or scratch_root()]


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


def absolute_roots(value: Any, name: str = "roots") -> tuple[str, ...]:
    """A config list of absolute directories (``~`` allowed). An empty or relative entry would
    resolve against whatever the current directory happens to be; a scalar would split into
    characters. Both are invalid."""
    roots = config_strings(value, name)
    if bad := [r for r in roots if not r.strip() or not Path(r).expanduser().is_absolute()]:
        raise ValueError(f"{name} must be absolute paths, got {bad[0]!r}")
    return roots


def configured_roots(extra: Iterable[str] = (), home: Path | None = None,
                     scratch: Path | None = None) -> list[Path]:
    """Defaults + already validated ``extra`` roots (the caller reads and validates the config)."""
    return [*default_roots(home, scratch), *(Path(r).expanduser() for r in extra)]


def user_roots(home: Path | None = None) -> tuple[str, ...]:
    """``disk_hygiene.roots`` of the user config, validated; invalid fails CLOSED."""
    try:
        return absolute_roots(user_disk_hygiene_config(home).get("roots"))
    except ValueError as exc:
        raise Refusal("config_unreadable", f"disk_hygiene.{exc}") from exc


def _git(cwd: Path, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Every git call goes through the hardened ``scratch_safety.git`` runner: ``GIT_*`` stripped,
    no fsmonitor or hooks, ``LC_ALL=C``. Repository-controlled config (a cloned repo is untrusted)
    cannot change what a check sees; the user's own ``HOME``/``XDG_CONFIG_HOME`` and global config
    apply, so ignored and untracked files are classified exactly as the user's ``git status`` does."""
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


def _trusted_bases(paths: list[Path]) -> list[Path]:
    """``_bases`` for DELETION roots: a ``claude-<uid>`` that is a symlink or foreign is dropped. The
    scratch guard keeps using ``_bases``: a planted root can only refuse more, never allow more."""
    return _bases([p for p in paths if not _untrusted_scratch(p)])


def _untrusted_scratch(path: Path) -> bool:
    """A ``claude-<uid>`` directory that is a symlink or not ours (another local user can plant one)."""
    if path.name.casefold() != f"claude-{os.getuid()}":
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        logging.getLogger(__name__).warning("ignoring %s: a symlink or not owned by this user", path)
        return True
    return False


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
        for base in _trusted_bases(roots):
            parts = relative_parts(candidate, base)
            if parts == ():
                raise Refusal("is_root", f"{candidate} is a configured root, not removable")
            if parts:
                return _verified(candidate, len(parts))
        for parent in _trusted_bases(temp_worktree_parents or []):
            if candidate.name.startswith(TEMP_WORKTREE_PREFIX) and relative_parts(candidate, parent) == (candidate.name,):
                return _verified(candidate, 1)
    raise Refusal("outside_roots", f"{target} is outside the agent-owned roots")


def _ancestors() -> frozenset[int]:
    """This process's ancestors, from one ``ps`` listing. The shell that ran ``cd / && herdr-eng
    cleanup --path X`` names X in its own command line; that is the caller, not a user of X."""
    listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True,
                             timeout=15, check=False).stdout
    parents = {int(pid): int(ppid) for pid, ppid in (line.split() for line in listing.splitlines()
                                                    if len(line.split()) == 2) if pid.isdigit() and ppid.isdigit()}
    found: set[int] = set()
    pid = os.getppid()
    while pid > 1 and pid not in found and len(found) < 64:
        found.add(pid)
        pid = parents.get(pid, 0)
    return frozenset(found)


def _argument_lines(ignore: frozenset[int]) -> tuple[str, ...]:
    """Command lines of other processes (they name paths too), best effort."""
    from .scratch_safety import _comparison, process_listing
    try:
        return tuple(_comparison(command) for pid, command in process_listing() if pid not in ignore)
    except (OSError, subprocess.SubprocessError):
        return ()


def in_use_snapshot(proc: Path = Path("/proc")) -> Snapshot:
    """The ``scratch_safety`` process snapshot (open files, cwds, argv). Where ``lsof`` is
    unavailable (Linux without it) it falls back to the open files and working directories in
    ``proc`` (``/proc``) plus the ``ps`` command lines; with neither source it fails CLOSED:
    ``Refusal('in_use')``."""
    from .scratch_safety import InspectionUnavailable, Snapshot, _comparison, process_snapshot
    ancestors: frozenset[int] = frozenset()
    try:
        ancestors = _ancestors()
        return process_snapshot(ancestors)
    except (InspectionUnavailable, OSError, ValueError, subprocess.SubprocessError) as exc:
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


def pr_excuses_unpushed(pr: dict[str, str] | None, head: str) -> bool:
    """Only a MERGED PR whose head is still this HEAD excuses commits that are on no remote.
    None (gh unknown), NONE, OPEN and CLOSED (maybe closed to be reworked) never do."""
    return pr is not None and pr["state"] == "MERGED" and bool(head) and pr["head"] == head


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


# OS-generated files nobody wrote: disposable whether untracked or ignored, and never "dirty".
OS_JUNK = (".DS_Store", "._*", "Thumbs.db", "desktop.ini", "Icon\r")


def _is_junk(root: Path, entry: str) -> bool:
    """A junk-named regular file. Never a directory (``._work/`` may be a nested repository,
    and git lists an untracked one as ``dir/``) and never a symlink."""
    from fnmatch import fnmatchcase
    if entry.endswith("/") or not any(fnmatchcase(entry.rsplit("/", 1)[-1], pattern) for pattern in OS_JUNK):
        return False
    try:
        return stat.S_ISREG(os.lstat(root / entry).st_mode)
    except OSError:
        return False


def _status(path: Path, *flags: str) -> list[tuple[str, str]] | None:
    """``git status --porcelain -z`` as (code, path) pairs: names are never quoted. None: failed."""
    out = _git(path, "status", "--porcelain", "-z", *flags)
    if out.returncode != 0:
        return None
    fields = out.stdout.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        code, name = fields[index][:2], fields[index][3:]
        entries.append((code, name))
        index += 2 if code[0] in "RC" else 1  # a rename/copy carries its source as the next field
    return entries


def untracked_junk(path: Path) -> list[str]:
    """Untracked OS-junk files (relative paths); ``git worktree remove`` refuses a tree holding them."""
    return [name for code, name in _status(path, "--untracked-files=all") or []
            if code == "??" and _is_junk(path, name)]


def is_dirty(path: Path) -> bool:
    entries = _status(path, "--untracked-files=all")
    return entries is None or any(not (code == "??" and _is_junk(path, name)) for code, name in entries)


def foreign_ignored(path: Path, rebuildable: tuple[str, ...] = REBUILDABLE, *, deep: bool = True) -> list[str]:
    """Ignored-but-present entries that are not plain rebuildable output (or OS junk).

    ``--ignored=matching`` lists each file by the pattern that ignored it and collapses a
    directory to one ``dir/`` entry only when the directory itself is ignored. A directory
    entry's name is matched against the (glob) rebuildable list; a file entry's name must match on
    its own, so ``dist/prod.env`` is never "rebuildable" because of ``dist``. A matching directory
    is still looked inside: a nested repository (an editable checkout in ``.venv``, a vendored
    clone in ``build``) or an unverifiable tree makes it foreign. ``deep=False`` judges names only
    (no walk of those directories): a cheap early refusal, never a clearance."""
    from fnmatch import fnmatchcase

    from .scratch_safety import inspect_tree
    entries = _status(path, "--untracked-files=normal", "--ignored=matching")
    if entries is None:
        return ["<unknown>"]
    found = []
    for code, raw in entries:
        if code != "!!":
            continue
        entry = raw.rstrip("/")
        if _is_junk(path, raw):
            continue
        if not any(fnmatchcase(entry.rsplit("/", 1)[-1], pattern) for pattern in rebuildable):
            found.append(entry)
            continue
        full = path / entry
        if not deep or full.is_symlink() or not full.is_dir():
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


def user_rebuildable() -> tuple[str, ...]:
    """The user's extra rebuildable patterns from the config; a bad value refuses (fail closed)."""
    try:
        return config_strings(user_disk_hygiene_config().get("rebuildable"), "rebuildable")
    except ValueError as exc:
        raise Refusal("config_unreadable", f"disk_hygiene.{exc}") from exc


def _refuse_unremovable_state(path: Path, rebuildable: tuple[str, ...] | None, *, deep: bool) -> None:
    """Local-only refusals: dirty, hidden index state, ignored files that are not rebuildable.
    ``deep=False`` skips the walk of rebuildable directories (an early, cheap refusal only)."""
    from .scratch_safety import hidden_index_reason
    if is_dirty(path):
        raise Refusal("dirty", f"{path} has uncommitted or untracked changes")
    if reason := hidden_index_reason(path):
        raise Refusal("hidden_index_state",
                      f"{path} has skip-worktree/assume-unchanged entries whose edits git hides ({reason})")
    patterns = (*REBUILDABLE, *(user_rebuildable() if rebuildable is None else rebuildable))
    if ignored := foreign_ignored(path, patterns, deep=deep):
        raise Refusal("ignored_files",
                      f"{path} has ignored files that are not plain rebuildable output: {', '.join(ignored[:5])}")


def worktree_quick_facts(path: Path, rebuildable: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Facts plus the cheap local refusals (dirty, hidden index, foreign ignored names), taken
    before any network call so a worktree that cannot be removed never costs a PR lookup. The
    walk of rebuildable directories is left to ``settle_worktree``."""
    facts = worktree_facts(path)
    _refuse_unremovable_state(path, rebuildable, deep=False)
    return facts


def settle_worktree(path: Path, facts: dict[str, Any], rebuildable: tuple[str, ...] | None = None) -> None:
    """The full refusals that must hold at the moment of removal, after any network lookup: HEAD is
    still the inspected one and the local state, including the nested-repository walk, is removable.
    ``rebuildable``: the user's extra patterns (None reads the config here)."""
    if _git(path, "rev-parse", "HEAD").stdout.strip() != facts["head"]:
        raise Refusal("changed", f"{path}: HEAD moved since it was inspected")
    _refuse_unremovable_state(path, rebuildable, deep=True)


def check_worktree_removable(path: Path, *, merged_ok: bool,
                             snapshot: Snapshot | Refusal | None = None,
                             rebuildable: tuple[str, ...] | None = None,
                             pr_head: str | None = None,
                             prechecked: dict[str, Any] | None = None) -> dict[str, Any]:
    """``prechecked``: facts from ``worktree_quick_facts`` (taken before the caller's own PR
    lookup). ``pr_head``: the head SHA of a MERGED PR the caller already looked up (None looks it
    up here when ``merged_ok``); it excuses unpushed commits only while it equals the HEAD. The
    cheap local checks run before any lookup, the full ``settle_worktree`` once after it; the
    in-use check runs last, right before any removal."""
    if rebuildable is None:
        rebuildable = user_rebuildable()  # read the config once
    facts = dict(prechecked) if prechecked is not None else worktree_quick_facts(path, rebuildable)
    if unpushed(path):
        merged = False
        if merged_ok:
            if pr_head is None:
                pr = gh_pr_state(path, facts["branch"], facts["head"], _origin_owner(path))
                excused = pr_excuses_unpushed(pr, facts["head"])
            else:
                excused = bool(facts["head"]) and pr_head == facts["head"]
            merged = patch_merged(path) or excused
        if not merged:
            raise Refusal("unpushed_commits",
                          f"{path} has commits that are on no remote and not merged (unpushed)")
    settle_worktree(path, facts, rebuildable)
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
        for name in untracked_junk(path):  # `worktree remove` refuses untracked files; these are junk files
            (path / name).unlink(missing_ok=True)
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


def _process_started(pid: int) -> float | None:
    """Start time (epoch seconds) of a live process, None if it cannot be read."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=15, check=False, env={**os.environ, "LC_ALL": "C"}).stdout.strip()
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return None


def _process_environment(pid: int) -> dict[str, str] | None:
    """The environment of a same-uid process: ``/proc/<pid>/environ`` on Linux, ``ps -E`` elsewhere.
    None when it cannot be read (macOS shows nothing for protected system binaries)."""
    try:
        raw = Path(f"/proc/{pid}/environ")
        if raw.parent.is_dir():
            pairs = raw.read_bytes().decode(errors="replace").split("\0")
            return dict(item.split("=", 1) for item in pairs if "=" in item)
        out = subprocess.run(["ps", "-E", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=15, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    tokens = dict(re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(\S*)", out))
    return tokens if "PATH" in tokens or "HOME" in tokens else None


def claude_config_dirs() -> list[Path]:
    """Every config dir a Claude account of this uid uses: the caller's, ``~/.claude`` and the
    ``CLAUDE_CONFIG_DIR`` of each running ``claude`` process (all accounts share ``/tmp/claude-<uid>``).
    A running ``claude`` whose environment cannot be read makes liveness unknowable: refuse."""
    from .scratch_reaper import claude_config_dirs as callers
    dirs = callers()
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured and not os.access(configured, os.R_OK | os.X_OK):
        raise Refusal("scratch_protected", f"CLAUDE_CONFIG_DIR={configured} cannot be read; liveness is unknown")
    listing = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,comm="], capture_output=True, text=True,
                             timeout=15, check=False).stdout
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit() or Path(command.strip()).name != "claude":
            continue
        env = _process_environment(int(pid))
        if env is None:
            raise Refusal("live_session_unknown", f"cannot read the environment of the running claude process {pid}")
        if configured := env.get("CLAUDE_CONFIG_DIR"):
            dirs.append(Path(configured))
    return list(dict.fromkeys(dirs))


def running_sessions(dirs: list[Path], *, own_only: bool = False) -> frozenset[str]:
    """Session ids of running Claude processes. Claude records ``<pid> -> sessionId, startedAt`` in
    ``<config dir>/sessions/<pid>.json``; the process now running under that pid must have started when
    the file says (a crashed session's stale file under a recycled pid counts for nothing).
    ``own_only`` keeps the processes this one descends from: the caller's own sessions. An environment
    variable never makes a session "own"."""
    ancestors = _ancestors() if own_only else None
    found: set[str] = set()
    for config in dirs:
        try:
            files = list((config / "sessions").glob("*.json"))
        except OSError:
            continue
        for file in files:
            try:
                data = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            pid = data.get("pid") if isinstance(data, dict) else None
            if not (isinstance(pid, int) and file.stem == str(pid) and isinstance(data.get("sessionId"), str)
                    and isinstance(data.get("startedAt"), int | float)):
                continue
            if ancestors is not None and pid not in ancestors:
                continue
            started = _process_started(pid)
            if started is not None and abs(started - data["startedAt"] / 1000) <= _START_SLACK_SECONDS:
                found.add(data["sessionId"].lower())
    return frozenset(found)


def _guard_scratch(path: Path, scratches: list[Path], live_roots: list[Path], now: float) -> None:
    """One rule for ``--scope tmp``. A path TOUCHES scratch when it or an ancestor is a ``claude-<uid>``
    directory (a symlinked or foreign one included: it only stops being a deletion root), or when
    anything below it is (unbounded walk; any walk error refuses). A touching path is removable only
    when it is one session ``<claude-uid>/[<project>/]<uuid>`` or inside one, with no ``claude-<uid>``
    nested below it, the session not the caller's own, not live and idle for the session TTL. The
    INNERMOST ``claude-<uid>`` decides. Everything else that touches scratch is ``scratch_protected``."""
    from .scratch_reaper import Policy, _live_ids
    from .scratch_safety import inspect_tree, keep_ancestor
    wanted = f"claude-{os.getuid()}".casefold()
    roots = [parts for base in _bases([*scratches, *(p for p in (path, *path.parents) if p.name.casefold() == wanted)])
             if (parts := relative_parts(path, base)) is not None]
    nested = False

    def walk_failed(error: OSError) -> None:
        raise Refusal("scratch_protected", f"{path} cannot be searched for scratch roots: {error}")

    for _dir, names, files in os.walk(path, onerror=walk_failed) if path.is_dir() else ():
        if any(name.casefold() == wanted for name in (*names, *files)):
            nested = True
            break
    if not roots and not nested:
        return
    if nested or not roots:
        raise Refusal("scratch_protected", f"{path} contains a scratch root or session; remove one session at a time")
    def session_index(parts: tuple[str, ...]) -> int | None:
        return next((i for i in (0, 1) if len(parts) > i and _UUID.fullmatch(parts[i].lower())), None)

    parts = min(roots, key=len)
    index = session_index(parts)
    if index is None:
        raise Refusal("scratch_protected", f"{path} is a scratch root or a project; only one session is removable")
    session = parts[index].lower()
    below = len(parts) - index - 1  # levels between the session dir and the target
    session_dir = path.parents[below - 1] if below else path
    # every session between the target and the outermost claude-<uid>: the target's own and each enclosing one
    sessions = {p[i].lower() for p in roots if (i := session_index(p)) is not None}
    dirs = claude_config_dirs()
    if sessions & running_sessions(dirs, own_only=True):
        raise Refusal("scratch_protected", "the caller's own session is, or encloses, the target")
    outermost = max(len(p) for p in roots)  # the outermost root has the most parts below it
    if keep_ancestor(path, path.parents[outermost - 1]):
        raise Refusal("keep_marker", f"{path} is under a .keep marker")
    policy = Policy()
    live = _live_ids([*live_roots, *(d / "projects" for d in dirs)], now - policy.live_seconds) | running_sessions(dirs)
    if sessions & live:
        raise Refusal("live_session", f"session {', '.join(sorted(sessions & live))} is still live")
    if inspect_tree(session_dir, accounting_only=True).newest > now - policy.session_ttl_seconds:
        raise Refusal("session_recent", f"session {session} was active within {policy.session_ttl_seconds / 3600:.0f} h")


def _remove_tmp(path: Path, *, dry_run: bool, scratch: list[Path], live_roots: list[Path],
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
    scratch = scratch_root() if scratch is None else scratch
    temp = default_temp_roots(scratch) if temp_roots is None else temp_roots
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
                            scratch=guard_roots(scratch),
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
                   help="worktree: allow commits on no remote when they are patch-merged or the PR is merged")
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
        scratch = scratch_root()  # resolved once: the roots, the temp roots and the guard agree
        out = cleanup(args.scope, args.path, roots=configured_roots(user_roots(), scratch=scratch),
                      dry_run=args.dry_run, merged_ok=args.merged_ok, state_dir=_default_state_dir(),
                      scratch_root=scratch)
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
