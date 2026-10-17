"""Conservative, pressure-driven cleanup of idle Cargo output directories.

Build-coordinate commands share ``build-maintenance.lock`` with this module. Cleanup holds
it exclusively for discovery, validation and removal. Linux process inspection
also protects workspaces used by visible commands outside the build gate.
Inaccessible Herdr descendants or unknown ancestry always preserve files. Only current-user Cargo ``target``
directories inside explicit roots are eligible; no symlinks are followed.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

_PRUNE = {".git", "node_modules", ".venv", "venv", ".cache", "__pycache__"}
_MAX_ENTRIES = 500_000
_MAX_DEPTH = 6



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


def _process_paths(proc: Path = Path("/proc")) -> list[Path]:
    """Inspect visible same-user processes; hidden Herdr descendants block cleanup."""
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


def _discover(root: Path) -> list[Path]:
    pending = [(root, 0)]
    device = root.stat().st_dev
    found = []
    count = 0
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                count += 1
                if count > _MAX_ENTRIES:
                    raise OSError(f"Discovery directory limit reached under {root}")
                path = Path(entry.path)
                if entry.stat(follow_symlinks=False).st_dev != device:
                    continue
                if entry.name == "target":
                    markers = [path / ".rustc_info.json", path / "debug/.fingerprint",
                               path / "release/.fingerprint"]
                    if (directory / "Cargo.toml").is_file() and any(p.exists() for p in markers):
                        found.append(path)
                    continue
                if entry.name in _PRUNE:
                    continue
                if depth >= _MAX_DEPTH:
                    continue  # deeper directories are outside this bounded sweep
                pending.append((path, depth + 1))
    return found


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
    result = subprocess.run(
        ["git", "-C", str(target.parent), "ls-files", "--", "target"],
        capture_output=True, timeout=30, check=False,
    )
    return result.returncode == 0 and not result.stdout


def _usage(path: Path) -> float:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        raise OSError(f"Cannot measure filesystem at {path}")
    # Use available space (rather than used) so reserved blocks count as unavailable.
    return 100 * (usage.total - usage.free) / usage.total


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
) -> dict:
    """Run one bounded sweep and atomically save its inspectable status.

    ``state_dir`` must be the same build-gates directory used by ``run_gated``.
    ``reclaimed_bytes`` measures observed free-space gain; ``removed_bytes`` is
    the allocated size of removed files. Concurrent filesystem activity can
    affect the former. Dry runs fully validate eligible targets but never remove
    them. Busy sweeps return immediately without overwriting the last report.
    """
    if not all(math.isfinite(v) for v in (high_water, low_water, min_age_hours)):
        raise ValueError("Cleanup policy values must be finite")
    if not 0 <= low_water < high_water <= 100 or min_age_hours < 24:
        raise ValueError("Require 0 <= low_water < high_water <= 100 and min_age_hours >= 24")
    state_dir = Path(state_dir).expanduser().absolute()
    state_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": datetime.now(UTC).isoformat(), "status": "ok", "dry_run": dry_run,
        "roots": [], "removed_count": 0, "removed_bytes": 0, "reclaimed_bytes": 0,
        "removed": [], "would_remove": [], "skipped": [], "errors": [],
    }
    lock_fd = os.open(state_dir / "build-maintenance.lock",
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["status"] = "busy"
            return result
        _sweep(roots, state_dir, result, high_water, low_water, min_age_hours)
        if result["errors"]:
            result["status"] = "error"
        _write_status(state_dir, result)
        return result
    finally:
        os.close(lock_fd)


def _sweep(roots, state_dir, result, high_water, low_water, min_age_hours):
    roots_by_device = {}
    candidates = []
    cutoff = time.time() - min_age_hours * 3600
    try:
        for raw in roots:
            root = Path(raw).expanduser().absolute()
            if root != root.resolve(strict=True) or not root.is_dir() or root == Path("/"):
                raise OSError(f"Root must be a canonical project directory: {root}")
            result["roots"].append(str(root))
            if _usage(root) < high_water:
                continue
            roots_by_device[root.stat().st_dev] = root
            for target in _discover(root):
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
                else:
                    candidates.append((newest, str(target), size))
        if not candidates:
            return
        # Verify process visibility before deleting anything.
        active = _process_paths()
    except (OSError, ValueError) as exc:
        result["errors"].append(str(exc))
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
            if any(path.is_relative_to(target.parent) for path in active):
                result["skipped"].append({"path": name, "reason": "active project"})
                continue
            if not _untracked(target):
                result["skipped"].append({"path": name, "reason": "tracked or unverifiable output"})
                continue
            # Revalidate immediately before deletion while still holding the build lock.
            newest, size = _inspect(target)
            if newest > cutoff:
                result["skipped"].append({"path": name, "reason": "recent activity"})
                continue
            active = _process_paths()
            if any(path.is_relative_to(target.parent) for path in active):
                result["skipped"].append({"path": name, "reason": "active project"})
                continue
            if result["dry_run"]:
                result["would_remove"].append(name)
                continue
            before = shutil.disk_usage(root).free
            # CPython's fd-based rmtree prevents symlink substitution attacks.
            if not shutil.rmtree.avoids_symlink_attacks:
                raise OSError("This platform lacks symlink-safe recursive removal")
            shutil.rmtree(target)
            after = shutil.disk_usage(root).free
            result["removed_count"] += 1
            result["removed_bytes"] += size
            result["reclaimed_bytes"] += max(0, after - before)
            result["removed"].append(name)
        except (OSError, subprocess.SubprocessError) as exc:
            result["errors"].append(f"{name}: {exc}")
            break  # unknown state is a reason to stop, never to broaden deletion
