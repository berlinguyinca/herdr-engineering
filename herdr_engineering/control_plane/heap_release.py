"""Return freed Python heap to the OS after a projection replay.

A full replay builds millions of short-lived row dicts. They are freed to pymalloc/glibc but glibc keeps the
fragmented arenas, so RSS stays at its peak. ``release_heap`` runs ``gc.collect()`` then glibc ``malloc_trim(0)``.
Non-glibc platforms (musl, macOS, a missing library) report ``trimmed=False``; it never raises.

Configured by ``control_plane.heap_release.enabled`` in the central fleet YAML (default true; no environment variable).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)
_KEYS = {"enabled"}


@dataclass(frozen=True)
class HeapRelease:
    rss_before_bytes: int
    rss_after_bytes: int
    trimmed: bool


def _rss_bytes() -> int:
    """Resident set size from /proc/self/statm; 0 where /proc is unavailable."""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _malloc_trim(libc_name: str | None) -> bool:
    try:
        name = ctypes.util.find_library("c") if libc_name is None else libc_name
        if not name:
            return False
        libc = ctypes.CDLL(name)
        trim = libc.malloc_trim          # AttributeError on musl / macOS
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
        return True
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def release_heap(*, libc_name: str | None = None) -> HeapRelease:
    """gc.collect() then malloc_trim(0). ``libc_name`` overrides the library looked up (default: the C library)."""
    before = _rss_bytes()
    gc.collect()
    trimmed = _malloc_trim(libc_name)
    after = _rss_bytes()
    log.info("heap release: rss %d -> %d MiB (trim=%s)", before >> 20, after >> 20, trimmed)
    return HeapRelease(before, after, trimmed)


def parse_heap_release(section: Any) -> dict:
    """Validate ``control_plane.heap_release``; ValueError names the field."""
    if section is None:
        return {"enabled": True}
    if not isinstance(section, dict):
        raise ValueError("heap_release must be a mapping")
    if set(section) - _KEYS:
        raise ValueError("heap_release: unknown key (allowed: enabled)")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("heap_release.enabled must be a boolean (true or false)")
    return {"enabled": enabled}


def heap_release_enabled(control_plane: dict | None) -> bool:
    return parse_heap_release((control_plane or {}).get("heap_release"))["enabled"]


def release_heap_if_enabled(enabled: bool) -> HeapRelease | None:
    """Never lets a release problem break boot or a refresh pass."""
    if not enabled:
        return None
    try:
        return release_heap()
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        log.warning("heap release failed (%s)", type(exc).__name__)
        return None
