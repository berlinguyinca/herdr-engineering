"""Conservative, pressure-driven cleanup of idle Cargo output directories.

Sweeps exclude each other with ``build-cleanup.lock`` (exclusive) and take
``build-maintenance.lock`` shared, which only excludes the destructive maintenance jobs
(retention, migration, cache eviction) and never a running build, so the disk guard can sweep
from inside ``build-coordinate``. Running builds are protected per target: recent activity,
build leases, open files / working directories of visible processes, and the managed target's
build lock (``targets/<name>.lock``): a build holds it shared from before cargo starts until it
ends, and the sweep takes it exclusively (non-blocking) right before deleting and keeps it until
the delete is done, so a build never starts into a directory that is being removed.
Inaccessible Herdr descendants or unknown ancestry always preserve files. Only current-user Cargo ``target``
directories inside explicit roots are eligible; no symlinks are followed.

When ``build-registry.db`` exists in the state directory (spec 05), a target whose
project directory is related (equal, inside or containing, by path prefix) to the
scope of an unexpired build lease is preserved (``active_lease``). The lease is
re-read right before deletion. A registry that exists but cannot be read makes the
whole pass fail closed (``registry_unreadable``). Leases only ever ADD protection:
the flock and /proc defences above are unchanged, and no registry file means
exactly the legacy behaviour.
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
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .cargo_target_storage import TARGET_MARKER, TOMBSTONE, _manager_lock, tombstone_path
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
    """
    seen = set()
    current = pid
    while current.name != "0":
        if current.name in seen or len(seen) >= 256:
            raise OSError(f"Unverifiable process ancestry at {pid}")
        seen.add(current.name)
        try:
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
        except FileNotFoundError as exc:
            raise OSError(f"Process ancestry changed while inspecting {pid}") from exc
        if len(parents) != 1 or not parents[0].isdigit():
            raise OSError(f"Unverifiable process parent at {current}")
        current = proc / parents[0]
    return False


def _lsof_paths() -> list[Path]:
    """Open files, cwds and executables of this user's processes where /proc is absent (macOS).

    Fails closed: if lsof cannot run, cleanup must not guess that nothing is in use.
    """
    try:
        result = subprocess.run(["lsof", "-w", "-n", "-P", "-u", str(os.getuid()), "-Fpn"],
                                capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"Cannot inspect running processes: {exc}") from exc
    if result.returncode > 1 or (result.returncode == 1 and not result.stdout):
        raise OSError("Cannot inspect running processes: lsof failed")
    paths, own = [], False
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            own = line[1:] == str(os.getpid())  # like the /proc scan, ignore this process
        elif line.startswith("n/") and not own:
            paths.append(Path(line[1:]))
    return paths


def _process_paths(proc: Path = Path("/proc")) -> list[Path]:
    """Inspect visible same-user processes; hidden Herdr descendants block cleanup."""
    if not proc.is_dir():
        return _lsof_paths()
    paths = []
    with os.scandir(proc) as entries:
        pids = [Path(entry.path) for entry in entries if entry.name.isdigit()]
    for pid in pids:
        try:
            if int(pid.name) == os.getpid() or pid.stat().st_uid != os.getuid():
                continue
            links = [pid / "cwd", pid / "exe"]
            with os.scandir(pid / "fd") as entries:
                links.extend(Path(entry.path) for entry in entries)
            for link in links:
                try:
                    value = os.readlink(link)
                except FileNotFoundError:  # descriptor closed / process exited
                    continue
                if value.startswith("/"):
                    paths.append(Path(value.removesuffix(" (deleted)")))
        except FileNotFoundError:  # process exited during inspection
            continue
        except PermissionError:
            if _managed_process(pid, proc):
                raise
            # Nondumpable daemons outside Herdr cannot be managed build jobs.
            continue
    return paths


def _discover(root: Path, managed: bool = True, unreadable: list[str] | None = None,
              tombstones: list[Path] | None = None) -> list[Path]:
    """Cargo targets under ``root``. ``managed`` also returns cache-only targets and Herdr-managed
    ``targets/<workspace>`` dirs (what the sweep reclaims); migration wants repository targets only.
    A subdirectory that cannot be read is skipped (and named in ``unreadable``), never the whole root."""
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
                if managed_dir and TOMBSTONE.fullmatch(entry.name):
                    if tombstones is not None:
                        tombstones.append(path)  # a delete a sweep started and did not finish
                    continue
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
    root marker and the target's own marker."""
    try:
        root_marker, marker = path.parent / ".herdr-target-root.json", path / TARGET_MARKER
        if root_marker.is_symlink() or marker.is_symlink() or not root_marker.is_file() or not marker.is_file():
            return None
        if json.loads(root_marker.read_text()).get("owner_uid") != os.getuid():
            return None
        data = json.loads(marker.read_text())
        if data.get("owner_uid") != os.getuid() or not isinstance(data.get("workspace"), str):
            return None
        return Path(data["workspace"])
    except (OSError, ValueError, AttributeError):
        return None


def _project(target: Path) -> Path:
    """Directory whose activity keeps a target alive: its manifest directory, or the owning workspace."""
    return _managed_workspace(target) or target.parent


_BUSY = -1
_SNAPSHOT_REUSE_SECONDS = 5.0
_CARGO_LOCK_NAMES = frozenset({".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock"})
_NOT_PROFILES = frozenset({"deps", "build", "incremental", ".fingerprint", "examples"})


def _cargo_lock_files(target: Path) -> list[str]:
    """Cargo's own lock files under ``target``: ``<profile>/`` and ``<triple>/<profile>/`` (cargo 1.75 has
    ``.cargo-lock``; newer cargo adds ``.cargo-build-lock`` and ``.cargo-artifact-lock``)."""
    found: list[str] = []
    pending = [(target, 0)]
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name in _CARGO_LOCK_NAMES and entry.is_file(follow_symlinks=False):
                    found.append(entry.path)
                elif depth < 2 and entry.name not in _NOT_PROFILES and entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), depth + 1))
    return found


def _lock_for_removal(target: Path) -> list[int] | int:
    """Exclusive, non-blocking locks on every Cargo lock file of ``target`` (_BUSY when one is held).

    Cargo flocks these for every build, whether it runs through the wrapper, an IDE or a script, so a
    build in progress is detected without any lock of ours. The descriptors are returned open and must
    be held through the delete; a target that was never built has none. An OSError (a lock file that
    cannot be opened) propagates, and the caller skips this target only.
    """
    fds: list[int] = []
    try:
        for path in _cargo_lock_files(target):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            fds.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _release_removal_lock(fds)
                return _BUSY
    except BaseException:
        _release_removal_lock(fds)
        raise
    return fds


def _release_removal_lock(fds: list[int]) -> None:
    for fd in fds:
        os.close(fd)


def _remove_target(target: Path) -> None:
    """Delete ``target``. A managed target keeps its directory and ownership marker: under the manager lock
    its contents move into a tombstone beside it, so ``./target`` never dangles and a cargo that was waiting
    on the locks resumes into a valid, empty managed target. An interrupted delete leaves the tombstone,
    which the next sweep or retention run collects."""
    if _managed_workspace(target) is not None:
        grave = tombstone_path(target)
        with _manager_lock(target.parent):
            grave.mkdir(mode=0o700)
            for entry in os.listdir(target):
                if entry != TARGET_MARKER:
                    os.rename(target / entry, grave / entry)
        target = grave
    # CPython's fd-based rmtree prevents symlink substitution attacks.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError("This platform lacks symlink-safe recursive removal")
    shutil.rmtree(target)


def _is_emptied(target: Path) -> bool:
    """A managed target that holds nothing but its ownership marker."""
    try:
        return os.listdir(target) == [TARGET_MARKER] and _managed_workspace(target) is not None
    except OSError:
        return False


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


def _in_use(target: Path, active: list[Path]) -> bool:
    project = _project(target)
    return any(path.is_relative_to(project) or path.is_relative_to(target) for path in active)


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
    ``MIN_IDLE_HOURS``). Process, lease and build-lock guards apply in both tiers. Dry runs fully
    validate eligible targets (a target a running build holds is not listed) but never remove them. Busy sweeps return immediately without overwriting the last report.
    ``only_device`` limits the sweep to roots on that filesystem (the one under pressure).
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
        "reconciled": [], "pressure": False, "process_scans": 0, "tombstones_removed": 0, "aborted": False,
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


def _sweep(roots, state_dir, result, high_water, low_water, min_age_hours,
           pressure_water=90, pressure_min_age_hours=2, only_device=None):
    roots_by_device = {}
    candidates = []
    cutoffs: dict[str, float] = {}
    now = time.time()
    try:
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
            cutoff = now - (min(min_age_hours, pressure_min_age_hours) if under_pressure
                            else min_age_hours) * 3600
            if under_pressure:
                result["pressure"] = True
            roots_by_device[root.stat().st_dev] = root
            try:
                unreadable: list[str] = []
                graves: list[Path] = []
                targets = _discover(root, unreadable=unreadable, tombstones=graves)
                for name in unreadable[:5]:
                    _skip_root(result, Path(name), "unreadable directory skipped")
            except OSError as exc:
                _skip_root(result, root, str(exc))
                continue
            for grave in graves if not result["dry_run"] else []:
                try:
                    shutil.rmtree(grave)
                    result["tombstones_removed"] += 1
                except OSError as exc:
                    _skip_root(result, grave, f"tombstone not removed: {exc}")
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
                if newest > cutoff:
                    result["skipped"].append({"path": str(target), "reason": "recent activity"})
                elif _is_emptied(target):
                    continue  # a managed target a sweep already emptied: nothing to reclaim
                else:
                    candidates.append((newest, str(target), size))
                    cutoffs[str(target)] = cutoff
        if not candidates:
            return
        # Verify process visibility before deleting anything.
        result["process_scans"] += 1
        active = _process_paths()
        scanned_at = time.monotonic()
    except (OSError, ValueError) as exc:
        result["errors"].append(str(exc))
        result["aborted"] = True  # the sweep itself failed (process scan, discovery): nothing was judged
        return
    seen = set()
    for _, name, _ in sorted(candidates):
        if name in seen:
            continue
        seen.add(name)
        target = Path(name)
        try:
            root = roots_by_device[target.stat().st_dev]
            if _usage(root) <= low_water:
                continue
            if _in_use(target, active):
                result["skipped"].append({"path": name, "reason": "active project"})
                continue
            if _skip_for_registry(state_dir, target, name, result):
                continue
            if not _untracked(target):
                result["skipped"].append({"path": name, "reason": "tracked or unverifiable output"})
                continue
            # Revalidate right before deletion; Cargo's own locks are held from here on.
            newest, size = _inspect(target)
            if newest > cutoffs[name]:
                result["skipped"].append({"path": name, "reason": "recent activity"})
                continue
            if not _recognized(target):
                raise OSError(f"Target no longer recognized as a Cargo output: {name}")
            try:
                lock_fds = _lock_for_removal(target)  # held through the delete
            except OSError as exc:  # one target's lock trouble never ends the sweep
                result["skipped"].append({"path": name, "reason": f"cannot lock: {exc}"})
                continue
            if lock_fds == _BUSY:
                result["skipped"].append({"path": name, "reason": "build in progress"})
                continue
            try:
                if result["dry_run"]:  # nothing is removed, so the first scan stands
                    if not _skip_for_registry(state_dir, target, name, result):
                        result["would_remove"].append(name)
                    continue
                # Re-read the lease and the ownership proof under the locks, right before removing.
                if _skip_for_registry(state_dir, target, name, result):
                    continue
                if not _recognized(target):
                    raise OSError(f"Target no longer recognized as a Cargo output: {name}")
                # Look at the processes once more right before removing. Cargo releases its locks while a
                # test or run binary executes out of the target, so the locks alone are not enough. A
                # snapshot younger than a few seconds is as good as a new one.
                if time.monotonic() - scanned_at >= _SNAPSHOT_REUSE_SECONDS:
                    result["process_scans"] += 1
                    active, scanned_at = _process_paths(), time.monotonic()
                if _in_use(target, active):
                    result["skipped"].append({"path": name, "reason": "active project"})
                    continue
                before = shutil.disk_usage(root).free
                _remove_target(target)
            finally:
                _release_removal_lock(lock_fds)
            after = shutil.disk_usage(root).free
            result["removed_count"] += 1
            result["removed_bytes"] += size
            result["reclaimed_bytes"] += max(0, after - before)
            result["removed"].append(name)
        except (OSError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"{name}: {exc}")
            break  # unknown state is a reason to stop, never to broaden deletion
