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
   and releases its lock. Every uid's process is scanned where /proc lets us read it. Limit: where it does
   not (``hidepid``, macOS without root, where ``lsof`` lists only this user's files) a target used by
   another uid's process is NOT detected.

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
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
    A process that has exited is not managed; an ancestor that exits mid-walk is retried once
    from a fresh ``PPid`` (the child was reparented).
    """
    for attempt in (0, 1):
        try:
            return _ancestry_is_herdr(pid, proc)
        except (FileNotFoundError, ProcessLookupError) as exc:
            if not pid.exists():
                return False  # the process itself exited
            if attempt:
                raise OSError(f"Process ancestry changed while inspecting {pid}") from exc
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
# (pid, process name, how, path): "uses" is a cwd, executable, open file or mapping; "builds into" is the
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
        result = subprocess.run(["lsof", "-w", "-n", "-P", "-u", str(os.getuid()), "-Fpcn"],
                                capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"Cannot inspect running processes: {exc}") from exc
    if result.returncode > 1 or (result.returncode == 1 and not result.stdout):
        raise OSError("Cannot inspect running processes: lsof failed")
    holds: list[Hold] = []
    pid, name = 0, ""
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid, name = int(line[1:]), ""
        elif line.startswith("c"):
            name = line[1:]
        elif line.startswith("n/") and pid != os.getpid():  # like the /proc scan, ignore this process
            holds.append((pid, name, "uses", Path(line[1:]), _NO_IDS))
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


def _scan(proc: Path = Path("/proc")) -> list[Hold]:
    """Everything this user's other processes hold or are building into, from one look at the host."""
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
            values: list[tuple[str, frozenset[Ident]]] = []
            for holder in _holders(pid):
                try:
                    for link in _links(holder):
                        try:
                            values.append((os.readlink(link), _link_ids(link, link.name == "cwd")))
                        except (FileNotFoundError, ProcessLookupError):  # descriptor closed / process exited
                            continue
                    values.extend(_mapped(holder))
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError as exc:
                    if holder != pid:  # a live thread of a zombie leader that cannot be read: unverifiable
                        raise OSError(f"Cannot inspect thread {holder}") from exc
                    raise
            holds.extend((number, name, "uses", Path(v.removesuffix(" (deleted)")), ids)
                         for v, ids in values if v.startswith("/"))
            if _cargo_family(name):
                holds.extend(_proc_claims(pid, number, name))
        except (FileNotFoundError, ProcessLookupError):  # process exited during inspection
            continue
        except PermissionError:
            if _gone_or_zombie(pid):  # became a zombie or exited since the check above
                continue
            if _managed_process(pid, proc):
                raise
            # Nondumpable daemons outside Herdr cannot be managed build jobs.
            continue
    return holds


def _proc_claims(pid: Path, number: int, name: str) -> list[Hold]:
    """Target directories a cargo-family process builds into: ``CARGO_TARGET_DIR`` and ``--target-dir``."""
    cwd = os.readlink(pid / "cwd")
    environ = (pid / "environ").read_bytes().split(b"\0")
    argv = [os.fsdecode(a) for a in (pid / "cmdline").read_bytes().split(b"\0")]
    pairs = [os.fsdecode(e).split("=", 1) for e in environ if b"=" in e]
    values = [value for key, value in pairs if key in _TARGET_ENV]
    return [(number, name, "builds into", Path(os.path.join(cwd, v)), _NO_IDS) for v in values + _argv_target_dirs(argv)]


def _discover(root: Path, managed: bool = True, unreadable: list[str] | None = None,
              trash: list[Path] | None = None) -> list[Path]:
    """Cargo targets under ``root``. ``managed`` also returns cache-only targets and Herdr-managed
    ``targets/<workspace>`` dirs (what the sweep reclaims); migration wants repository targets only.
    A subdirectory that cannot be read is skipped (and named in ``unreadable``), never the whole root.
    Leftovers of an interrupted orphan removal (``targets/.trash-*``) are listed in ``trash``, never as targets."""
    pending = [(root, 0)]
    device = root.stat().st_dev
    found = []
    count = 0
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
                count += 1
                if count > _MAX_ENTRIES:
                    raise OSError(f"Discovery directory limit reached under {root}")
                path = Path(entry.path)
                if entry.stat(follow_symlinks=False).st_dev != device:
                    continue
                if managed_dir and entry.name.startswith(".trash-"):
                    if trash is not None:
                        trash.append(path)
                    continue
                if managed_dir and entry.name == ".owners":
                    continue  # the manager's ownership records, not a target
                if managed_dir and _managed_workspace(path) is not None:
                    if _cargo_markers(path):
                        found.append(path)
                    continue
                if entry.name == "target":
                    if _cargo_markers(path) and ((directory / "Cargo.toml").is_file()
                                                 or (managed and _cache_only_target(path))):
                        found.append(path)
                    continue
                if entry.name in _PRUNE:
                    continue
                if depth >= _MAX_DEPTH:
                    continue  # deeper directories are outside this bounded sweep
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

    This stats files of arbitrary processes; on a dead network mount that can block, so the sweep runs it
    in a child process with a deadline (``_busy_check``)."""
    mine = {ident for ident in (_identity(target), _identity(_project(target))) if ident}
    inside = mine | _inventory(target)
    target_id = _identity(target)
    target_chain = _chain(target)
    for pid, name, how, path, ids in holds:
        if how == "uses":
            if (ids | _chain(path)) & inside:
                return f"{name} (pid {pid}) uses {path}"
        elif (target_id and target_id in _chain(path)) or (_identity(path) in target_chain):
            return f"{name} (pid {pid}) builds into {path}"
    return None


_BUSY_CHILD = "import sys; from herdr_engineering import build_cleanup as c; c._busy_child(sys.argv[1])"
_BUSY_SECONDS = 120.0  # the scan of a normal host takes a second; ``HERDR_BUSY_CHECK_SECONDS`` overrides


def _busy_child(target: str) -> None:
    try:
        print(json.dumps({"busy": _busy_reason(_scan(), Path(target))}))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))


def _busy_check(target: Path) -> str | None:
    """``_busy_reason`` over a fresh process scan, run in a child with a deadline so that a stat that hangs
    (a dead NFS or sshfs mount a process sits on) can never hang the sweep or a build's disk guard. A child
    that does not answer in time is killed and left (it may be unkillable); the caller fails closed."""
    seconds = float(os.environ.get("HERDR_BUSY_CHECK_SECONDS") or _BUSY_SECONDS)
    root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")]))}
    child = subprocess.Popen([sys.executable, "-c", _BUSY_CHILD, str(target)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
    try:
        out, _ = child.communicate(timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        child.kill()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass  # stuck in the kernel: abandoned, never waited for
        raise TimeoutError(f"busy check timed out after {seconds:g} s") from exc
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
                    identity = (json.dumps(read_owner(target.parent, target.name), sort_keys=True) + "\n").encode()
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


def _inspect(target: Path) -> tuple[float, int]:
    """Return newest mtime and allocated size, rejecting links and nested sources."""
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
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return newest, size


def _untracked(target: Path) -> bool:
    """Git must verify the entire target is untracked, including nested files."""
    if _managed_workspace(target) is not None:
        return True  # lives in the state directory, not in a repository
    cache_only = not (target.parent / "Cargo.toml").is_file()
    if cache_only and subprocess.run(
            ["git", "-C", str(target.parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, timeout=30, check=False).returncode != 0:
        return True  # cache-only directory (verified by CACHEDIR.TAG) outside any repository
    result = subprocess.run(
        ["git", "-C", str(target.parent), "ls-files", "--", target.name],
        capture_output=True, timeout=30, check=False,
    )
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


def _write_status(state_dir: Path, result: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".build-cleanup-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        os.replace(name, state_dir / "build-cleanup.json")
    finally:
        Path(name).unlink(missing_ok=True)


def run_once(
    roots: list[str | Path], state_dir: str | Path, dry_run: bool = False,
    high_water: float = 85, low_water: float = 80, min_age_hours: float = 24,
    pressure_water: float = 90, pressure_min_age_hours: float = 2,
    only_device: int | None = None,
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
    """
    if not all(math.isfinite(v) for v in (high_water, low_water, min_age_hours,
                                          pressure_water, pressure_min_age_hours)):
        raise ValueError("Cleanup policy values must be finite")
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
        "reconciled": [], "pressure": False, "process_scans": 0, "aborted": False, "blocked": [],
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
        _sweep(roots, state_dir, result, high_water, low_water, min_age_hours,
               pressure_water, pressure_min_age_hours, only_device)
        if result["warnings"]:
            result["status"] = "warning"
        if result["errors"]:
            result["status"] = "error"
        _write_status(state_dir, result)
        if not dry_run:
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


def _candidates(roots, state_dir, result, high_water, min_age_hours, pressure_water, pressure_min_age_hours,
                only_device):
    """Targets on the roots that are over ``high_water``: (newest mtime, path, size, idle cutoff) each, and
    the root they live on by device. Leftovers of an interrupted orphan removal are removed here."""
    candidates, roots_by_device = [], {}
    now = time.time()
    for raw in roots:
        root = Path(raw).expanduser().absolute()
        try:
            canonical = root == root.resolve(strict=True) and root.is_dir() and root != Path("/")
        except OSError:
            canonical = False
        if not canonical:
            _skip_root(result, root, "not a canonical project directory")
            continue
        if only_device is not None and root.stat().st_dev != only_device:
            continue  # deleting there cannot relieve the filesystem under pressure
        result["roots"].append(str(root))
        usage = _usage(root)
        if usage < high_water:
            continue
        under_pressure = usage >= pressure_water
        cutoff = now - (min(min_age_hours, pressure_min_age_hours) if under_pressure else min_age_hours) * 3600
        result["pressure"] = result["pressure"] or under_pressure
        roots_by_device[root.stat().st_dev] = root
        try:
            unreadable: list[str] = []
            trash: list[Path] = []
            targets = _discover(root, unreadable=unreadable, trash=trash)
            for leftover in trash if not result["dry_run"] else []:
                try:
                    shutil.rmtree(leftover)  # nothing can be using it: it was renamed out of reach
                except OSError as exc:  # one stuck leftover skips itself; the rest of the root still sweeps
                    result["skipped"].append({"path": str(leftover), "reason": f"cannot delete: {exc}"})
            for name in unreadable[:5]:
                _skip_root(result, Path(name), "unreadable directory skipped")
        except OSError as exc:
            _skip_root(result, root, str(exc))
            continue
        for target in targets:
            if state_dir.is_relative_to(target):
                raise OSError(f"Maintenance state is inside cleanup target: {target}")
            if target.stat().st_dev != root.stat().st_dev:
                result["skipped"].append({"path": str(target), "reason": "different filesystem"})
                continue
            try:
                newest, size = _inspect(target)
            except OSError as exc:
                result["skipped"].append({"path": str(target), "reason": str(exc)})
                result["errors"].append(str(exc))
                continue
            if newest > cutoff and not _orphaned(target):
                result["skipped"].append({"path": str(target), "reason": "recent activity"})
            else:
                candidates.append((newest, str(target), size, cutoff))
    return candidates, roots_by_device


def _sweep(roots, state_dir, result, high_water, low_water, min_age_hours,
           pressure_water=90, pressure_min_age_hours=2, only_device=None):
    try:
        candidates, roots_by_device = _candidates(
            roots, state_dir, result, high_water, min_age_hours, pressure_water, pressure_min_age_hours,
            only_device)
    except (OSError, ValueError) as exc:
        result["errors"].append(str(exc))
        result["aborted"] = True  # the sweep itself failed (discovery): nothing was judged
        return
    seen = set()
    for _, name, size, cutoff in sorted(candidates):
        if result["aborted"]:
            break  # a failed busy check stops the sweep: nothing more is judged on a host we cannot read
        if name in seen:
            continue
        seen.add(name)
        target = Path(name)
        try:
            root = roots_by_device[target.stat().st_dev]
            if _usage(root) <= low_water:
                continue
            before = shutil.disk_usage(root).free
            if _orphaned(target):
                if _blocked_by_process(target, name, result):
                    continue  # moved, not deleted: something still builds into it
                if result["dry_run"]:
                    result["would_remove"].append(name)
                    continue
                if not _remove_orphan(target):
                    result["skipped"].append({"path": name, "reason": "worktree is back"})
                    continue
            elif not _clean_idle(target, name, cutoff, state_dir, result):
                continue
        except (OSError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"{name}: {exc}")
            break  # unknown state is a reason to stop, never to broaden deletion
        result["removed_count"] += 1
        result["removed_bytes"] += size
        result["reclaimed_bytes"] += max(0, shutil.disk_usage(root).free - before)
        result["removed"].append(name)


def _blocked_by_process(target: Path, name: str, result: dict) -> bool:
    """A fresh process scan right before deleting ``target`` (idle or orphan): True when it must stay, with
    the reason in ``skipped`` and ``blocked``. A scan that cannot be read fails closed."""
    result["process_scans"] += 1
    busy = None
    for attempt in range(6):  # a process mid-exec is unreadable for a moment
        try:
            busy = _busy_check(target)
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


def _clean_idle(target: Path, name: str, cutoff: float, state_dir: Path, result: dict) -> bool:
    """Clean one idle target through cargo; True when it went. Otherwise the reason is in ``skipped``."""
    def skip(reason: str) -> bool:
        result["skipped"].append({"path": name, "reason": reason})
        return False

    if not _untracked(target):
        return skip("tracked or unverifiable output")
    newest, _ = _inspect(target)  # revalidate right before deleting
    if newest > cutoff:
        return skip("recent activity")
    if not _recognized(target):
        raise OSError(f"Target no longer recognized as a Cargo output: {name}")
    if _blocked_by_process(target, name, result):
        return False
    if _skip_for_registry(state_dir, target, name, result):  # read last: a lease taken during the scan counts
        return False
    if result["dry_run"]:
        result["would_remove"].append(name)
        return False
    reason = _cargo_clean(target)
    return skip(reason) if reason else True
