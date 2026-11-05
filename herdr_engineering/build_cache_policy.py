"""Fabric build-cache quota/eviction/admission policy (spec 07).

Everything is default-off: nothing runs unless ``fabric_build_cache.enabled``.
sccache keeps its own LRU size (``SCCACHE_CACHE_SIZE`` = ``local.capacity_gib``), so
this module is a safety net: it only PLANS removal of individual cache files when the
directory is beyond ``eviction.hard_cap_gib``, and only deletes when the operator asks
twice (``--apply`` AND ``eviction.destructive_enabled: true``). Between the two caps it
reports only. Dry-run is the default and creates no files.

Destruction takes ``build-maintenance.lock`` exclusively and non-blocking (running
builds hold it shared, so ``busy`` is reported instead of waiting), consults the
build registry (any related unexpired lease, or an unreadable registry, blocks), and
re-validates every file immediately before ``unlink``: still a regular file (never a
symlink), unchanged size/mtime, realpath inside the cache dir, no new lease. Cargo
``target`` paths are never eligible. Low free disk never causes deletion; it makes
admission refuse (the spec-03 ``low_free_disk`` guard) and is reported.

The slot suggestion is advisory: a pure function of recent ``builds.jsonl`` records,
load average and available memory, bounded by ``adaptive_slots.min/max_slots``. It
changes the effective slot count only when ``adaptive_slots.enabled`` is true.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .build_cache_config import BuildCacheConfig

GIB = 1024 ** 3
_MAX_SCAN_FILES = 2_000_000
_MAX_REPORTED = 200
_TAIL_BYTES = 512 * 1024


@dataclass(frozen=True)
class Entry:
    path: str  # relative to the cache dir
    size: int
    mtime: float
    atime: float

    @property
    def last_used(self) -> float:
        return max(self.mtime, self.atime)


@dataclass(frozen=True)
class Plan:
    reason: str
    used_bytes: int
    capacity_bytes: int
    hard_cap_bytes: int
    victims: tuple[Entry, ...]
    planned_bytes: int
    truncated: bool
    skipped: tuple[tuple[str, str], ...]

    def public_view(self) -> dict[str, Any]:
        return {
            "reason": self.reason, "used_bytes": self.used_bytes,
            "capacity_bytes": self.capacity_bytes, "hard_cap_bytes": self.hard_cap_bytes,
            "planned_bytes": self.planned_bytes, "planned_count": len(self.victims),
            "truncated": self.truncated,
            "victims": [{"path": v.path, "bytes": v.size, "reason": "least_recently_used"}
                        for v in self.victims[:_MAX_REPORTED]],
            "skipped": [{"path": p, "reason": r} for p, r in self.skipped[:_MAX_REPORTED]],
        }


def scan_cache(cache_dir: Path) -> list[Entry]:
    """Regular files under ``cache_dir`` (links and special files are ignored, never followed)."""
    out: list[Entry] = []
    root = str(cache_dir)
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if len(out) >= _MAX_SCAN_FILES:
                raise OSError("cache scan limit reached; refusing to plan on partial data")
            out.append(Entry(os.path.relpath(full, root), st.st_size, st.st_mtime, st.st_atime))
    return out


def plan_eviction(entries: list[Entry], *, capacity_bytes: int, hard_cap_bytes: int,
                  now: float, min_idle_s: float, max_entries: int,
                  max_bytes: int) -> Plan:
    """Pure: oldest-used first, down to ``capacity_bytes``, only above the hard cap."""
    used = sum(e.size for e in entries)
    base = dict(used_bytes=used, capacity_bytes=capacity_bytes, hard_cap_bytes=hard_cap_bytes)
    if used <= capacity_bytes:
        return Plan("within_cap", victims=(), planned_bytes=0, truncated=False,
                    skipped=(), **base)
    if used <= hard_cap_bytes:
        return Plan("over_sccache_cap_report_only", victims=(), planned_bytes=0,
                    truncated=False, skipped=(), **base)
    skipped: list[tuple[str, str]] = []
    eligible: list[Entry] = []
    for e in entries:
        if "target" in Path(e.path).parts:
            skipped.append((e.path, "cargo_target"))
        elif now - e.last_used < min_idle_s:
            continue  # recently used: silently not a candidate
        else:
            eligible.append(e)
    eligible.sort(key=lambda e: (e.last_used, e.path))
    victims: list[Entry] = []
    planned = 0
    truncated = False
    for e in eligible:
        if used - planned <= capacity_bytes:
            break
        if len(victims) >= max_entries or planned + e.size > max_bytes:
            truncated = True
            break
        victims.append(e)
        planned += e.size
    return Plan("over_hard_cap", victims=tuple(victims), planned_bytes=planned,
                truncated=truncated, skipped=tuple(skipped), **base)


def _lease_block(state_dir: Path, target: Path) -> str | None:
    """``active_lease`` / ``registry_unreadable`` / None. Fresh read on every call."""
    from .build_registry import RegistryError, active_scopes, db_path
    db = db_path(state_dir)
    if not db.exists() and not db.is_symlink():
        return None
    try:
        scopes = active_scopes(state_dir)
    except RegistryError:
        return "registry_unreadable"
    for raw in scopes:
        if not raw.startswith("/"):
            continue
        scope = Path(raw)
        if target.is_relative_to(scope) or scope.is_relative_to(target):
            return "active_lease"
    return None


def remove_planned(cache_dir: Path, plan: Plan, state_dir: Path, *, now: float,
                   min_idle_s: float) -> tuple[list[str], list[tuple[str, str]]]:
    """Delete planned files, re-validating each one right before ``unlink``."""
    removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    real_root = os.path.realpath(cache_dir)
    for v in plan.victims:
        full = os.path.join(cache_dir, v.path)
        try:
            st = os.lstat(full)
        except OSError:
            skipped.append((v.path, "vanished"))
            continue
        if not stat.S_ISREG(st.st_mode):
            skipped.append((v.path, "not_regular_file"))
            continue
        if os.path.commonpath([real_root, os.path.realpath(os.path.dirname(full))]) != real_root:
            skipped.append((v.path, "outside_cache_dir"))
            continue
        if st.st_size != v.size or st.st_mtime != v.mtime or now - st.st_mtime < min_idle_s:
            skipped.append((v.path, "changed_since_plan"))
            continue
        block = _lease_block(state_dir, Path(real_root) / v.path)
        if block:
            skipped.append((v.path, block))
            continue
        try:
            os.unlink(full)
        except OSError:
            skipped.append((v.path, "unlink_failed"))
            continue
        removed.append(v.path)
    return removed, skipped


def run_eviction(cfg: BuildCacheConfig, state_dir: Path | str, *, apply: bool = False,
                 capacity_bytes: int | None = None, hard_cap_bytes: int | None = None,
                 min_idle_s: float | None = None, max_bytes: int | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """One preview or applied pass over ``<state_dir>/sccache``. JSON-safe, no secrets.

    The byte/idle overrides exist for tests; production uses the config values.
    """
    if not cfg.enabled:
        return {"enabled": False}
    from .build_cache_env import _free_percent
    state = Path(state_dir)
    cache_dir = state / "sccache"
    when = time.time() if now is None else now
    capacity = int(cfg.local.capacity_gib * GIB) if capacity_bytes is None else capacity_bytes
    hard = int(cfg.eviction.hard_cap_gib * GIB) if hard_cap_bytes is None else hard_cap_bytes
    idle = cfg.cleanup.min_idle_hours * 3600 if min_idle_s is None else min_idle_s
    cap_bytes = (int(cfg.cleanup.max_evict_per_pass_gib * GIB) if max_bytes is None
                 else max_bytes)
    free = _free_percent(cache_dir)
    low = free is not None and free < cfg.local.min_free_disk_percent
    out: dict[str, Any] = {
        "enabled": True, "mode": "apply" if apply else "dry_run",
        "admission": {"allowed": not low, "reason": "low_free_disk" if low else "ok"},
    }
    if cache_dir.is_symlink() or (cache_dir.exists() and not cache_dir.is_dir()):
        return {**out, "status": "refused", "reason": "cache_dir_unsafe"}
    if apply and not cfg.eviction.destructive_enabled:
        return {**out, "status": "refused", "reason": "destructive_not_enabled"}

    lock_fd = None
    if apply:
        try:
            state.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(state / "build-maintenance.lock",
                              os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            return {**out, "status": "busy"}
        except OSError:
            if lock_fd is not None:
                os.close(lock_fd)
            return {**out, "status": "refused", "reason": "lock_unavailable"}
    try:
        block = _lease_block(state, cache_dir)
        try:
            entries = scan_cache(cache_dir) if cache_dir.is_dir() else []
        except OSError as exc:
            return {**out, "status": "error", "reason": str(exc)}
        plan = plan_eviction(entries, capacity_bytes=capacity, hard_cap_bytes=hard,
                             now=when, min_idle_s=idle,
                             max_entries=cfg.eviction.max_entries_per_run,
                             max_bytes=cap_bytes)
        out["plan"] = plan.public_view()
        if not apply:
            out["status"] = "preview"
            if block:
                out["blocked_by"] = block
            return out
        if block:
            return {**out, "status": "skipped", "reason": block}
        removed, skipped = remove_planned(cache_dir, plan, state, now=when, min_idle_s=idle)
        out.update(status="applied", removed_count=len(removed),
                   removed_bytes=sum(v.size for v in plan.victims if v.path in set(removed)),
                   recheck_skipped=[{"path": p, "reason": r} for p, r in skipped[:_MAX_REPORTED]])
        return out
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


# ---- advisory concurrency suggestion ------------------------------------------
@dataclass(frozen=True)
class SlotRecommendation:
    slots: int
    reasons: tuple[str, ...]
    inputs: dict[str, Any]


def read_recent_builds(path: Path | str, limit: int = 200) -> list[dict[str, Any]]:
    """Last ``limit`` ``build_finished`` records; unreadable or garbage lines are skipped."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == "build_finished":
            out.append(rec)
    return out[-limit:]


def parse_meminfo(text: str) -> int | None:
    """``MemAvailable`` in kB from /proc/meminfo text."""
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def recommend_slots(records: list[dict[str, Any]], *, load1: float | None, cores: int,
                    mem_available_kb: int | None, min_slots: int,
                    max_slots: int) -> SlotRecommendation:
    """Pure: slot count in ``[min_slots, max_slots]`` from observed load, memory and history."""
    slots = max_slots
    reasons: list[str] = []
    cores = max(1, cores)
    if load1 is not None:
        pressure = load1 / cores
        if pressure >= 1.0:
            slots, _ = min_slots, reasons.append("high_load")
        elif pressure > 0.5:
            cut = min_slots + int((max_slots - min_slots) * (1.0 - pressure) / 0.5)
            if cut < slots:
                slots = cut
                reasons.append("moderate_load")
    rss = sorted(r for r in (_num(x.get("max_rss_kb")) for x in records) if r)
    if rss and mem_available_kb is not None:
        p90 = rss[min(len(rss) - 1, math.ceil(0.9 * len(rss)) - 1)]
        by_mem = int(0.7 * mem_available_kb / p90)
        if by_mem < slots:
            slots = by_mem
            reasons.append("memory_bound")
    cpu = []
    for x in records:
        el, u, s = _num(x.get("elapsed_s")), _num(x.get("user_cpu_s")), _num(x.get("sys_cpu_s"))
        if el and u is not None and s is not None:
            cpu.append((u + s) / el)
    if cpu:
        by_cpu = int(cores / max(1.0, sum(cpu) / len(cpu)))
        if by_cpu < slots:
            slots = by_cpu
            reasons.append("cpu_bound")
    if not records:
        reasons.append("no_history")
    slots = max(min_slots, min(max_slots, slots))
    return SlotRecommendation(slots, tuple(reasons), {
        "load1": load1, "cores": cores, "mem_available_kb": mem_available_kb,
        "builds_considered": len(records), "min_slots": min_slots, "max_slots": max_slots})


def observed_slot_suggestion(cfg: BuildCacheConfig, state_dir: Path | str) -> dict[str, Any]:
    """Read real load/memory/history and report the advisory recommendation."""
    try:
        load1: float | None = os.getloadavg()[0]
    except OSError:
        load1 = None
    try:
        mem = parse_meminfo(Path("/proc/meminfo").read_text())
    except OSError:
        mem = None
    a = cfg.adaptive_slots
    rec = recommend_slots(read_recent_builds(Path(state_dir) / "builds.jsonl"), load1=load1,
                          cores=os.cpu_count() or 1, mem_available_kb=mem,
                          min_slots=a.min_slots, max_slots=a.max_slots)
    return {"recommended": rec.slots, "reasons": list(rec.reasons), "inputs": rec.inputs,
            "advisory": not a.enabled, "adaptive_enabled": a.enabled}


def effective_slots(cfg: BuildCacheConfig | None, default: int, state_dir: Path | str) -> int:
    """``default`` unless ``adaptive_slots.enabled``; never raises."""
    if cfg is None or not cfg.enabled or not cfg.adaptive_slots.enabled:
        return default
    try:
        return int(observed_slot_suggestion(cfg, state_dir)["recommended"])
    except Exception:  # noqa: BLE001 - never block a build
        return default
