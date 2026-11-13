"""Bounded, ownership-checked retention of idle Cargo profile output."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from .cargo_target_migration import process_paths
from .cargo_target_storage import TARGET_MARKER, _owned_json, target_directory

LOCK_NAMES = {".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock"}
GIB = 1024 ** 3


class Status(StrEnum):
    OK = "ok"
    BUSY = "busy"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass
class TierReport:
    status: Status = Status.OK
    tier: str = ""
    root: str = ""
    used_bytes: int = 0
    budget_bytes: int = 0
    reclaimed_bytes: int = 0
    would_reclaim_bytes: int = 0
    growth_bytes: int | None = None
    over_budget: bool = False
    profile_count: int = 0
    skipped: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)

    def skip(self, value: str) -> None:
        if len(self.skipped) < 20:
            self.skipped.append(value[:300])


@dataclass
class ScanLimit:
    remaining: int
    deadline: float

    def visit(self) -> None:
        self.remaining -= 1
        if self.remaining < 0 or time.monotonic() > self.deadline:
            raise TimeoutError("inventory limit reached; further cleanup skipped")


def _children(path: Path, limit: ScanLimit) -> list[Path]:
    result = []
    with os.scandir(path) as entries:
        for entry in entries:
            limit.visit()
            result.append(Path(entry.path))
    return result


def _scan(root: Path, limit: ScanLimit) -> tuple[int, float]:
    pending, seen, size, newest = [root], set(), 0, 0.0
    device = root.stat().st_dev
    canonical = root.resolve()
    while pending:
        path = pending.pop()
        limit.visit()
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_dev != device:
            raise ValueError("foreign ownership or mount boundary")
        if path.name in {"Cargo.toml", ".git"}:
            raise ValueError("source marker")
        newest = max(newest, info.st_mtime)
        if stat.S_ISLNK(info.st_mode):
            if not path.resolve().is_relative_to(canonical):
                raise ValueError("escaping symlink")
        elif stat.S_ISDIR(info.st_mode):
            pending.extend(_children(path, limit))
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError("special file")
        identity = (info.st_dev, info.st_ino)
        if identity not in seen:
            seen.add(identity)
            size += info.st_blocks * 512
    return size, newest


def _owned_target(cache: Path, target: Path) -> tuple[float, Path]:
    marker = target / TARGET_MARKER
    metadata = _owned_json(marker)
    if (target.is_symlink() or not metadata or metadata.get("owner_uid") != os.getuid()
            or metadata.get("version") != 1 or not isinstance(metadata.get("workspace"), str)):
        raise ValueError("unrecognized target ownership")
    workspace = Path(str(metadata["workspace"]))
    if not workspace.is_absolute():
        raise ValueError("workspace must be absolute")
    expected = target_directory(cache, workspace).name
    if target.name != expected and not re.fullmatch(re.escape(expected) + r"-legacy-[0-9a-f]{12}", target.name):
        raise ValueError("target identity mismatch")
    return marker.stat().st_mtime, workspace


def _profiles(target: Path, limit: ScanLimit) -> list[Path]:
    # Cargo profiles are target/<profile> or target/<triple>/<profile>.
    profiles = []
    for first in _children(target, limit):
        if not first.is_dir() or first.is_symlink():
            continue
        children = [first, *_children(first, limit)]
        for child in children:
            lock = child / ".cargo-lock"
            if (child.is_dir() and not child.is_symlink()
                    and lock.is_file() and not lock.is_symlink()):
                profiles.append(child)
    return sorted(set(profiles))


def _remove_at(parent_fd: int, name: str, device: int, limit: ScanLimit) -> None:
    """Bounded fd-relative removal: never follow replacement links or mounts."""
    limit.visit()
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if info.st_uid != os.getuid() or info.st_dev != device or name in {"Cargo.toml", ".git"}:
        raise ValueError("changed ownership, source marker or mount boundary")
    if stat.S_ISDIR(info.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            if os.fstat(fd).st_ino != info.st_ino:
                raise ValueError("directory changed during cleanup")
            with os.scandir(fd) as entries:
                for entry in entries:
                    _remove_at(fd, entry.name, device, limit)
            os.rmdir(name, dir_fd=parent_fd)
        finally:
            os.close(fd)
    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise ValueError("special file appeared during cleanup")


def _clean_profile(profile: Path, use_time: float, cutoff: float, limit: ScanLimit,
                   apply: bool) -> int:
    profile_fd = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(".cargo-lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=profile_fd)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            size, newest = _scan(profile, limit)
            if profile.stat().st_ino != os.fstat(profile_fd).st_ino:
                raise ValueError("profile changed during inspection")
            if max(use_time, newest) > cutoff:
                raise ValueError("recent output")
            if apply:
                with os.scandir(profile_fd) as entries:
                    for entry in entries:
                        if entry.name not in LOCK_NAMES:
                            _remove_at(profile_fd, entry.name, os.fstat(profile_fd).st_dev, limit)
                after, _newest = _scan(profile, limit)
                return max(0, size - after)
            return size
        finally:
            os.close(descriptor)
    finally:
        os.close(profile_fd)


def sweep(cache: Path, state: Path, *, budget_bytes: int, min_idle_hours: float = 168,
          apply: bool = False, max_entries: int = 1_000_000, max_seconds: float = 20) -> TierReport:
    """Try once without interrupting builds; reclaim only recognized profile contents."""
    report = TierReport(root=str(cache.parent / "targets"), budget_bytes=budget_bytes)
    if (budget_bytes <= 0 or min_idle_hours < 24 or max_seconds <= 0
            or not all(math.isfinite(value) for value in (min_idle_hours, max_seconds))):
        raise ValueError("Require positive budget/time and at least 24 idle hours")
    root = cache.parent / "targets"
    if not cache.is_dir() or not root.is_dir() or root.is_symlink():
        report.status = Status.UNAVAILABLE
        return report
    metadata = _owned_json(root / ".herdr-target-root.json")
    if (metadata != {"version": 1, "owner_uid": os.getuid()} or root.stat().st_uid != os.getuid()
            or root.stat().st_dev != cache.stat().st_dev):
        report.status = Status.ERROR
        report.skip("target root ownership mismatch")
        return report
    state.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(state / "build-maintenance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report.status = Status.BUSY
            return report
        limit = ScanLimit(max_entries, time.monotonic() + max_seconds)
        candidates: list[tuple[float, Path, float, Path]] = []
        # Complete inventory before any deletion: partial scans never evict.
        for target in sorted(_children(root, limit)):
            if target.name.startswith("."):
                continue
            try:
                if target.stat().st_dev != root.stat().st_dev:
                    raise ValueError("target mount boundary")
                use_time, workspace = _owned_target(cache, target)
                size, newest = _scan(target, limit)
                report.used_bytes += size
                profiles = _profiles(target, limit)
                report.profile_count += len(profiles)
                for profile in profiles:
                    candidates.append((max(use_time, newest), profile, use_time, workspace))
            except (OSError, ValueError) as error:
                # Unknown output makes the total uncertain: report, never evict.
                report.skip(f"{target.name}: {error}")
                report.status = Status.PARTIAL
        report.over_budget = report.used_bytes > budget_bytes
        if report.status != Status.OK or not report.over_budget:
            return report
        paths, compilers = process_paths()
        cutoff = time.time() - min_idle_hours * 3600
        remaining = report.used_bytes
        for newest, profile, use_time, workspace in sorted(candidates):
            if remaining <= budget_bytes:
                break
            if newest > cutoff:
                report.skip(f"{profile}: recent output")
                continue
            if (any(path == profile or path.is_relative_to(profile) for path in paths)
                    or any(path == workspace or path.is_relative_to(workspace) for path in compilers)):
                report.skip(f"{profile}: open files or active compiler")
                continue
            try:
                size = _clean_profile(profile, use_time, cutoff, limit, apply)
                remaining -= size
                if apply:
                    report.reclaimed_bytes += size
                    if len(report.cleaned) < 20:
                        report.cleaned.append(str(profile))
                else:
                    report.would_reclaim_bytes += size
            except (OSError, ValueError) as error:
                report.skip(f"{profile}: {error}")
        report.used_bytes = max(0, report.used_bytes - report.reclaimed_bytes)
        report.over_budget = report.used_bytes > budget_bytes
    except TimeoutError as error:
        report.status = Status.PARTIAL
        report.skip(str(error))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report.status = Status.ERROR
        report.skip(str(error))
    finally:
        os.close(descriptor)
    return report


def maintain_targets(*, apply: bool = False) -> dict[str, object]:
    from .build_cache_location import StorageTier, select_storage
    from .build_coord import _default_state_dir
    from .cargo_host import _atomic_write
    from .cli import _build_cache_config
    config = _build_cache_config()
    if config is None or not config.local.manage_targets:
        raise ValueError("Enable managed Cargo targets before retention")
    if apply and not config.local.target_retention_enabled:
        raise ValueError("Enable target retention before applying cleanup")
    state = _default_state_dir()
    status_file = state / "target-retention.json"
    previous = read_report(state) or {}
    old_tiers = previous.get("tiers", [])
    old_sizes = {item.get("root"): item.get("used_bytes") for item in old_tiers
                 if isinstance(item, dict) and item.get("status") == Status.OK} if isinstance(old_tiers, list) else {}
    reports = []
    tiers = config.local.storage_tiers or (StorageTier("internal", str(state / "sccache")),)
    if len(tiers) > 32:
        raise ValueError("Target retention supports at most 32 storage tiers")
    for tier in tiers:
        selected = select_storage((tier,), state / "sccache", 0)
        budget = int((config.local.target_external_gib if tier.volume_uuid else config.local.target_internal_gib) * GIB)
        report = (sweep(selected.directory, state, budget_bytes=budget,
                        min_idle_hours=config.local.target_min_idle_hours, apply=apply)
                  if selected.directory else TierReport(status=Status.UNAVAILABLE, budget_bytes=budget))
        report.tier = tier.name
        old = old_sizes.get(report.root)
        if isinstance(old, int) and report.status == Status.OK:
            report.growth_bytes = report.used_bytes - old
        reports.append(asdict(report))
    result: dict[str, object] = {"timestamp": time.time(), "apply": apply, "tiers": reports,
                                 "reclaimed_bytes_note": "estimated allocated bytes removed; hardlinks/APFS sharing affect physical free space"}
    encoded = json.dumps(result, sort_keys=True)
    if len(encoded.encode()) > 256 * 1024:
        for tier_report in reports:
            tier_report["skipped"] = ["Details truncated to bound report size"]
            tier_report["cleaned"] = []
        encoded = json.dumps(result, sort_keys=True)
    _atomic_write(status_file, encoded + "\n")
    return result


def read_report(state: Path) -> dict[str, object] | None:
    path = state / "target-retention.json"
    try:
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def retention_report() -> str:
    from .build_coord import _default_state_dir
    data = read_report(_default_state_dir())
    if not data:
        return "No target-retention report yet; run cargo-host targets-maintain."
    stamp = data.get("timestamp", 0)
    if not isinstance(stamp, (int, float)):
        return "Invalid target-retention report timestamp."
    lines = [f"Cargo target storage (last scan: {time.strftime('%Y-%m-%d %H:%M', time.localtime(stamp))})"]
    tiers = data.get("tiers", [])
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            used, budget = tier.get("used_bytes", 0), tier.get("budget_bytes", 0)
            if isinstance(used, (int, float)) and isinstance(budget, (int, float)):
                if tier.get("status") != Status.OK:
                    lines.append(f"{tier.get('tier')}: usage unavailable, {budget / GIB:.0f} GiB budget, {tier.get('status')}")
                    continue
                growth = tier.get("growth_bytes")
                delta = f", growth {growth / GIB:+.2f} GiB" if isinstance(growth, (int, float)) else ""
                lines.append(f"{tier.get('tier')}: {used / GIB:.2f}/{budget / GIB:.0f} GiB, {tier.get('status')}{delta}; reclaimed {tier.get('reclaimed_bytes', 0)} bytes")
    return "\n".join(lines)


def install_job(home: Path, executable: Path, *, config_path: Path | None = None,
                activate: bool = True) -> Path:
    """Publish only our dedicated user job; stdout/stderr never accumulate logs."""
    import plistlib

    from .build_coord import _default_state_dir
    from .cargo_host import _atomic_write
    label = "com.herdr.cargo-target-retention"
    job = home / "Library/LaunchAgents" / (label + ".plist")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Retention executable is unavailable")
    payload = plistlib.dumps({"Label": label, "StartInterval": 3600, "RunAtLoad": True,
                             "ProgramArguments": [str(executable), "cargo-host", "targets-maintain", "--apply"],
                             "EnvironmentVariables": {
                                 "HERDR_ENGINEERING_CONFIG": str((config_path or home / ".config/herdr-engineering/config.yaml").resolve()),
                                 "HERDR_ENGINEERING_STATE": str(_default_state_dir().parent.resolve()),
                             },
                             "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null",
                             "ProcessType": "Background", "Nice": 10}).decode()
    if job.is_symlink() or (job.exists() and job.read_text() != payload):
        raise ValueError("Existing retention job is not the managed definition; preserved")
    if not job.exists():
        _atomic_write(job, payload)
    if activate:
        service = f"gui/{os.getuid()}/{label}"
        loaded = subprocess.run(["/bin/launchctl", "print", service], capture_output=True, timeout=5)
        if loaded.returncode:
            subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(job)],
                           capture_output=True, timeout=10, check=True)
    return job
