"""Read-only selection of ordered compiler-cache storage locations."""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .disk_guard import measure
from .errors import InvalidError


@dataclass(frozen=True)
class StorageTier:
    name: str
    directory: str
    volume_uuid: str | None = None


@dataclass(frozen=True)
class StorageCandidate:
    name: str
    directory: Path | None
    reason: str | None = None


@dataclass(frozen=True)
class StorageSelection:
    name: str | None
    directory: Path | None
    skipped: tuple[str, ...] = ()


def parse_storage_tiers(value: object) -> tuple[StorageTier, ...]:
    label = "fabric_build_cache.local.storage_tiers"
    if not isinstance(value, list):
        raise InvalidError(f"{label} must be a list")
    result: list[StorageTier] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "directory", "volume_uuid"}:
            raise InvalidError(f"{label} entries accept name, directory and volume_uuid only")
        name, directory, identity = item.get("name"), item.get("directory"), item.get("volume_uuid")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise InvalidError(f"{label}.name must be a short identifier")
        if any(t.name == name for t in result):
            raise InvalidError(f"{label} names must be unique")
        if not isinstance(directory, str) or not directory or "\x00" in directory:
            raise InvalidError(f"{label}.directory must be a non-empty path")
        if identity is not None:
            try:
                identity = str(UUID(identity))
            except (ValueError, TypeError, AttributeError):
                raise InvalidError(f"{label}.volume_uuid must be a volume UUID") from None
            if Path(directory).is_absolute() or ".." in Path(directory).parts:
                raise InvalidError(f"{label} volume directory must be relative without '..'")
        elif not Path(directory).expanduser().is_absolute() or directory.startswith("/Volumes/"):
            raise InvalidError(f"{label} internal directory must be absolute; external tiers require a UUID")
        result.append(StorageTier(name, directory, identity))
    return tuple(result)


def volume_mount_from_plist(raw: bytes, identity: str) -> Path | None:
    """The mount point named by a `diskutil info -plist` document, when it is this identity and writable."""
    try:
        info = plistlib.loads(raw)
    except (ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(info, dict):
        return None
    mount = info.get("MountPoint")
    if (str(info.get("VolumeUUID", "")).lower() != identity.lower()
            or info.get("WritableVolume") is not True or not isinstance(mount, str)):
        return None
    return writable_mount(Path(mount))


def writable_mount(path: Path) -> Path | None:
    """``path`` when it is a mount point the user can write to."""
    return path if path.is_mount() and os.access(path, os.W_OK) else None


def mounted_volume(identity: str) -> Path | None:  # pragma: darwin-only
    """Resolve a mounted, writable macOS volume by identity, including renamed drives."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", identity],
                                capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return None if result.returncode else volume_mount_from_plist(result.stdout, identity)


VolumeResolver = Callable[[str], "Path | None"]


def _candidate(tier: StorageTier, min_free_percent: float,
               resolve_volume: VolumeResolver = mounted_volume) -> StorageCandidate:
    if tier.volume_uuid:
        mount = resolve_volume(tier.volume_uuid)
        if mount is None:
            return StorageCandidate(tier.name, None, "volume_unavailable")
        path = mount / tier.directory
        # Runtime never creates a mount point or a new directory on a removable
        # drive. Enrollment provisions the dedicated directory after checking UUID.
        if not path.is_dir() or not path.resolve().is_relative_to(mount.resolve()):
            return StorageCandidate(tier.name, None, "cache_directory_unavailable")
        try:
            marker = json.loads((path / ".herdr-compiler-cache.json").read_text())
            if not isinstance(marker, dict) or str(marker.get("volume_uuid", "")).lower() != tier.volume_uuid.lower():
                return StorageCandidate(tier.name, None, "cache_identity_mismatch")
        except (OSError, ValueError):
            return StorageCandidate(tier.name, None, "cache_not_enrolled")
    else:
        path = Path(tier.directory).expanduser()
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        if not os.access(probe, os.W_OK):
            return StorageCandidate(tier.name, None, "not_writable")
        # The one disk figure (df: reserved blocks are neither used nor available), shared with the guard and
        # the cleanup, so "full" means the same thing in all three.
        if 100 - measure(probe)[0] < min_free_percent:
            return StorageCandidate(tier.name, None, "low_free_disk")
    except OSError:
        return StorageCandidate(tier.name, None, "storage_unavailable")
    return StorageCandidate(tier.name, path)


def choose_storage(candidates: tuple[StorageCandidate, ...]) -> StorageSelection:
    """Pure ordering rule, independently testable without simulated mounts."""
    skipped: list[str] = []
    for item in candidates:
        if item.directory is not None and item.reason is None:
            return StorageSelection(item.name, item.directory, tuple(skipped))
        skipped.append(f"{item.name}:{item.reason or 'unavailable'}")
    return StorageSelection(None, None, tuple(skipped))


def select_storage(tiers: tuple[StorageTier, ...], default: Path,
                   min_free_percent: float, *, resolve_volume: VolumeResolver = mounted_volume) -> StorageSelection:
    if not tiers:
        return StorageSelection("default", default)
    # Probe lazily: an available primary does not wait for absent lower tiers.
    skipped: list[str] = []
    for tier in tiers:
        selected = choose_storage((_candidate(tier, min_free_percent, resolve_volume),))
        if selected.directory is not None:
            return StorageSelection(selected.name, selected.directory, tuple(skipped))
        skipped.extend(selected.skipped)
    return StorageSelection(None, None, tuple(skipped))
