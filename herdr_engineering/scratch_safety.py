"""Fail-closed filesystem, Git and live-process guards for scratch retention."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_COMPILERS = {
    "cargo",
    "rustc",
    "swift",
    "swift-build",
    "swift-frontend",
    "xcodebuild",
    "clang",
    "clang++",
    "ninja",
    "make",
    "cmake",
    "xcrun",
    "actool",
    "ibtool",
}
_MAX_ENTRIES = 1_000_000


class InspectionUnavailable(OSError):
    """A global process/lease guard is unavailable; no candidate may be applied."""


@dataclass(frozen=True)
class TreeInfo:
    newest: float
    allocated_bytes: int
    device: int
    inode: int
    keep: bool
    repositories: tuple[Path, ...]
    session_ids: frozenset[str]
    reserved_bytes: int


def inspect_tree(
    path: Path, *, ignore_root_ctime: bool = False, accounting_only: bool = False
) -> TreeInfo:
    first = path.lstat()
    if not stat.S_ISDIR(first.st_mode):
        raise OSError("candidate is not a real directory")
    newest, allocated, keep = 0.0, 0, False
    reserved = 0
    repositories: list[Path] = []
    session_ids: set[str] = set()
    pending = [path]
    seen: set[tuple[int, int]] = set()
    count = 0
    deadline = time.monotonic() + 45
    while pending:
        item = pending.pop()
        metadata = item.lstat()
        count += 1
        if count > _MAX_ENTRIES or (count % 1024 == 0 and time.monotonic() > deadline):
            raise OSError("candidate inspection limit reached")
        if metadata.st_dev != first.st_dev or (
            not accounting_only and metadata.st_uid != os.getuid()
        ):
            raise OSError("foreign owner or filesystem in candidate")
        newest = max(
            newest,
            metadata.st_mtime,
            0 if ignore_root_ctime and item == path else metadata.st_ctime,
        )
        identity = (metadata.st_dev, metadata.st_ino)
        if identity not in seen:
            allocated += metadata.st_blocks * 512
            reserved += max(
                metadata.st_blocks * 512, metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0
            )
            seen.add(identity)
        keep |= item.name == ".keep"
        session_ids.update(re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", item.name))
        if stat.S_ISLNK(metadata.st_mode):
            continue  # unlinking a link must never inspect/remove its target
        if stat.S_ISDIR(metadata.st_mode):
            if not accounting_only and metadata.st_mode & (stat.S_IWUSR | stat.S_IXUSR) != (
                stat.S_IWUSR | stat.S_IXUSR
            ):
                raise OSError("read-only directory in candidate")
            with os.scandir(item) as entries:
                children = [Path(entry.path) for entry in entries]
            names = {child.name for child in children}
            if ".git" in names:
                repositories.append(item)
            elif {"HEAD", "objects"} <= names and ".git" not in item.relative_to(path).parts:
                repositories.append(item)  # bare/unknown dependency repository
            pending.extend(children)
        elif not accounting_only and not stat.S_ISREG(metadata.st_mode):
            raise OSError("special file in candidate")
    return TreeInfo(
        newest,
        allocated,
        first.st_dev,
        first.st_ino,
        keep,
        tuple(repositories),
        frozenset(session_ids),
        reserved,
    )


def git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_NO_LAZY_FETCH="1",
        LC_ALL="C",
    )
    return subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "submodule.recurse=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "log.showSignature=false",
            "-c",
            "core.trustctime=true",
            "-c",
            "core.checkStat=default",
            "-c",
            "core.ignoreStat=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(repo),
            *arguments,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


def repository_reason(repo: Path) -> str | None:
    filters = git(
        repo,
        "config",
        "--local",
        "--includes",
        "--get-regexp",
        r"^(filter\.|extensions\.partialclone|remote\..*\.promisor)",
    )
    if filters.returncode == 0 and filters.stdout.strip():
        return "repository_filter"
    if filters.returncode not in {0, 1}:
        return "repository_unverifiable"
    top = git(repo, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.strip()).resolve() != repo.resolve():
        return "repository_unverifiable"
    index = git(repo, "ls-files", "-v", "-z")
    if index.returncode:
        return "repository_unverifiable"
    if any(
        entry[:1].islower() or entry.startswith("S ") for entry in index.stdout.split("\0") if entry
    ):
        return "repository_hidden_index_state"
    checks = [
        ("dirty_repository", ("status", "--porcelain", "--untracked-files=all", "--ignored")),
        (
            "unpublished_commits",
            ("log", "--branches", "HEAD", "--not", "--remotes", "-1", "--format=%H"),
        ),
        ("repository_stash", ("stash", "list")),
    ]
    remote = git(repo, "remote")
    if remote.returncode or not remote.stdout.strip():
        return "repository_without_remote"
    for reason, args in checks:
        result = git(repo, *args)
        if result.returncode:
            return "repository_unverifiable"
        if result.stdout.strip():
            return reason
    return None


def tracked_reason(path: Path) -> str | None:
    result = git(path.parent, "rev-parse", "--show-toplevel")
    if result.returncode:
        if result.returncode == 128 and "not a git repository" in result.stderr:
            return None
        return "enclosing_repository_unverifiable"
    top = Path(result.stdout.strip()).resolve()
    if not path.is_relative_to(top):
        return "enclosing_repository_unverifiable"
    tracked = git(top, "ls-files", "--", str(path.relative_to(top)))
    if tracked.returncode or tracked.stdout.strip():
        return "tracked_or_unverifiable_output"
    return None


def keep_ancestor(path: Path, root: Path) -> bool:
    current = path
    while current.is_relative_to(root):
        if os.path.lexists(current / ".keep"):
            return True
        if current == root:
            break
        current = current.parent
    return False


@dataclass(frozen=True)
class Snapshot:
    paths: tuple[str, ...]
    arguments: tuple[str, ...]
    compiler_cwds: tuple[str, ...]
    references: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def uses(self, anchor: Path, source_root: Path | None = None) -> bool:
        # lsof paths and anchors are absolute. Use a separator boundary rather
        # than allocating Path.relative_to exceptions for every unrelated FD.
        name = _comparison(str(anchor))
        prefix = name.rstrip("/") + "/"
        if any(path == name or path.startswith(prefix) for path in self.paths):
            return True
        pattern = re.compile(re.escape(name) + r"(?=$|[\s/\"'])")
        if any(pattern.search(command) for command in self.arguments):
            return True
        for cwd, commands in self.references:
            relative = os.path.relpath(name, cwd)
            matching = tuple(command for command in commands if relative in command)
            if not matching:
                continue
            relative_pattern = re.compile(
                r"(?:^|[\s\"'=])(?:\./)?" + re.escape(relative) + r"(?=$|[\s/\"'])"
            )
            if any(relative_pattern.search(command) for command in matching):
                return True
        if source_root is not None:
            source = _comparison(str(source_root))
            return any(
                cwd == source or cwd.startswith(source.rstrip("/") + "/")
                for cwd in self.compiler_cwds
            )
        return False


def _comparison(value: str) -> str:
    # macOS paths can use equivalent Unicode/case spellings. Overprotection on
    # a case-sensitive APFS volume is safer than missing a live reference.
    if sys.platform != "darwin":
        return value
    value = re.sub(
        r"(^|[\s\"'=(:]|file://)/(tmp|var|etc)(?=/|$|[\s\"'])",
        lambda match: match.group(1) + "/private/" + match.group(2),
        value,
    )
    return unicodedata.normalize("NFC", value).casefold()


def process_snapshot() -> Snapshot:
    try:
        return _process_snapshot()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise InspectionUnavailable("process visibility is unavailable; cleanup blocked") from error


def _process_snapshot() -> Snapshot:
    listing = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,args="], capture_output=True, text=True, timeout=15
    )
    if listing.returncode or len(listing.stdout) > 32 * 2**20:
        raise OSError("process argument inspection failed")
    arguments: list[str] = []
    commands: dict[int, str] = {}
    compilers: set[int] = set()
    own = os.getpid()
    for line in listing.stdout.splitlines():
        pieces = line.strip().split(None, 1)
        if len(pieces) != 2 or not pieces[0].isdigit() or int(pieces[0]) == own:
            continue
        arguments.append(_comparison(pieces[1]))
        commands[int(pieces[0])] = _comparison(pieces[1])
        if Path(pieces[1].split()[0]).name in _COMPILERS:
            compilers.add(int(pieces[0]))
    result = subprocess.run(
        ["lsof", "-nP", "-w", "-a", "-u", str(os.getuid()), "-F0pcnft"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 64 * 2**20:
        raise OSError("open-file inspection failed")
    paths: list[str] = []
    compiler_cwds: list[str] = []
    references: dict[str, set[str]] = {}
    pid, descriptor = 0, ""
    for raw in result.stdout.split(b"\0"):
        field = raw.lstrip(b"\n")
        if not field:
            continue
        key, value = field[:1], os.fsdecode(field[1:])
        if key == b"p":
            pid, descriptor = int(value), ""
        elif key == b"f":
            descriptor = value
        elif key == b"n" and value.startswith("/") and pid != own:
            path = _comparison(value.removesuffix(" (deleted)"))
            paths.append(path)
            if descriptor == "cwd" and pid in compilers:
                compiler_cwds.append(path)
            if descriptor == "cwd" and pid in commands:
                references.setdefault(path, set()).add(commands[pid])
    return Snapshot(
        tuple(paths),
        tuple(arguments),
        tuple(compiler_cwds),
        tuple((cwd, tuple(values)) for cwd, values in references.items()),
    )


def open_parent(root: Path, path: Path) -> int:
    """Anchor every ancestor lookup with directory FDs and O_NOFOLLOW."""
    relative = path.relative_to(root)
    if not relative.parts or any(part in {".", "..", ""} for part in relative.parts):
        raise ValueError("cannot retire a managed root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
