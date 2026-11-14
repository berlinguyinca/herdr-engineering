"""`fleet.lock.yaml` semantics (spec section 7): the one writer of the lock, and its read-only verifier.

The lock's `repos:` section means "the fabric is converged on these SHAs", so it moves only after a rollout fully
succeeded (the runner calls `pin_repos`), and every SHA that is written or verified here must be published history
(decision D3): a commit of the remote's `main`, found in the watcher's own bare cache clone. One bad SHA refuses the
whole write. The edit is textual and confined to `repos:` values, so every other byte and comment survives; the file is
replaced atomically (temp + fsync + rename, mode kept) and the previous file is kept as `<lock>.bak-<UTC date>` (a
later one the same day: `-HHMMSSffffff`, with `-N` appended on a further collision), beside the file a symlinked lock
points to. Line endings (LF or CRLF) are preserved.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..control_plane.fleet_rollout import WATCHED_REPOS
from . import core, state, verify

#: Per-pin verdicts of `fleet lock verify` (closed vocabulary).
PIN_STATUSES = ("ok", "malformed_sha", "fetch_failed", "unknown_commit", "not_on_origin_main",
                "converged_mismatch", "unwatched")
_SHA = re.compile(r"[0-9a-f]{40}")
_REPOS_LINE = re.compile(r"^repos:[ \t]*(#.*)?$")
_ENTRY = re.compile(r"""^(?P<indent>[ \t]+)(?P<key>[^\s#:'"]+)(?P<mid>:[ \t]*['"]?)(?P<val>[^\s#'"]*)(?P<rest>.*)$""")


class LockRefused(Exception):
    """The write was refused as a whole (nothing changed). `reason` is a `fleet_rollout.REASONS` value."""

    def __init__(self, reason: str, repos: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.repos = repos or []


def owner_repo(name: str) -> str:
    return f"berlinguyinca/{name}"


def short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def _url(base: str, repo: str) -> str:
    return f"{base.rstrip('/')}/{owner_repo(short(repo))}.git"


def read_pins(path: Path) -> dict[str, str]:
    """`repos:` as written (key -> value text); raises LockRefused(lock_invalid) when unreadable or not a mapping."""
    try:
        data = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise LockRefused("lock_invalid") from exc
    repos = data.get("repos", {}) if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        raise LockRefused("lock_invalid")
    return {str(k): str(v) for k, v in repos.items()}


def _edit(text: str, updates: Mapping[str, str]) -> str:
    """Replace or add `repos:` values without touching any other byte."""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if "\r\n" in text else "\n"                 # added lines follow the file's own convention
    start = next((i for i, ln in enumerate(lines) if _REPOS_LINE.match(ln.rstrip("\r\n"))), None)
    pending = dict(updates)
    if start is None:
        if re.search(r"^repos:", text, re.M):
            raise LockRefused("lock_invalid")                  # inline/flow form: not edited textually
        body = "".join(f"  {k}: {v}{eol}" for k, v in pending.items())
        return text + ("" if not text or text.endswith("\n") else eol) + f"repos:{eol}" + body
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace() and not ln.lstrip().startswith("#"):
            end = i
            break
    last_entry = None
    for i in range(start + 1, end):
        m = _ENTRY.match(lines[i].rstrip("\r\n"))
        if not m or lines[i].lstrip().startswith("#"):
            continue
        last_entry = i
        name = short(m["key"])
        if name in pending and (m["key"] == name or m["key"] == owner_repo(name)):
            nl = lines[i][len(lines[i].rstrip("\r\n")):]
            lines[i] = f'{m["indent"]}{m["key"]}{m["mid"]}{pending.pop(name)}{m["rest"]}{nl}'
    if pending:
        indent = _ENTRY.match(lines[last_entry].rstrip("\r\n"))["indent"] if last_entry is not None else "  "
        at = (last_entry + 1) if last_entry is not None else start + 1
        if at > 0 and not lines[at - 1].endswith("\n"):
            lines[at - 1] += eol
        lines[at:at] = [f"{indent}{k}: {v}{eol}" for k, v in pending.items()]
    return "".join(lines)


def _reserve_backup(path: Path, now: datetime) -> Path:
    """Create (empty, exclusively) the backup file for this write and return it; never reuses an existing name."""
    names = [f"{path.name}.bak-{now:%Y%m%d}", f"{path.name}.bak-{now:%Y%m%d-%H%M%S%f}"]
    names += [f"{names[1]}-{n}" for n in range(1, 1000)]
    for name in names:
        candidate = path.with_name(name)
        try:
            os.close(os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            continue
        return candidate
    raise LockRefused("lock_invalid")


def _write_atomic(path: Path, text: str, mode: int, previous: bytes | None, now: datetime) -> None:
    path = path.resolve()                      # a symlinked lock keeps its link; the target file is replaced
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if previous is not None:
            if path.read_bytes() != previous:      # changed since pin_repos read it: never overwrite someone else's edit
                raise LockRefused("lock_invalid")
            backup = _reserve_backup(path, now)
            shutil.copy2(path, backup)
        os.replace(tmp, path)
    except BaseException:                          # also SystemExit/KeyboardInterrupt: no stray `<lock>.tmp`
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def pin_repos(updates: Mapping[str, str], path: str | Path, *, directory: Path | None = None,
              base: str | None = None, allow_downgrade: bool = False, create: bool = False,
              now: datetime | None = None) -> list[dict]:
    """Move `repos:` pins to `updates` ({repo: sha}); returns `[{repo, before, after}]` for the pins that changed.

    Raises LockRefused (nothing written, no backup) when any SHA is not published `main` history
    (`not_published`), would move a pin backwards without `allow_downgrade` (`downgrade`), or the lock cannot be
    edited (`lock_invalid`: unreadable, missing without `create`, an unwatched repo).
    """
    path = Path(path).expanduser()
    directory = directory or state.state_dir()
    base = base or core.git_base()
    for repo in updates:
        if short(repo) not in WATCHED_REPOS:
            raise LockRefused("lock_invalid", [short(repo)])
    try:
        raw = path.read_bytes()                  # bytes, so CRLF survives and the changed-since-read check can match
        text = raw.decode("utf-8")
        mode = path.stat().st_mode & 0o7777
        previous: bytes | None = raw
    except FileNotFoundError:
        if not create:
            raise LockRefused("lock_invalid") from None
        text, mode, previous = "", 0o600, None
    except (OSError, UnicodeDecodeError) as exc:
        raise LockRefused("lock_invalid") from exc
    pins = read_pins(path) if previous is not None else {}
    unpublished = []
    caches = {}
    for repo, sha in updates.items():
        cache = verify.cache_path(directory, owner_repo(short(repo)))
        caches[repo] = cache
        if not verify.verify_on_main(sha, cache=cache, url=_url(base, repo)).ok:
            unpublished.append(short(repo))
    if unpublished:
        raise LockRefused("not_published", unpublished)
    changes, writes = [], {}
    for repo, sha in updates.items():
        name = short(repo)
        current = pins.get(name) or pins.get(owner_repo(name))
        if current == sha:
            continue
        if (not allow_downgrade and current and _SHA.fullmatch(current)
                and verify.is_ancestor(caches[repo], sha, current)):
            raise LockRefused("downgrade", [name])
        changes.append({"repo": name, "before": current if current and _SHA.fullmatch(current) else None,
                        "after": sha})
        writes[name] = sha
    if not writes:
        return []
    new_text = _edit(text, writes)
    try:
        written = (yaml.safe_load(new_text) or {}).get("repos") or {}
        check = {short(str(k)): str(v) for k, v in written.items()}
    except (yaml.YAMLError, AttributeError) as exc:
        raise LockRefused("lock_invalid") from exc
    if any(check.get(k) != v for k, v in writes.items()):
        raise LockRefused("lock_invalid")
    _write_atomic(path, new_text, mode, previous, now or datetime.now(UTC))
    return changes


def verify_lock(path: str | Path, *, directory: Path | None = None, base: str | None = None,
                converged: Mapping[str, str] | None = None) -> list[dict]:
    """Read-only: one `{repo, sha, status}` row per `repos:` pin; every status is in PIN_STATUSES.

    A pin must be published history of `main` (D3), and, when the watcher's state knows the converged SHA of that
    repo, equal to it (the lock means "converged"). Keys that are not watched repos are `unwatched` (not a failure).
    """
    pins = read_pins(Path(path).expanduser())
    directory = directory or state.state_dir()
    base = base or core.git_base()
    converged = converged or {}
    rows = []
    for key, sha in sorted(pins.items()):
        name = short(key)
        if name not in WATCHED_REPOS:
            rows.append({"repo": "unwatched", "sha": sha if _SHA.fullmatch(sha) else "", "status": "unwatched"})
            continue
        shown = sha if _SHA.fullmatch(sha) else ""
        verdict = verify.verify_on_main(sha, cache=verify.cache_path(directory, owner_repo(name)),
                                        url=_url(base, name))
        status = "ok" if verdict.ok else str(verdict.reason)
        want = converged.get(owner_repo(name))
        if status == "ok" and want and want != sha:
            status = "converged_mismatch"
        rows.append({"repo": name, "sha": shown, "status": status})
    return rows
