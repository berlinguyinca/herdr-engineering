"""Disk-budget guard: refuse new builds on a nearly full disk, after one pressure cleanup.

The cargo wrapper and ``build-coordinate`` call :func:`check_build_allowed` before starting a
build. At or above ``build_cleanup.refuse_builds_percent`` it runs the pressure cleanup once
(rate limited so a build storm cannot spawn a storm of sweeps), re-measures, and refuses with a
clear message if the disk is still too full. ``HERDR_DISK_GUARD_OVERRIDE=1`` bypasses it.

A build is refused only when the filesystem it writes to is both at least
``refuse_builds_percent`` used and below ``refuse_builds_min_free_gib`` of space available to the
user, so a large disk with ample room is never blocked. A disabled ``build_cleanup`` disables the
guard too.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

OVERRIDE_ENV = "HERDR_DISK_GUARD_OVERRIDE"
EXIT_REFUSED = 75  # EX_TEMPFAIL: retry after space is freed or with the override
_COOLDOWN_SECONDS = 600
DEFAULT_MIN_FREE_GIB = 50.0


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    usage_percent: float
    message: str = ""
    cleanup_ran: bool = False


def measure(path: Path) -> tuple[float, float]:
    """(percent used, GiB available to the user) as ``df`` reports them (reserved blocks are not free)."""
    stat = os.statvfs(path)
    used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
    avail = stat.f_bavail * stat.f_frsize
    if used + avail <= 0:
        raise OSError(f"Cannot measure filesystem at {path}")
    return 100 * used / (used + avail), avail / 2**30


def _stamp(state_dir: Path) -> Path:
    return state_dir / "disk-guard.json"


def _recently_triggered(state_dir: Path, now: float) -> bool:
    try:
        return now - float(json.loads(_stamp(state_dir).read_text())["triggered_at"]) < _COOLDOWN_SECONDS
    except (OSError, ValueError, KeyError, TypeError):
        return False


def check_build_allowed(path: Path, cleanup_cfg: Mapping, state_dir: Path,
                        env: Mapping[str, str] | None = None) -> GuardDecision:
    """Decide whether a build whose output lands on ``path``'s filesystem may start."""
    from .build_cleanup import run_once
    from .build_maintenance import effective_roots, policy_kwargs
    env = os.environ if env is None else env
    enabled = bool(cleanup_cfg.get("enabled", True))
    refuse_at = float(cleanup_cfg.get("refuse_builds_percent", 90))
    floor_gib = float(cleanup_cfg.get("refuse_builds_min_free_gib", DEFAULT_MIN_FREE_GIB))
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage, free_gib = measure(probe)
    if not enabled or usage < refuse_at or free_gib >= floor_gib:
        return GuardDecision(True, usage)
    if env.get(OVERRIDE_ENV) == "1":
        return GuardDecision(True, usage, f"disk {usage:.1f}% full; proceeding because {OVERRIDE_ENV}=1")
    ran = False
    now = time.time()
    roots = effective_roots(dict(cleanup_cfg))
    if roots and not _recently_triggered(state_dir, now):
        kwargs = policy_kwargs(dict(cleanup_cfg))
        kwargs["high_water"] = max(min(kwargs["high_water"], refuse_at), 1)
        kwargs["low_water"] = min(kwargs["low_water"], kwargs["high_water"] - 1)
        kwargs["pressure_water"] = min(kwargs["pressure_water"], refuse_at)
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            result = run_once(roots=roots, state_dir=state_dir, **kwargs)
        except OSError:
            result = {"status": "error"}  # a full disk can fail the sweep itself: refuse below
        if result.get("status") != "busy":  # a busy sweep freed nothing and must not use the cooldown
            ran = True
            try:
                _stamp(state_dir).write_text(json.dumps({"triggered_at": now}))
            except OSError:
                pass  # no room for the stamp: still decide from the measurement below
        usage, free_gib = measure(probe)
        if usage < refuse_at or free_gib >= floor_gib:
            return GuardDecision(True, usage, cleanup_ran=ran)
    return GuardDecision(
        False, usage,
        f"disk is {usage:.1f}% full with {free_gib:.1f} GiB free (limits {refuse_at:g}% / "
        f"{floor_gib:g} GiB): refusing to start a new build. "
        f"Free space (herdr-eng build-cleanup run) or set {OVERRIDE_ENV}=1 to build anyway.",
        cleanup_ran=ran)
