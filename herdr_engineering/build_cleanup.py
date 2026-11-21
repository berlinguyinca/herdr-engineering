"""Conservative cleanup of Cargo output directories, narrow by design.

Sweeps exclude each other with ``build-cleanup.lock`` (exclusive) and take ``build-maintenance.lock``
shared, which only excludes the destructive maintenance jobs (retention, migration, cache eviction) and
never a running build, so the disk guard can sweep from inside ``build-coordinate``.

What a sweep deletes, and nothing else:

1. An orphaned target: a Herdr-managed target whose owning worktree path no longer exists and which no
   process uses or is told to build into (the same per-target scan as for an idle target: a worktree that
   was moved, not deleted, may still have a build running). The decision is made again under the manager lock, so a worktree recreated at the
   same path is prepared afresh by the build and never loses its target. Only a not-found workspace whose
   parent lists counts; the target is renamed to ``.trash-*`` and deleted after the lock is released.
2. An idle target (no change for the idle floor, 24 h, or ``pressure_min_age_hours`` under pressure) whose
   workspace exists, and only when no process uses the target or its workspace: its cwd,
   executable, an open file or (Linux) a mapping is under either, or it is cargo-family with
   ``CARGO_TARGET_DIR`` or ``--target-dir`` naming the target. The scan is taken fresh right before each
   clean and fails closed. The delete is ``cargo clean --target-dir <dir>`` run for the workspace's
   manifest, with ``build.build-dir`` pinned to the target (a shared build-dir is never deleted), so Cargo does
its own locking. Busy checks compare (device, inode), never path spelling. On Linux the identity is read
   from the kernel (the /proc cwd, exe and fd links, the maps dev:inode columns), so a process in another mount
   namespace (a devcontainer, a rootless container) is seen. The scan runs in a child with a deadline
   (``HERDR_BUSY_CHECK_SECONDS``, default 120): on timeout the sweep stops, reports "busy check timed out"
   and releases its lock. Limit, stated plainly: without root (or CAP_SYS_PTRACE) the kernel's ptrace check makes
   another uid's cwd, exe, fd and maps unreadable (``hidepid`` or not), so only this user's processes, and those
   in user namespaces this user owns (rootless containers), are detected. Root services, ``sudo``-run binaries
   and rootful containers running as a different uid are NOT; neither on macOS, where ``lsof`` lists only this
   user's files. Such a process is skipped: ``sudo -i`` and setuid-root binaries (euid 0) by their root-owned
   ``/proc/<pid>``. This user's own non-dumpable processes (``ping``, file-capability binaries, ``prctl``) have a
   root-owned ``fd/``; they are detected ONLY through their command line, not by cwd, open files or mappings:
   an absolute path in any argument (also after ``=``, in a ``:`` list, or glued as ``-L/x``) inside the target
   or the workspace, or a program started by a relative path (``./target/release/x``) or a ``comm`` (cut at 15 characters)
   that is the name of a regular file in the target; a title with whitespace is not a name. Other relative
   arguments are not looked at.

3. An idle target over the per-target size cap (``max_target_bytes``, ``build_cleanup.max_target_gb``),
   on every sweep and whatever the disk usage (reason ``size_cap``). It goes through exactly the path of
   (2): idle floor, process scan, lease, git and ``cargo clean``. ``after_build_size_cap`` runs this pass
   for the target of one finished build.

Targets are skipped when a build lease covers them, when git tracks files in them, or when anything inside
looks foreign (links, special files, other owners). Only current-user Cargo ``target`` directories inside
explicit roots are eligible; no symlinks are followed. A target without a workspace to clean through (a
cache-only directory) is left alone.

When ``build-registry.db`` exists in the state directory (spec 05), a target whose
project directory is related (equal, inside or containing, by path prefix) to the
scope of an unexpired build lease is preserved (``active_lease``). The lease is
re-read right before deletion. A registry that exists but cannot be read makes the
whole pass fail closed (``registry_unreadable``). Leases only ever ADD protection.
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .cargo_target_storage import (
    TARGET_MARKER,
    _manager_lock,
    owner_record,
    read_owner,
    write_cachedir_tag,
)
from .disk_guard import measure

log = logging.getLogger(__name__)
_PRUNE = {".git", "node_modules", ".venv", "venv", ".cache", "__pycache__"}
_MAX_ENTRIES = 500_000
_MAX_DEPTH = 6
MIN_IDLE_HOURS = 1


def _managed_process(pid: Path, proc: Path) -> bool:
    """Verify whether a process descends from this user's local Herdr process.

    Outside Herdr's tree, inaccessible daemon state is outside the build safety
    contract. Unknown ancestry raises instead of treating a process as outside.
    A starting process that has itself exited is not managed. When an ANCESTOR exits mid-walk the
    walk is repeated once from a fresh ``PPid``, and only a positive answer is trusted: a retry that
    finds no Herdr (the child was reparented to init or a subreaper) cannot tell an unmanaged process
    from one Herdr just orphaned, so it raises like any other unverifiable ancestry.
    """
    vanished = False
    for _attempt in (0, 1):
        try:
            if _ancestry_is_herdr(pid, proc):
                return True
            if not vanished:
                return False
            raise OSError(f"Process ancestry changed while inspecting {pid}")
        except (FileNotFoundError, ProcessLookupError) as exc:
            if not pid.exists():
                return False  # the process itself exited
            if vanished:
                raise OSError(f"Process ancestry changed while inspecting {pid}") from exc
            vanished = True
    return False


def _ancestry_is_herdr(pid: Path, proc: Path) -> bool:
    seen = set()
    current = pid
    while current.name != "0":
        if current.name in seen or len(seen) >= 256:
            raise OSError(f"Unverifiable process ancestry at {pid}")
        seen.add(current.name)
        metadata = current.stat()
        argv = (current / "cmdline").read_bytes().split(b"\0")
        if (metadata.st_uid == os.getuid() and argv and argv[0]
                and Path(os.fsdecode(argv[0])).name == "herdr"
                ):
            executable = (current / "exe").resolve(strict=True)
            if executable.name == "herdr":
                return True
            raise OSError(f"Unverifiable Herdr executable at {current}")
        parents = [line.split()[1]
                   for line in (current / "status").read_text().splitlines()
                   if line.startswith("PPid:")]
        if len(parents) != 1 or not parents[0].isdigit():
            raise OSError(f"Unverifiable process parent at {current}")
        current = proc / parents[0]
    return False


def _state(pid: Path) -> str:
    """``/proc/<pid>/stat`` state letter; ``""`` when the process is gone (ENOENT/ESRCH mid-walk).
    An entry without a readable ``stat`` that still exists counts as live."""
    try:
        return (pid / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return "R" if pid.exists() else ""


def _threads(pid: Path) -> int:
    try:
        for line in (pid / "status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 2  # unknown: assume threads, so a zombie is inspected rather than skipped


def _gone_or_zombie(pid: Path) -> bool:
    """True only when the process holds nothing: it vanished, or it is a zombie (``Z``/``X``) whose
    thread group has no other thread. A leader that exited while other threads still run stays ``Z``,
    but those threads keep its cwd and descriptors (readable under ``task/<tid>``): not skippable."""
    state = _state(pid)
    if state == "":
        return True
    return state in ("Z", "X") and _threads(pid) <= 1


def _holders(pid: Path) -> list[Path]:
    """The ``/proc`` directories whose cwd/exe/fd describe this process: itself, or for a zombie
    leader the live threads under ``task/``."""
    if _state(pid) not in ("Z", "X"):
        return [pid]
    try:
        tasks = [pid / "task" / name for name in os.listdir(pid / "task") if name != pid.name]
    except FileNotFoundError:
        return []
    return [task for task in tasks if _state(task) not in ("", "Z", "X")]


def _links(holder: Path) -> list[Path]:
    links = [holder / "cwd", holder / "exe"]
    with os.scandir(holder / "fd") as entries:
        links.extend(Path(entry.path) for entry in entries)
    return links


_CARGO_NAMES = frozenset({"cargo", "rustc", "rustdoc", "rustfmt", "rust-analyzer", "clippy-driver"})
_TARGET_ENV = ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR")
# (pid, process name, how, path): "uses" is an executable, open file or mapping, "cwd" a working directory
# (which counts like "uses", but the invoker of a pass may be excused for it); "builds into" is the
# target directory a cargo-family process was told to write (its environment or ``--target-dir``).
# The fifth field is the identity the kernel gives the thing held, (st_dev, st_ino) of it and, for a cwd, of
# its ancestors: read through /proc magic links and the maps columns, so it is right in any mount namespace
# (a container's /workspaces/x is a path that does not exist here). Empty where only a path is known.
Ident = tuple[int, int]
Hold = tuple[int, str, str, Path, frozenset[Ident]]
_NO_IDS: frozenset[Ident] = frozenset()


def _cargo_family(name: str) -> bool:
    return name in _CARGO_NAMES or name.startswith("cargo-")


def _mapped(holder: Path) -> list[tuple[str, frozenset[Ident]]]:
    """Files a process has mapped (``/proc/<pid>/maps``) with the device and inode that file lists: a library
    loaded from a target stays in use after its descriptor is closed."""
    found = []
    for line in (holder / "maps").read_text(errors="replace").splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith("/"):
            ids = _NO_IDS
            try:
                major, minor = fields[3].split(":")
                if int(fields[4]) != 0:
                    ids = frozenset({(os.makedev(int(major, 16), int(minor, 16)), int(fields[4]))})
            except ValueError:
                pass
            found.append((fields[5], ids))
    return found


def _link_ids(link: Path, directory: bool = False) -> frozenset[Ident]:
    """(device, inode) of what a /proc magic link points at (and, for a cwd, of every ancestor in that
    process's own mount): the kernel resolves the link, whatever the process's mount namespace spells."""
    found: list[Ident] = []
    try:
        info = os.stat(link)
        found.append((info.st_dev, info.st_ino))
        step = str(link)
        while directory and len(found) < 64:
            step += "/.."
            info = os.stat(step)
            if (info.st_dev, info.st_ino) == found[-1]:
                break
            found.append((info.st_dev, info.st_ino))
    except OSError:
        pass
    return frozenset(found)


def _argv_target_dirs(argv: list[str]) -> list[str]:
    found = []
    for index, arg in enumerate(argv):
        if arg == "--target-dir" and index + 1 < len(argv):
            found.append(argv[index + 1])
        elif arg.startswith("--target-dir="):
            found.append(arg.split("=", 1)[1])
    return found


def _lsof_holds() -> list[Hold]:
    """Open files, cwds, executables and mappings of this user's processes where /proc is absent (macOS).

    Fails closed: if lsof cannot run, cleanup must not guess that nothing is in use.
    """
    try:
        result = subprocess.run(["lsof", "-w", "-n", "-P", "-u", str(os.getuid()), "-Fpcfn"],
                                capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"Cannot inspect running processes: {exc}") from exc
    if result.returncode > 1 or (result.returncode == 1 and not result.stdout):
        raise OSError("Cannot inspect running processes: lsof failed")
    holds: list[Hold] = []
    pid, name, descriptor = 0, "", ""
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid, name = int(line[1:]), ""
        elif line.startswith("c"):
            name = line[1:]
        elif line.startswith("f"):
            descriptor = line[1:]
        elif line.startswith("n/") and pid != os.getpid():  # like the /proc scan, ignore this process
            holds.append((pid, name, "cwd" if descriptor == "cwd" else "uses", Path(line[1:]), _NO_IDS))
    return holds


def _ps(columns: str, *flags: str) -> list[tuple[int, str]]:
    """``ps`` rows (pid, rest of the line) of this user; a ``ps`` that cannot run is an OSError."""
    try:
        listing = subprocess.run(["ps", *flags, "-U", str(os.getuid()), "-o", columns],
                                 capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"Cannot list running processes: {exc}") from exc
    if listing.returncode != 0:
        raise OSError("Cannot list running processes: ps failed")
    rows = []
    for line in listing.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != os.getpid():
            rows.append((int(parts[0]), parts[1].strip()))
    return rows


def _ps_claims() -> list[Hold]:
    """Target directories that cargo-family processes were told to build into, where /proc is absent: the
    environment and arguments ``ps -E`` shows (best effort on macOS; a value with spaces is cut short)."""
    names = {pid: Path(rest.split(None, 1)[1]).name for pid, rest in _ps("pid=,stat=,comm=")
             if len(rest.split(None, 1)) == 2 and not rest.startswith(("Z", "X"))}
    claims: list[Hold] = []
    for pid, text in _ps("pid=,command=", "-ww", "-E"):
        name = names.get(pid, "")
        if not _cargo_family(name):
            continue
        tokens = text.split()
        values = [t.split("=", 1)[1] for t in tokens if t.split("=", 1)[0] in _TARGET_ENV and "=" in t]
        claims.extend((pid, name, "builds into", Path(v), _NO_IDS) for v in values + _argv_target_dirs(tokens)
                      if v.startswith("/"))
    return claims


def _scan(proc: Path = Path("/proc"), *, paths_only: bool = False) -> list[Hold]:
    """Everything this user's other processes hold or are building into, from one look at the host.

    ``paths_only`` (the cleanup command's view) reads only the link targets: no ``stat`` of a link or its
    ancestors and no ``maps``, which can block on a hung network mount, and a hidden (non-dumpable) process
    that descends from Herdr raises, so the caller refuses as ``in_use``. The sweep keeps identities and
    never aborts on a hidden process."""
    if not proc.is_dir():
        return _lsof_holds() + _ps_claims()
    holds: list[Hold] = []
    with os.scandir(proc) as entries:
        pids = [Path(entry.path) for entry in entries if entry.name.isdigit()]
    for pid in pids:
        try:
            if int(pid.name) == os.getpid():
                continue  # every uid: what another user's process holds shows only where /proc lets us read
            if _gone_or_zombie(pid):
                continue
            number = int(pid.name)
            try:
                name = (pid / "comm").read_text().strip()
            except OSError:
                name = ""
            values: list[tuple[str, frozenset[Ident], str]] = []
            for holder in _holders(pid):
                try:
                    for link in _links(holder):
                        try:
                            values.append((os.readlink(link),
                                           _NO_IDS if paths_only else _link_ids(link, link.name == "cwd"),
                                           "cwd" if link.name == "cwd" else "uses"))
                        except (FileNotFoundError, ProcessLookupError):  # descriptor closed / process exited
                            continue
                    if not paths_only:
                        values.extend((v, ids, "uses") for v, ids in _mapped(holder))
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError as exc:
                    if holder != pid:  # a live thread of a zombie leader that cannot be read: unverifiable
                        raise OSError(f"Cannot inspect thread {holder}") from exc
                    raise
            holds.extend((number, name, how, Path(v.removesuffix(" (deleted)")), ids)
                         for v, ids, how in values if v.startswith("/"))
            if _cargo_family(name) and not paths_only:
                holds.extend(_proc_claims(pid, number, name))
        except (FileNotFoundError, ProcessLookupError):  # process exited during inspection
            continue
        except PermissionError:
            if _gone_or_zombie(pid):  # became a zombie or exited since the check above
                continue
            try:
                # An owner other than us (``sudo -i``, a setuid-root binary: euid 0) shows in ``/proc/<pid>``.
                # This user's own non-dumpable process (``ping``, file capabilities, ``prctl``) keeps that
                # owner, but its ``fd`` entry turns root-owned.
                foreign = pid.stat().st_uid != os.getuid()
                hidden = False
                if not foreign:
                    try:
                        hidden = (pid / "fd").stat().st_uid != os.getuid()
                    except PermissionError:
                        pass  # not even stat-able: unverifiable, handled as this user's own below
            except OSError:
                continue  # exited meanwhile
            if foreign:
                continue  # another user's process: nothing of it can be read
            if hidden:  # cwd, exe and fds are closed to us, the command line is not
                if paths_only and _managed_process(pid, proc):
                    raise
                holds.extend(_cmdline_holds(pid, int(pid.name)))
                continue
            if _managed_process(pid, proc):
                raise
            # Nondumpable daemons outside Herdr cannot be managed build jobs.
            continue
    return holds


def _process_paths(proc: Path = Path("/proc")) -> list[Path]:
    """Every path another process holds (cwd, executable, open file, mapping), for ``cleanup_command``'s
    /proc fallback. The same scan as the sweep, so both see one set of processes."""
    return [hold[3] for hold in _scan(proc, paths_only=True)]


def _cmdline_paths(argv: list[str]) -> set[str]:
    """Absolute paths an argument spells: the argument itself, each ``=`` separated value and everything
    after the first ``=`` (kept whole, so a path with ``:`` or ``=`` in it is caught), the entries of a ``:``
    list, and the path of a short option glued to it (``-L/abs``). Linear in the length of the argument."""
    found: set[str] = set()
    for arg in argv:
        spellings = {arg, arg.partition("=")[2], *arg.split("=")[1:]}
        for spelling in spellings:
            for piece in {spelling, *spelling.split(":")}:
                if piece.startswith("/"):
                    found.add(piece)
                elif re.match(r"-[A-Za-z]/", piece):
                    found.add(piece[2:])
    return found


def _cmdline_holds(pid: Path, number: int) -> list[Hold]:
    """What a non-dumpable process of this user still gives away on its command line: absolute paths (the
    program, ``--target-dir=/x``, ``-L/x``, ``a=b=/x``, ``/a:/b``). A relative program path (``./target/x``)
    cannot be resolved without its cwd, so its basename, and the process's ``comm``, are held as names: a
    regular file of that name in the target keeps it. Other relative arguments are not looked at. ``ping``
    and ``sudo`` name nothing of a target."""
    try:
        comm = (pid / "comm").read_text().strip()
        argv = [os.fsdecode(a) for a in (pid / "cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []  # unreadable or gone: skipped
    if not argv:
        return []
    holds: list[Hold] = [(number, comm, "uses", Path(item), _NO_IDS) for item in sorted(_cmdline_paths(argv))]
    names = {comm}
    if not argv[0].startswith("/"):
        names.add(os.path.basename(argv[0]))
    # a rewritten title (``sshd-session: user@pts/3``) is not the name of a file
    holds.extend((number, comm, "named", Path(name), _NO_IDS) for name in sorted(names)
                 if name and not re.search(r"\s", name) and not (name != comm and re.search(r"\s", argv[0])))
    return holds


def _files_named(target: Path, names: set[str]) -> dict[str, Path]:
    """One walk of ``target``: for each of ``names`` a regular file of that name (or whose first 15
    characters are it: ``comm`` is cut there)."""
    found: dict[str, Path] = {}
    for directory, _dirs, files in os.walk(target):  # walk() never follows links to directories
        for name in files:
            for key in {name, name[:15]} & names:
                path = Path(directory, name)
                if key not in found and path.is_file() and not path.is_symlink():
                    found[key] = path
        if len(found) == len(names):
            break
    return found


def _proc_claims(pid: Path, number: int, name: str) -> list[Hold]:
    """Target directories a cargo-family process builds into: ``CARGO_TARGET_DIR`` and ``--target-dir``."""
    cwd = os.readlink(pid / "cwd")
    environ = (pid / "environ").read_bytes().split(b"\0")
    argv = [os.fsdecode(a) for a in (pid / "cmdline").read_bytes().split(b"\0")]
    pairs = [os.fsdecode(e).split("=", 1) for e in environ if b"=" in e]
    values = [value for key, value in pairs if key in _TARGET_ENV]
    return [(number, name, "builds into", Path(os.path.join(cwd, v)), _NO_IDS) for v in values + _argv_target_dirs(argv)]


_HOME_SKIP = {"Library"}  # beyond the hidden directories: macOS application data is not a project tree


def _is_home(root: Path) -> bool:
    """True for the user's home directory: as a root it is searched with the exclusions above (a configured
    ``~/.cache`` or ``~/.cargo/...`` root is searched as named)."""
    try:
        return root.samefile(Path.home())
    except OSError:
        return False


def _mount_points() -> frozenset[str]:
    """Mount points of this host (Linux), so that a mount is passed by its name and never stat'ed: a stat on
    a dead network mount blocks. Elsewhere the deadline of the survey child is the protection."""
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return frozenset()
    found = set()
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            found.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
    return frozenset(found)


def _discover(root: Path, managed: bool = True, unreadable: list[str] | None = None,
              trash: list[Path] | None = None, notes: list[str] | None = None,
              exclude: frozenset[Path] = frozenset()) -> list[Path]:
    """Cargo targets under ``root``. ``managed`` also returns cache-only targets and Herdr-managed
    ``targets/<workspace>`` dirs (what the sweep reclaims); migration wants repository targets only.
    A subdirectory that cannot be read is skipped (and named in ``unreadable``), never the whole root.
    Leftovers of an interrupted orphan removal (``targets/.trash-*``) are listed in ``trash``, never as targets.
    Only directories that are entered count against the limit; past it the walk goes no deeper and says so in
    ``notes``, keeping what it found. Mount points and ``exclude`` (other roots, walked on their own) are not
    entered; a directory is stat'ed only when it is about to be entered or kept."""
    pending = [(root, 0)]
    device = root.stat().st_dev
    found = []
    count = 0
    home_root = _is_home(root)
    mounts = _mount_points() - {str(root)}
    while pending:
        directory, depth = pending.pop()
        try:
            managed_dir = (managed and directory.name == "targets"
                           and (directory / ".herdr-target-root.json").is_file())
            scanner = os.scandir(directory)
        except OSError:
            if directory == root:
                raise
            if unreadable is not None:
                unreadable.append(str(directory))
            continue
        with scanner as entries:
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                path = Path(entry.path)
                if entry.path in mounts or path in exclude:
                    continue

                def local(entry=entry) -> bool:
                    return entry.stat(follow_symlinks=False).st_dev == device
                if managed_dir and entry.name.startswith(".trash-"):
                    if trash is not None and local():
                        trash.append(path)
                    continue
                if managed_dir and entry.name == ".owners":
                    continue  # the manager's ownership records, not a target
                if managed_dir and _managed_workspace(path) is not None:
                    if local() and _cargo_markers(path):
                        found.append(path)
                    continue
                if entry.name == "target":
                    if local() and _cargo_markers(path) and (
                            (directory / "Cargo.toml").is_file() or (managed and _cache_only_target(path))):
                        found.append(path)
                    continue
                if entry.name in _PRUNE:
                    continue
                if home_root and (entry.name.startswith(".") or entry.name in _HOME_SKIP):
                    continue  # ~/.cargo, ~/.rustup, the trash and other dot directories are never entered
                if depth >= _MAX_DEPTH or not local():
                    continue  # deeper directories are outside this bounded sweep
                count += 1
                if count > _MAX_ENTRIES:
                    note = f"Discovery directory limit reached under {root}; kept what was found"
                    if notes is None:
                        raise OSError(note)
                    if note not in notes:
                        notes.append(note)
                    continue
                pending.append((path, depth + 1))
    return found


_CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


def _cargo_markers(target: Path) -> bool:
    return any((target / m).exists() for m in
               (".rustc_info.json", "debug/.fingerprint", "release/.fingerprint"))


def _cache_only_target(target: Path) -> bool:
    """A Cargo target not beside a manifest (shared pool, CARGO_TARGET_DIR) proven by CACHEDIR.TAG."""
    tag = target / "CACHEDIR.TAG"
    try:
        return (not tag.is_symlink() and tag.is_file()
                and tag.read_bytes()[:128].find(_CACHEDIR_SIGNATURE) >= 0)
    except OSError:
        return False


def _managed_workspace(path: Path) -> Path | None:
    """Workspace of a per-worktree managed target (``targets/<label>-<id>``), proven by the manager's
    root marker and the target's own marker, or, when a killed ``cargo clean`` took the marker, the
    manager's ownership record kept outside the target."""
    try:
        root_marker, marker = path.parent / ".herdr-target-root.json", path / TARGET_MARKER
        if root_marker.is_symlink() or marker.is_symlink() or not root_marker.is_file():
            return None
        if json.loads(root_marker.read_text()).get("owner_uid") != os.getuid():
            return None
        if marker.is_file():
            data = json.loads(marker.read_text())
        else:
            data = read_owner(path.parent, path.name) or {}
        if data.get("owner_uid") != os.getuid() or not isinstance(data.get("workspace"), str):
            return None
        return Path(data["workspace"])
    except (OSError, ValueError, AttributeError):
        return None


def _project(target: Path) -> Path:
    """Directory whose activity keeps a target alive: its manifest directory, or the owning workspace."""
    return _managed_workspace(target) or target.parent


_CLEAN_SECONDS = 900  # one ``cargo clean`` may take this long


def _chain(path: Path) -> set[Ident]:
    """(device, inode) of ``path`` and of every ancestor that exists. Two spellings of one directory (case,
    symlinks, bind mounts) give the same identity, which a string comparison would miss."""
    found: set[Ident] = set()
    for item in (path, *path.parents):
        try:
            info = os.stat(item)
        except OSError:
            continue
        found.add((info.st_dev, info.st_ino))
    return found


def _identity(path: Path) -> Ident | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _inventory(target: Path) -> set[Ident]:
    """(device, inode) of everything inside ``target``: what a process in another mount namespace, which
    spells the path its own way, can be told apart by."""
    found: set[Ident] = set()
    pending = [target]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    found.add((info.st_dev, info.st_ino))
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError:
            continue  # vanished or unreadable: whatever is there cannot be matched, nor deleted by cargo
    own = _identity(target)
    if own:
        found.add(own)
    return found


def _busy_reason(holds: list[Hold], target: Path) -> str | None:
    """Why ``target`` must not be cleaned right now, or None. Busy: a process has its cwd, executable, an
    open file or a mapping inside the target or its workspace, or a cargo-family process was told to build
    into the target. What is compared is identity ((device, inode) from the kernel where the scan has it,
    else of the path and its ancestors), never the spelling of a path. Processes elsewhere do not matter.
    A hold of kind ``named`` (a non-dumpable process known only by a relative program path or its ``comm``)
    is busy when a regular file of that name, or of that 15-character prefix, is anywhere in the target.

    This stats files of arbitrary processes; on a dead network mount that can block, so the sweep runs it
    in a child process with a deadline (``_busy_check``)."""
    mine = {ident for ident in (_identity(target), _identity(_project(target))) if ident}
    inside = mine | _inventory(target)
    target_id = _identity(target)
    target_chain = _chain(target)
    files = _files_named(target, {path.name for _p, _n, how, path, _i in holds if how == "named"})
    for pid, name, how, path, ids in holds:
        if how == "named":
            if path.name in files:
                return f"{name} (pid {pid}) is named like {files[path.name]}"
        elif how in ("uses", "cwd"):
            if (ids | _chain(path)) & inside:
                return f"{name} (pid {pid}) uses {path}"
        elif (target_id and target_id in _chain(path)) or (_identity(path) in target_chain):
            return f"{name} (pid {pid}) builds into {path}"
    return None


_BUSY_CHILD = ("import sys; from herdr_engineering import build_cleanup as c; "
               "c._busy_child(sys.argv[1], int(sys.argv[2]), [int(p) for p in sys.argv[3].split(',') if p])")
_BUSY_SECONDS = 120.0  # the scan of a normal host takes a second; ``HERDR_BUSY_CHECK_SECONDS`` overrides


def _busy_child(target: str, sweeper: int, excused: Sequence[int] = ()) -> None:
    """The sweeper that asked is not a holder (it may be run from inside the project it sweeps), and neither
    are ``excused`` processes (the invoker's ancestors) for their working directory alone: an open file or a
    mapping of theirs inside the target still counts."""
    try:
        holds = [hold for hold in _scan() if hold[0] != sweeper and not (hold[2] == "cwd" and hold[0] in excused)]
        print(json.dumps({"busy": _busy_reason(holds, Path(target))}))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))


def _deadline(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        log.warning("ignoring %s=%r (not a positive number); using %g", name, raw, default)
        return default
    return seconds


def _busy_deadline() -> float:
    return _deadline("HERDR_BUSY_CHECK_SECONDS", _BUSY_SECONDS)


_SURVEY_SECONDS = 600.0  # discovery and size checks of one root; ``HERDR_SURVEY_SECONDS`` overrides


def _survey_deadline() -> float:
    return _deadline("HERDR_SURVEY_SECONDS", _SURVEY_SECONDS)


def _ancestors() -> frozenset[int]:
    """This process and every process above it (its shell, the terminal, the agent that started it)."""
    chain, pid = {os.getpid()}, os.getppid()
    while pid > 1 and pid not in chain and len(chain) < 64:
        chain.add(pid)
        try:
            try:  # Linux: the parent is the second field after the command name
                pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError):
                pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                                         timeout=10, check=False).stdout.strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return frozenset(chain)


def _in_child(work: Callable[[], object], seconds: float) -> object:
    """``work()`` in a forked child with a deadline: its JSON-able result, or ``OSError`` for what it raised.
    A child that does not answer (a stat on a dead network mount blocks in the kernel, unkillable) has its
    whole process group killed and is never waited for, so the sweep that asked can fail closed and let go
    of its locks. Forked, not spawned, so that it sees the same module state; it only touches the file system."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        code = 0
        try:
            os.setsid()
            os.close(reader)
            try:
                payload = json.dumps({"ok": work()})
            except BaseException as exc:  # noqa: BLE001 - whatever it was, the parent hears of it
                payload = json.dumps({"error": f"{exc}" or type(exc).__name__})
            data = payload.encode()
            while data:
                data = data[os.write(writer, data):]
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(writer)
    deadline, chunks, answered = time.monotonic() + seconds, [], False
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([reader], [], [], left)[0]:
                raise TimeoutError(f"survey timed out after {seconds:g} s")
            chunk = os.read(reader, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        answered = True
    finally:
        os.close(reader)
        if not answered:
            for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
                try:
                    kill()
                except OSError:
                    continue
        for _ in range(20 if answered else 1):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    break
            except ChildProcessError:
                break
            time.sleep(0.1 if answered else 0)
    try:
        answer = json.loads(b"".join(chunks))
    except ValueError as exc:
        raise OSError("survey child gave no answer") from exc
    if "error" in answer:
        raise OSError(answer["error"])
    return answer["ok"]


def _busy_check(target: Path, excused: Sequence[int] = ()) -> str | None:
    """``_busy_reason`` over a fresh process scan, run in a child with a deadline so that a stat that hangs
    (a dead NFS or sshfs mount a process sits on) can never hang the sweep or a build's disk guard. The child
    runs from ``/`` without the script directory on its path (the sweeper's cwd is a user workspace). One that
    does not answer in time, or any other failure here, gets its whole process group killed (``lsof``
    included) and is left unwaited if it is stuck in the kernel; the caller fails closed."""
    seconds = _busy_deadline()
    root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")]))}
    child = subprocess.Popen([sys.executable, "-P", "-c", _BUSY_CHILD, str(target), str(os.getpid()),
                              ",".join(map(str, excused))],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, cwd="/",
                             start_new_session=True)
    answered = False
    try:
        try:
            out, _ = child.communicate(timeout=seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"busy check timed out after {seconds:g} s") from exc
        answered = True
    finally:
        if not answered and child.returncode is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # stuck in the kernel: abandoned, never waited for
    try:
        answer = json.loads(out.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise OSError("busy check gave no answer") from exc
    if "error" in answer:
        raise OSError(answer["error"])
    return answer["busy"]


def _orphaned(target: Path) -> bool:
    """True when the managed target's worktree is provably gone: ``lstat`` of its recorded absolute path says
    it does not exist (and nothing else: a permission error says nothing) while the directory that held it
    can be listed (a missing parent may be an unmounted volume, not a deletion)."""
    workspace = _managed_workspace(target)
    if workspace is None or not workspace.is_absolute():
        return False
    try:
        os.lstat(workspace)
        return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        with os.scandir(workspace.parent) as entries:
            next(entries, None)
    except OSError:
        return False
    return True


def _remove_orphan(target: Path) -> bool:
    """Delete an orphaned managed target, deciding again under the manager lock (False: not an orphan).

    The decision and one atomic rename to ``targets/.trash-<name>-<pid>`` happen under the lock; the slow
    ``rmtree`` runs after it. A removal that stops part-way leaves only that trash directory, which the
    next sweep's discovery removes: nothing can be using it.
    """
    # CPython's fd-based rmtree prevents symlink substitution attacks.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError("This platform lacks symlink-safe recursive removal")
    trash = target.parent / f".trash-{target.name}-{os.getpid()}"
    with _manager_lock(target.parent):
        if not _orphaned(target):
            return False
        if os.path.lexists(trash):
            shutil.rmtree(trash)
        os.rename(target, trash)
        owner_record(target.parent, target.name).unlink(missing_ok=True)
    shutil.rmtree(trash)
    return True


def _cargo_clean(target: Path, timeout: float = _CLEAN_SECONDS) -> str | None:
    """``cargo clean`` for the target's workspace (None on success, else why not). Cargo takes its own
    locks; a managed target is cleaned under the manager lock, so a build that starts meanwhile waits in
    ``prepare_target`` and then finds a freshly prepared target."""
    from .build_hygiene import resolve_real_cargo
    managed = _managed_workspace(target)
    workspace = managed or target.parent
    manifest = workspace / "Cargo.toml"
    if not manifest.is_file() or manifest.is_symlink():
        return "no workspace to clean through"
    try:
        proxy = Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo") / "bin/cargo"
        cargo = proxy if proxy.is_file() and os.access(proxy, os.X_OK) else resolve_real_cargo(Path.home())
        env = {**os.environ, "CARGO_NET_OFFLINE": "true", "RUSTUP_AUTO_INSTALL": "0"}
        for key in ("RUSTC_WRAPPER", "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_BUILD_DIR"):
            env.pop(key, None)
        # build-dir is pinned to the target too: a shared build-dir (config or environment) is never deleted
        argv = [str(cargo), "clean", "--manifest-path", str(manifest), "--target-dir", str(target),
                "--config", f"build.build-dir={json.dumps(str(target))}"]
        if managed is None:
            done = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        else:
            with _manager_lock(target.parent):
                marker = target / TARGET_MARKER
                if marker.exists():
                    identity = marker.read_bytes()
                else:  # a killed clean took it earlier: the manager's own record says what it was
                    owner = read_owner(target.parent, target.name)
                    if owner is None:
                        return "no ownership record to restore the marker from"
                    identity = (json.dumps(owner, sort_keys=True) + "\n").encode()
                write_cachedir_tag(target)  # newer cargo refuses to clean a directory without one
                try:
                    done = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True,
                                          timeout=timeout, check=False)
                finally:  # however cargo ended (timeout, signal, error), the directory stays a managed one
                    if not os.path.lexists(target):  # a workspace alias must not be left dangling
                        target.mkdir(mode=0o700)
                    if not os.path.lexists(marker):
                        marker.write_bytes(identity)
                    write_cachedir_tag(target)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"cargo clean could not run: {exc}"
    if done.returncode != 0:
        return f"cargo clean failed: {(done.stderr or done.stdout).strip().splitlines()[-1:] or ['?']}"
    return None


def _inspect(target: Path, stop_at: int | None = None) -> tuple[float, int]:
    """Return newest mtime and allocated size, rejecting links and nested sources. With ``stop_at`` the walk
    ends as soon as the size passes it (the size is then only a lower bound, and the mtime partial)."""
    device = target.lstat().st_dev
    pending = [target]
    newest = 0.0
    size = 0
    count = 0
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        count += 1
        if count > _MAX_ENTRIES:
            raise OSError(f"Inspection entry limit reached at {target}")
        if metadata.st_uid != os.getuid() or metadata.st_dev != device:
            raise OSError(f"Foreign owner or mount inside {target}")
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"Symlink inside {target}")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise OSError(f"Special file inside {target}")
        if path.name in {"Cargo.toml", ".git"}:
            raise OSError(f"Source or repository marker inside {target}")
        newest = max(newest, metadata.st_mtime)
        size += metadata.st_blocks * 512
        if stop_at is not None and size > stop_at:
            break
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return newest, size


_SHALLOW_DEPTH = 4  # target / profile / .fingerprint | build | incremental / <unit> / files
_NOT_LISTED = {"deps", "examples"}  # the bulk of a profile: only the directory itself is looked at


def _shallow_newest(target: Path) -> float:
    """Newest mtime of Cargo's bookkeeping instead of every file: the target, its profile directories, and
    what ``.fingerprint``, ``build`` and ``incremental`` hold, down to the fingerprint files (``invoked.timestamp``
    is touched by every build). ``deps`` is not listed, but its own mtime moves whenever a file is created or
    replaced in it, which a build always does. Costs a few entries per crate, however large the target is."""
    newest, count = 0.0, 0
    pending = [(target, 0)]
    while pending:
        path, depth = pending.pop()
        info = path.lstat()
        count += 1
        if count > _MAX_ENTRIES:
            raise OSError(f"Inspection entry limit reached at {target}")
        newest = max(newest, info.st_mtime)
        if stat.S_ISDIR(info.st_mode) and depth < _SHALLOW_DEPTH and not (depth == 2 and path.name in _NOT_LISTED):
            with os.scandir(path) as entries:
                pending.extend((Path(entry.path), depth + 1) for entry in entries)
    return newest


def _measure(target: Path, cap: int | None, full: bool) -> dict:
    """What the sweep needs to know of one target: newest mtime, size and whether it is over ``cap``.
    ``full`` (disk pressure) walks everything, as before. Otherwise only the cap is being decided: the walk
    stops once the size passes it, and the idle time then comes from ``_shallow_newest``, so a target of
    millions of files is judged as cheaply as it is proven large. A target that cannot be decided raises."""
    if full:
        try:
            newest, size = _inspect(target)
            return {"newest": newest, "size": size, "over": cap is not None and size > cap}
        except OSError:
            if cap is None:
                raise
    assert cap is not None
    _, size = _inspect(target, stop_at=cap)
    if size <= cap:
        return {"newest": 0.0, "size": size, "over": False}
    return {"newest": _shallow_newest(target), "size": size, "over": True}


def _survey(spec: dict) -> dict:
    """Discovery of a root (or the known ``targets``) and the measurement of each target, in one pass that
    ``_in_child`` bounds in time: ``{"rows": [...], "unreadable": [...], "trash": [...], "notes": [...]}``."""
    root, cap, full = Path(spec["root"]), spec["cap"], spec["full"]
    unreadable: list[str] = []
    trash: list[Path] = []
    notes: list[str] = []
    if spec["targets"] is not None:
        targets = [Path(t) for t in spec["targets"] if _recognized(Path(t))]
    else:
        targets = _discover(root, unreadable=unreadable, trash=trash, notes=notes,
                            exclude=frozenset(Path(p) for p in spec["exclude"]))
    device = root.stat().st_dev
    rows = []
    for target in targets:
        row: dict = {"path": str(target)}
        try:
            if target.stat().st_dev != device:
                row["foreign_device"] = True
            else:
                row.update(_measure(target, cap, full))
        except OSError as exc:
            row["error"] = str(exc)
        rows.append(row)
    return {"rows": rows, "unreadable": unreadable, "trash": [str(t) for t in trash], "notes": notes}


_DISCOVERY_REFRESH = 6 * 3600  # a cached target list is walked afresh after this long


def _cache_file(state_dir: Path) -> Path:
    return state_dir / "build-cleanup-targets.json"


def _load_cache(state_dir: Path) -> dict:
    try:
        data = json.loads(_cache_file(state_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store_cache(state_dir: Path, root: Path, targets: list[str]) -> None:
    data = _load_cache(state_dir)
    data[str(root)] = {"at": time.time(), "targets": targets}
    _write_json(state_dir, _cache_file(state_dir).name, data)


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess:
    """``git -C directory ...`` in a clean environment: ``GIT_DIR``, ``GIT_INDEX_FILE`` and the rest, inherited
    from a hook or a wrapper script, would point git at some other repository."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, timeout=30, check=False,
                          env=clean)


def _untracked(target: Path) -> bool:
    """Git must verify the entire target is untracked, including nested files, in the nearest repository
    that holds the workspace. A work tree rooted at the home directory (``git init ~`` for dotfiles) is no
    project repository: it says nothing about a project inside it, which is then left alone like one outside
    git."""
    if _managed_workspace(target) is not None:
        return True  # lives in the state directory, not in a repository
    cache_only = not (target.parent / "Cargo.toml").is_file()
    top = _git(target.parent, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return cache_only  # outside any repository: only a cache-only directory (CACHEDIR.TAG) may go
    try:
        if Path(os.fsdecode(top.stdout.strip())).resolve() == Path.home().resolve():
            return False
    except OSError:
        return False
    result = _git(target.parent, "ls-files", "--", target.name)
    return result.returncode == 0 and not result.stdout


def _recognized(target: Path) -> bool:
    """Same recognition rule as discovery: a Cargo ``target`` with build markers."""
    if _managed_workspace(target) is not None:
        return _cargo_markers(target)
    return (target.name == "target" and _cargo_markers(target)
            and ((target.parent / "Cargo.toml").is_file() or _cache_only_target(target)))


def _lease_block(state_dir: Path, target: Path) -> str | None:
    """Skip reason from the build registry, or None. Fresh read on every call."""
    from .build_registry import RegistryError, active_scopes, db_path
    if not db_path(state_dir).exists() and not db_path(state_dir).is_symlink():
        return None
    try:
        scopes = active_scopes(state_dir)
    except RegistryError:
        return "registry_unreadable"
    project = _project(target)
    for raw in scopes:
        if not raw.startswith("/"):
            continue  # named scopes do not describe a path
        scope = Path(raw)
        if project.is_relative_to(scope) or scope.is_relative_to(project):
            return "active_lease"
    return None


def _usage(path: Path) -> float:
    """Percent used, by the one measurement every disk decision shares (``df``; see ``disk_guard.measure``)."""
    return measure(path)[0]


def _write_json(state_dir: Path, name: str, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".build-cleanup-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, state_dir / name)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _write_status(state_dir: Path, result: dict, name: str = "build-cleanup.json") -> None:
    _write_json(state_dir, name, result)


def run_once(
    roots: list[str | Path], state_dir: str | Path, dry_run: bool = False,
    high_water: float = 85, low_water: float = 80, min_age_hours: float = 24,
    pressure_water: float = 90, pressure_min_age_hours: float = 2,
    only_device: int | None = None,
    max_target_bytes: int | None = None, size_cap_min_age_hours: float | None = None,
    only_targets: list[Path] | None = None, invoker: Sequence[int] = (), discovery: str = "walk",
) -> dict:
    """Run one bounded sweep and atomically save its inspectable status.

    ``state_dir`` must be the same build-gates directory used by ``run_gated``.
    ``reclaimed_bytes`` measures observed free-space gain; ``removed_bytes`` is
    the allocated size of removed files. Concurrent filesystem activity can
    affect the former. Targets must be idle ``min_age_hours``; when a root's usage reaches
    ``pressure_water`` the idle floor drops to ``pressure_min_age_hours`` (never below
    ``MIN_IDLE_HOURS``). Orphaned managed targets go at any age; every other target needs the idle floor
    and no process of this user inside it or its workspace (see the module docstring). Lease and git guards apply in both
    tiers. Dry runs fully validate eligible targets but never remove them. Busy sweeps return immediately
    without overwriting the last report. ``only_device`` limits the sweep to roots on that filesystem (the
    one under pressure).

    ``max_target_bytes`` (None: no cap) also cleans an idle target larger than that, on every sweep and below
    ``high_water`` too (``reasons[path] == "size_cap"``), through the same checks. Such a target needs
    ``size_cap_min_age_hours`` of idle time (default ``min_age_hours``; the after-build pass uses 0, because
    its target was written a moment ago: the process scan and the leases then decide). ``only_targets``
    limits the pass to those targets, found under ``roots`` without a discovery walk, and its report goes to
    ``build-cleanup-last-build.json``, never over the sweep's ``build-cleanup.json`` (whose timestamp says when
    the periodic sweep last ran). ``invoker``: processes (the caller and its ancestors) whose working
    directory alone does not keep a target busy.

    ``discovery``: ``walk`` looks for targets afresh; ``cached`` reuses each root's list of targets
    for 6 hours; ``cache_only`` (the disk guard before a build) never walks the home directory, and uses what
    a sweep found before. The discovery and the size checks of a root run in a child with a deadline
    (``HERDR_SURVEY_SECONDS``, default 600): a root that does not answer is skipped with a warning.
    """
    if not all(math.isfinite(v) for v in (high_water, low_water, min_age_hours,
                                          pressure_water, pressure_min_age_hours)):
        raise ValueError("Cleanup policy values must be finite")
    if max_target_bytes is not None and max_target_bytes < 0:
        raise ValueError("max_target_bytes must not be negative")
    if size_cap_min_age_hours is not None and not (
            math.isfinite(size_cap_min_age_hours) and size_cap_min_age_hours >= 0):
        raise ValueError("size_cap_min_age_hours must be a finite number, 0 or more")
    if discovery not in ("walk", "cached", "cache_only"):
        raise ValueError("discovery must be walk, cached or cache_only")
    if not 0 <= low_water < high_water <= 100 or not 0 <= pressure_water <= 100:
        raise ValueError("Require 0 <= low_water < high_water <= 100 and 0 <= pressure_water <= 100")
    if min_age_hours < MIN_IDLE_HOURS or pressure_min_age_hours < MIN_IDLE_HOURS:
        raise ValueError(f"Idle floors must be at least {MIN_IDLE_HOURS} hour(s)")
    state_dir = Path(state_dir).expanduser().absolute()
    state_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": datetime.now(UTC).isoformat(), "status": "ok", "dry_run": dry_run,
        "roots": [], "removed_count": 0, "removed_bytes": 0, "reclaimed_bytes": 0,
        "removed": [], "would_remove": [], "skipped": [], "errors": [], "warnings": [],
        "reconciled": [], "reasons": {}, "pressure": False, "process_scans": 0, "aborted": False, "blocked": [],
    }
    lock_fds = [os.open(state_dir / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                for name in ("build-cleanup.lock", "build-maintenance.lock")]
    try:
        try:
            fcntl.flock(lock_fds[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_fds[1], fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            result["status"] = "busy"
            return result
        _reconcile_registry(state_dir, result)
        cap = None if max_target_bytes is None else (max_target_bytes, min_age_hours if
                                                     size_cap_min_age_hours is None else size_cap_min_age_hours)
        _sweep(roots, state_dir, result, high_water, low_water, min_age_hours,
               pressure_water, pressure_min_age_hours, only_device, cap, only_targets, tuple(invoker), discovery)
        if result["warnings"]:
            result["status"] = "warning"
        if result["errors"]:
            result["status"] = "error"
        _write_status(state_dir, result, "build-cleanup.json" if only_targets is None
                      else "build-cleanup-last-build.json")  # a one-target pass is not the last full report
        if not dry_run and only_targets is None:
            from .build_metrics import emit_event
            emit_event(state_dir, "cleanup_completed", kind="cleanup", status=result["status"],
                       removed_count=result["removed_count"], removed_bytes=result["removed_bytes"])
        return result
    finally:
        for fd in lock_fds:
            os.close(fd)


def _reconcile_registry(state_dir: Path, result: dict) -> None:
    """Fail attempts whose lease expired and whose owner is provably dead. Only registry rows change; no
    artifact is touched, and an existing registry is required (the sweep never creates one)."""
    from .build_registry import Registry, RegistryError, active_scopes, db_path
    if not db_path(state_dir).exists():
        return
    try:
        active_scopes(state_dir)  # read-only probe: an unreadable registry must stay unreadable (fail closed)
    except RegistryError:
        return
    try:
        result["reconciled"] = Registry(state_dir).reconcile()
    except Exception as exc:  # noqa: BLE001 - a registry fault must not stop the sweep
        result["warnings"].append(f"registry reconcile failed: {type(exc).__name__}")


def _skip_for_registry(state_dir: Path, target: Path, name: str, result: dict) -> bool:
    reason = _lease_block(state_dir, target)
    if reason is None:
        return False
    result["skipped"].append({"path": name, "reason": reason})
    if reason == "registry_unreadable":
        note = "build registry exists but is unreadable; skipping all candidates this pass"
        if note not in result["warnings"]:
            result["warnings"].append(note)
    return True


def _skip_root(result: dict, root: Path, reason: str) -> None:
    """One bad root never stops the sweep of the others (the managed pools come last)."""
    note = f"skipped root {root}: {reason}"
    log.warning("build cleanup %s", note)
    result["warnings"].append(note)


def _survey_root(root: Path, state_dir: Path, result: dict, spec: dict, discovery: str) -> dict | None:
    """The survey of one root (``_survey`` in a child with a deadline), using the cached target list where
    ``discovery`` allows; None when the root is not to be looked at now."""
    cached = _load_cache(state_dir).get(str(root)) if spec["targets"] is None else None
    if cached and isinstance(cached.get("targets"), list) and (
            discovery == "cache_only" or (discovery == "cached" and time.time() - cached.get("at", 0) < _DISCOVERY_REFRESH)):
        spec = {**spec, "targets": cached["targets"]}
    elif discovery == "cache_only" and spec["targets"] is None and _is_home(root):
        return None  # before a build the home directory is not walked
    survey = _in_child(lambda: _survey(spec), _survey_deadline())
    if spec["targets"] is None:
        _store_cache(state_dir, root, [row["path"] for row in survey["rows"]])
    return survey  # type: ignore[return-value]


def _candidates(roots, state_dir, result, high_water, min_age_hours, pressure_water, pressure_min_age_hours,
                only_device, cap=None, only_targets=None, discovery="walk"):
    """Targets to consider: (newest mtime, path, size, idle cutoff, reason) each, and the root they live on
    by device. All of a root's targets when it is over ``high_water`` (reason ``pressure``); below it, only
    those over the size ``cap`` (reason ``size_cap``; the cap wins when both apply). Leftovers of an interrupted orphan removal are removed here."""
    candidates, roots_by_device = [], {}
    now = time.time()
    canonical_roots = []
    for raw in roots:
        root = Path(raw).expanduser().absolute()
        try:
            if root == root.resolve(strict=True) and root.is_dir() and root != Path("/"):
                canonical_roots.append(root)
                continue
        except OSError:
            pass
        _skip_root(result, root, "not a canonical project directory")
    for root in canonical_roots:
        if only_device is not None and root.stat().st_dev != only_device:
            continue  # deleting there cannot relieve the filesystem under pressure
        result["roots"].append(str(root))
        usage = _usage(root)
        pressured = usage >= high_water
        if not pressured and cap is None:
            continue
        under_pressure = usage >= pressure_water
        cutoff = now - (min(min_age_hours, pressure_min_age_hours) if under_pressure else min_age_hours) * 3600
        result["pressure"] = result["pressure"] or under_pressure
        roots_by_device[root.stat().st_dev] = root
        spec = {"root": str(root), "cap": None if cap is None else cap[0], "full": pressured,
                "exclude": [str(other) for other in canonical_roots if other != root and other.is_relative_to(root)],
                "targets": None if only_targets is None else [str(t) for t in only_targets if t.parent == root]}
        try:
            survey = _survey_root(root, state_dir, result, spec, discovery)
        except OSError as exc:  # also a root that did not answer in time: skipped, the others still sweep
            _skip_root(result, root, str(exc))
            continue
        if survey is None:
            continue
        for note in survey["notes"]:
            _skip_root(result, root, note)
        for leftover in [Path(t) for t in survey["trash"]] if not result["dry_run"] else []:
            try:
                shutil.rmtree(leftover)  # nothing can be using it: it was renamed out of reach
            except OSError as exc:  # one stuck leftover skips itself; the rest of the root still sweeps
                result["skipped"].append({"path": str(leftover), "reason": f"cannot delete: {exc}"})
        for name in survey["unreadable"][:5]:
            _skip_root(result, Path(name), "unreadable directory skipped")
        for row in survey["rows"]:
            target = Path(row["path"])
            if state_dir.is_relative_to(target):
                raise OSError(f"Maintenance state is inside cleanup target: {target}")
            if row.get("foreign_device"):
                result["skipped"].append({"path": str(target), "reason": "different filesystem"})
                continue
            if "error" in row:
                result["skipped"].append({"path": str(target), "reason": row["error"]})
                if pressured:  # below high water this is a size look only: it is a warning, never a silent ok
                    result["errors"].append(row["error"])
                else:
                    result["warnings"].append(f"size cap undecided for {target}: {row['error']}")
                continue
            newest, size, over_cap = row["newest"], row["size"], row["over"]
            if not pressured and not over_cap:
                continue
            reason = "size_cap" if over_cap else "pressure"
            # the smaller idle time of the reasons that apply (a larger cutoff admits more targets)
            target_cutoff = max(cutoff if pressured else 0.0, now - cap[1] * 3600 if over_cap else 0.0)
            if newest > target_cutoff and not _orphaned(target):
                result["skipped"].append({"path": str(target), "reason": "recent activity"})
            else:
                candidates.append((newest, str(target), size, target_cutoff, reason))
    return candidates, roots_by_device


def _sweep(roots, state_dir, result, high_water, low_water, min_age_hours,
           pressure_water=90, pressure_min_age_hours=2, only_device=None, cap=None, only_targets=None,
           invoker=(), discovery="walk"):
    try:
        candidates, roots_by_device = _candidates(
            roots, state_dir, result, high_water, min_age_hours, pressure_water, pressure_min_age_hours,
            only_device, cap, only_targets, discovery)
    except (OSError, ValueError) as exc:
        result["errors"].append(str(exc))
        result["aborted"] = True  # the sweep itself failed (discovery): nothing was judged
        return
    seen = set()
    for _, name, size, cutoff, reason in sorted(candidates):
        if result["aborted"]:
            break  # a failed busy check stops the sweep: nothing more is judged on a host we cannot read
        if name in seen:
            continue
        seen.add(name)
        target = Path(name)
        try:
            root = roots_by_device[target.stat().st_dev]
            if reason == "pressure" and _usage(root) <= low_water:
                continue  # the pressure sweep stops at low water; a target over the cap goes regardless
            before = shutil.disk_usage(root).free
            if _orphaned(target):
                if _blocked_by_process(target, name, result, invoker):
                    continue  # moved, not deleted: something still builds into it
                if result["dry_run"]:
                    result["would_remove"].append(name)
                    result["reasons"][name] = "orphan"
                    continue
                if not _remove_orphan(target):
                    result["skipped"].append({"path": name, "reason": "worktree is back"})
                    continue
                result["reasons"][name] = "orphan"
            elif not _clean_idle(target, name, cutoff, state_dir, result, reason, invoker):
                continue
        except (OSError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"{name}: {exc}")
            break  # unknown state is a reason to stop, never to broaden deletion
        result["removed_count"] += 1
        result["removed_bytes"] += size
        result["reclaimed_bytes"] += max(0, shutil.disk_usage(root).free - before)
        result["removed"].append(name)
        result["reasons"].setdefault(name, reason)


def _blocked_by_process(target: Path, name: str, result: dict, invoker: Sequence[int] = ()) -> bool:
    """A fresh process scan right before deleting ``target`` (idle or orphan): True when it must stay, with
    the reason in ``skipped`` and ``blocked``. A scan that cannot be read fails closed."""
    result["process_scans"] += 1
    busy = None
    for attempt in range(6):  # a process mid-exec is unreadable for a moment
        try:
            busy = _busy_check(target, invoker)
            break
        except TimeoutError as exc:  # a hung stat will hang again: no retries, fail closed
            busy = f"process list unreadable: {exc}"
            break
        except (OSError, ValueError) as exc:  # still unreadable after retries: fail closed, this target stays
            busy = f"process list unreadable: {exc}"
            if attempt < 5:
                time.sleep(0.25)
    if busy is None:
        return False
    if busy.startswith("process list unreadable"):
        result["errors"].append(busy)
        result["aborted"] = True
    result["blocked"].append({"path": name, "reason": busy})
    result["skipped"].append({"path": name, "reason": busy})
    return True


def _clean_idle(target: Path, name: str, cutoff: float, state_dir: Path, result: dict,
                reason: str = "pressure", invoker: Sequence[int] = ()) -> bool:
    """Clean one idle target through cargo; True when it went. Otherwise the reason is in ``skipped``."""
    def skip(reason: str) -> bool:
        result["skipped"].append({"path": name, "reason": reason})
        return False

    if not _untracked(target):
        return skip("tracked or unverifiable output")
    # revalidate right before deleting (a target past the size cap is judged by its bookkeeping, as in the survey)
    newest = _shallow_newest(target) if reason == "size_cap" else _inspect(target)[0]
    if newest > cutoff:
        return skip("recent activity")
    if not _recognized(target):
        raise OSError(f"Target no longer recognized as a Cargo output: {name}")
    if _skip_for_registry(state_dir, target, name, result):  # cheap: a leased target needs no scan
        return False
    if _blocked_by_process(target, name, result, invoker):
        return False
    if _skip_for_registry(state_dir, target, name, result):  # read again: a lease taken during the scan counts
        return False
    if result["dry_run"]:
        result["would_remove"].append(name)
        result["reasons"][name] = reason
        return False
    failure = _cargo_clean(target)
    return skip(failure) if failure else True


def _build_targets(cmd: list[str], cwd: Path) -> list[Path]:
    """Directories a finished ``cargo`` build may have written: where the command directs its output, or
    the workspace's ``target`` and its Herdr-managed target (which the cargo wrapper picks, out of sight)."""
    from .build_hygiene import resolve_real_cargo
    from .cargo_target_storage import target_directory, target_override, workspace_root
    override = target_override(cmd[1:], os.environ, cwd)
    if isinstance(override, Path):
        return [override]
    if override:
        return []  # directed to a place only a config file names: not guessed
    workspace = workspace_root(resolve_real_cargo(Path.home()), cmd[1:], cwd)
    from .build_maintenance import _managed_output_dirs
    managed = [target_directory(root.parent / "cache", workspace) for root in _managed_output_dirs()
               if root.name == "targets"]
    return [workspace / "target", *managed]


_AFTER_BUILD_WINDOW = 24 * 3600  # one clean per target per day: a project that is always over the cap is not churned
_AFTER_BUILD_MEMORY = 7 * 24 * 3600  # how long cleans are remembered for the "capped again" warning


def size_cap_applies(cmd: list[str], cfg: dict) -> bool:
    """True for a cargo build/check/test/... while ``build_cleanup`` is enabled with a size cap."""
    try:
        from .build_maintenance import policy_kwargs
        from .cargo_host import BUILD_SUBCOMMANDS, cargo_subcommand
        return (bool(cmd) and bool(cfg.get("enabled", True)) and policy_kwargs(cfg)["max_target_bytes"] is not None
                and os.path.basename(cmd[0]) == "cargo"
                and cargo_subcommand(cmd[1:]) in BUILD_SUBCOMMANDS - {"clean"})
    except Exception:  # noqa: BLE001 - a malformed command or policy: no cap pass
        return False


def _after_build_record(state_dir: Path) -> dict:
    try:
        data = json.loads((state_dir / "build-cleanup-after-build.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def finish_build(cmd: list[str], cwd: Path, cfg: dict, state_dir: Path, code: int) -> dict | None:
    """The size-cap pass after a build that ended with exit status ``code``: only a build that succeeded is
    followed by one (a failed or interrupted build leaves the target for the next attempt to reuse).
    Never raises, for any caller."""
    if code != 0:
        return None
    try:
        return after_build_size_cap(cmd, cwd, cfg, state_dir)
    except KeyboardInterrupt:  # Ctrl-C during the pass: the build's result stands, the locks are released
        log.warning("build cleanup: size cap pass interrupted")
        return None


def after_build_size_cap(cmd: list[str], cwd: Path, cfg: dict, state_dir: Path) -> dict | None:
    """Size-cap pass for the target of one finished ``cargo`` build (the result of ``run_once``), or None when
    nothing applies: cleanup or the cap is off, the command is not a cargo build, or it has no target.

    Called after the build's lease is released. Only the build's own target is looked at, and it goes only if
    it is over the cap and no other build or process holds it (the lease and process checks of every sweep;
    the invoking shell and its ancestors do not hold it by their working directory alone). The idle floor is
    0, as the target was written a moment ago. A target is cleaned at most once in 24 hours, and cleaned
    again within a week gets a warning to raise ``max_target_gb``. A pass that is blocked says why at WARNING.
    Never raises: a failure is logged and the build's exit code is untouched."""
    try:
        from .build_maintenance import policy_kwargs
        policy = policy_kwargs(cfg)
        if not size_cap_applies(cmd, cfg):
            return None
        targets = [t for t in _build_targets(cmd, cwd) if t.is_dir() and not t.is_symlink()]
        removed: dict = {"removed": [], "errors": [], "skipped": []}
        record = _after_build_record(state_dir)
        now = time.time()
        invoker = _ancestors()
        for target in targets:
            history = [t for t in record.get(str(target), []) if isinstance(t, (int, float))
                       and now - t < _AFTER_BUILD_MEMORY]
            if history and now - max(history) < _AFTER_BUILD_WINDOW:
                log.info("build cleanup: size cap pass skipped, %s was cleaned less than a day ago", target)
                continue
            result = run_once([target.parent], state_dir, **{**policy, "size_cap_min_age_hours": 0,
                                                             "high_water": 100, "low_water": 0},
                              only_targets=[target], invoker=sorted(invoker))
            for key in removed:
                removed[key].extend(result.get(key, []))
            if result.get("status") == "busy":
                log.info("build cleanup: size cap pass skipped, another sweep runs: %s", target)
            for blocked in result.get("blocked", []):
                log.warning("build cleanup: size cap pass blocked for %s: %s", blocked["path"], blocked["reason"])
            for warning in result.get("warnings", []):
                log.warning("build cleanup: %s", warning)
            if str(target) in result.get("removed", []):
                if history:
                    log.warning("build cleanup: %s went over max_target_gb again after being cleaned; "
                                "a higher max_target_gb would keep its build cache", target)
                record[str(target)] = [*history, now]
                try:
                    _write_json(state_dir, "build-cleanup-after-build.json", record)
                except OSError as exc:
                    log.warning("build cleanup: cannot record the clean of %s: %s", target, exc)
        return removed if targets else None
    except Exception as exc:  # noqa: BLE001 - a cleanup problem must never fail the build
        log.warning("build cleanup: size cap pass after the build failed: %s: %s", type(exc).__name__, exc)
        return None
