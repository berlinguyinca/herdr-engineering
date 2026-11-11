"""Fabric build-cache host inventory (spec 06): capability record + compatibility.

Read-only. Nothing here schedules compilation, creates files or directories,
opens the network, or reads credentials. ``collect`` probes THIS host only; a
fleet inventory is the union of records each host reports about itself.

Compatibility is deliberately strict: two hosts may reuse each other's cache
objects only when namespace, OS, architecture and the toolchain digest all
match. Anything else (including a host with no verifiable toolchain) is an
explicit miss with a reason code; there is no cross-platform reuse.
"""
from __future__ import annotations

import os
import shutil
import socket
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .build_cache_config import BuildCacheConfig
from .build_cache_env import Tools, detect_tools, fingerprint_from_text
from .build_cache_storage import StorageError, namespace_prefix, os_arch_from_triple

_MAX_WALK_FILES = 200_000  # bound the used-bytes walk; marks the figure truncated


class Reason(StrEnum):
    COMPATIBLE = "compatible"
    NO_FINGERPRINT = "no_fingerprint"
    NAMESPACE_MISMATCH = "namespace_mismatch"
    OS_MISMATCH = "os_mismatch"
    ARCH_MISMATCH = "arch_mismatch"
    TOOLCHAIN_MISMATCH = "toolchain_mismatch"


@dataclass(frozen=True)
class CacheCapacity:
    size_cap_gib: float
    used_bytes: int
    used_truncated: bool
    free_disk_bytes: int | None


@dataclass(frozen=True)
class HostCapability:
    host_id: str
    os: str
    arch: str
    rustc_version: str | None
    cargo_present: bool
    toolchain_digest: str | None
    sccache_present: bool
    sccache_version: str | None
    cache: CacheCapacity
    policy: dict[str, Any]
    namespace: str
    compat_key: str | None  # namespace/os-arch/digest16/ or None when unverifiable

    def public_view(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Compat:
    compatible: bool
    reason: Reason


def _used_bytes(root: Path) -> tuple[int, bool]:
    total, seen = 0, 0
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            seen += 1
            if seen > _MAX_WALK_FILES:
                return total, True
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total, False


def _free_disk(path: Path) -> int | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _policy(config: BuildCacheConfig) -> dict[str, Any]:
    sc = config.shared_cache
    return {"mode": config.effective_mode.value, "shared_enabled": sc.enabled,
            "shared_writes": config.shared_writes_effective}


def build_capability(config: BuildCacheConfig, *, host_id: str, tools: Tools,
                     cache_dir: Path) -> HostCapability:
    """Pure assembly from explicit inputs (reads cache_dir size and disk free only)."""
    fp = fingerprint_from_text(tools.rustc_vv)
    host_os, arch = os_arch_from_triple(fp.host) if fp else (os.uname().sysname.lower(),
                                                             os.uname().machine)
    ns = config.shared_cache.namespace
    key: str | None = None
    if fp:
        try:
            key = namespace_prefix(ns, fp.digest, host_os, arch)
        except StorageError:
            key = None
    used, trunc = _used_bytes(cache_dir) if cache_dir.is_dir() else (0, False)
    return HostCapability(
        host_id=host_id, os=host_os, arch=arch,
        rustc_version=fp.release if fp else None,
        cargo_present=tools.cargo is not None,
        toolchain_digest=fp.digest if fp else None,
        sccache_present=tools.sccache is not None,
        sccache_version=tools.sccache_version,
        cache=CacheCapacity(config.local.capacity_gib, used, trunc, _free_disk(cache_dir)),
        policy=_policy(config), namespace=ns, compat_key=key)


def compatibility(a: HostCapability, b: HostCapability) -> Compat:
    """Symmetric: reuse is allowed only on identical namespace, os, arch and toolchain."""
    if a.toolchain_digest is None or b.toolchain_digest is None \
            or a.compat_key is None or b.compat_key is None:
        return Compat(False, Reason.NO_FINGERPRINT)
    if a.namespace != b.namespace:
        return Compat(False, Reason.NAMESPACE_MISMATCH)
    if a.os != b.os:
        return Compat(False, Reason.OS_MISMATCH)
    if a.arch != b.arch:
        return Compat(False, Reason.ARCH_MISMATCH)
    if a.toolchain_digest != b.toolchain_digest or a.compat_key != b.compat_key:
        return Compat(False, Reason.TOOLCHAIN_MISMATCH)
    return Compat(True, Reason.COMPATIBLE)


def collect(config: BuildCacheConfig, *, path_env: str | None = None,
            state_dir: Path | None = None) -> HostCapability | None:
    """Probe this host. ``None`` (and nothing touched) unless the section is enabled."""
    if not config.enabled:
        return None
    from .build_cache_location import select_storage
    from .build_coord import _default_state_dir
    default = (state_dir or _default_state_dir()) / "sccache"
    selected = select_storage(config.local.storage_tiers, default,
                              config.local.min_free_disk_percent)
    cache_dir = selected.directory or default
    return build_capability(config, host_id=socket.gethostname(),
                            tools=detect_tools(path_env if path_env is not None
                                               else os.environ.get("PATH", "")),
                            cache_dir=cache_dir)


def inventory_view(config: BuildCacheConfig, hosts: list[HostCapability]) -> dict[str, Any]:
    """JSON-safe read-only view; contains no paths, endpoints or credentials."""
    if not config.enabled:
        return {"enabled": False, "hosts": []}
    return {"enabled": True, "hosts": [h.public_view() for h in hosts]}
