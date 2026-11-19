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

import fcntl
import json
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

OVERRIDE_ENV = "HERDR_DISK_GUARD_OVERRIDE"
EXIT_REFUSED = 75  # EX_TEMPFAIL: retry after space is freed or with the override
_COOLDOWN_SECONDS = 600
_ABORT_COOLDOWN_SECONDS = 120  # a sweep that failed as a whole backs off briefly: its cause may persist
DEFAULT_MIN_FREE_GIB = 50.0
BUSY_WAIT_SECONDS = 120.0  # longest wait for another sweep that is already freeing space


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    usage_percent: float
    message: str = ""
    cleanup_ran: bool = False


def measure(path: Path) -> tuple[float, float]:
    """(percent used, GiB available to the user) as ``df`` reports them (reserved blocks are not free)."""
    usage = shutil.disk_usage(path)  # used = blocks - bfree, free = bavail: the figures ``df`` prints
    if usage.used + usage.free <= 0:
        raise OSError(f"Cannot measure filesystem at {path}")
    return 100 * usage.used / (usage.used + usage.free), usage.free / 2**30


def _existing(path: Path) -> Path:
    """The closest existing ancestor of ``path`` (a directory a build will create is measured where it lands)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _stamp(state_dir: Path) -> Path:
    return state_dir / "disk-guard.json"


def _read_stamp(state_dir: Path) -> dict:
    try:
        data = json.loads(_stamp(state_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _recently_triggered(state_dir: Path, now: float) -> bool:
    stamp = _read_stamp(state_dir)
    try:
        cooldown = _ABORT_COOLDOWN_SECONDS if stamp.get("aborted") else _COOLDOWN_SECONDS
        return now - float(stamp["triggered_at"]) < cooldown
    except (KeyError, ValueError, TypeError):
        return False


def last_abort(state_dir: Path) -> str:
    """Why the last sweep failed as a whole ('' when it did not)."""
    reason = _read_stamp(state_dir).get("aborted")
    return reason if isinstance(reason, str) else ""


def last_blocked(state_dir: Path) -> str:
    """Which target the last sweep left alone because a process was using it ('' when none)."""
    reason = _read_stamp(state_dir).get("blocked")
    return reason if isinstance(reason, str) else ""


def _wait_for_other_sweep(state_dir: Path, seconds: float) -> None:
    """Block (bounded) until the sweep that holds ``build-cleanup.lock`` finishes."""
    try:
        fd = os.open(state_dir / "build-cleanup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        return
    try:
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.2)
    finally:
        os.close(fd)


def relieve(cleanup_cfg: Mapping, state_dir: Path, at_percent: float,
            busy_wait: float = BUSY_WAIT_SECONDS, *, probe: Path | None = None,
            pressure: bool = True) -> bool | None:
    """One cleanup of the configured roots at ``at_percent`` usage and above.

    ``probe`` limits it to roots on that path's filesystem, the one under pressure. ``pressure`` (the
    guard's refusal line) lets the short idle floor apply from ``at_percent``; without it (the free-space
    reserve) only the configured ``pressure_percent`` does, so a reserve at 80% never cleans like 90%.

    Rate limited (a build storm must not spawn a storm of sweeps). Returns True when a sweep ran and removed
    something, False when it removed nothing (a sweep that failed as a whole starts a short, 2 minute
    cooldown and its reason goes into the refusal) or another
    sweep was already freeing space (it is waited for, at most ``busy_wait`` seconds), and
    None when none could run (no roots, or one ran moments ago): the caller decides from a measurement.
    """
    from .build_cleanup import run_once
    from .build_maintenance import effective_roots, policy_kwargs
    now = time.time()
    roots = effective_roots(dict(cleanup_cfg))
    if not roots or _recently_triggered(state_dir, now):
        return None
    kwargs = policy_kwargs(dict(cleanup_cfg))
    kwargs["high_water"] = max(min(kwargs["high_water"], at_percent), 1)
    kwargs["low_water"] = min(kwargs["low_water"], kwargs["high_water"] - 1)
    if pressure:
        kwargs["pressure_water"] = min(kwargs["pressure_water"], at_percent)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        device = os.stat(_existing(probe)).st_dev if probe is not None else None
        result = run_once(roots=roots, state_dir=state_dir, only_device=device, **kwargs)
    except OSError:
        return None  # a full disk can fail the sweep itself: nothing ran, so no cooldown starts
    if result.get("status") == "busy":  # another sweep is running: its result is what we need
        _wait_for_other_sweep(state_dir, busy_wait)
        return False
    removed = bool(result.get("removed_count"))
    stamp: dict[str, object] = {"triggered_at": now}
    if result.get("aborted"):  # the sweep itself failed: back off briefly (a persistent cause must not
        stamp["aborted"] = str((result.get("errors") or ["unknown"])[0])[:300]  # become a sweep storm)
    blocked = result.get("blocked") or []
    if blocked:  # why the cleanup could not free what it found, for the refusal message
        stamp["blocked"] = f"{blocked[0]['path']}: {blocked[0]['reason']}"[:600]
    try:
        _stamp(state_dir).write_text(json.dumps(stamp))
    except OSError:
        pass  # no room for the stamp: still decide from the measurement
    return removed


def check_build_allowed(path: Path, cleanup_cfg: Mapping, state_dir: Path,
                        env: Mapping[str, str] | None = None,
                        busy_wait: float = BUSY_WAIT_SECONDS) -> GuardDecision:
    """Decide whether a build whose output lands on ``path``'s filesystem may start.

    When another sweep already holds the cleanup lock, this waits (at most ``busy_wait`` seconds) for it
    to finish and re-measures, rather than refusing while that sweep is freeing the space.
    """
    env = os.environ if env is None else env
    enabled = bool(cleanup_cfg.get("enabled", True))
    refuse_at = float(cleanup_cfg.get("refuse_builds_percent", 90))
    floor_gib = float(cleanup_cfg.get("refuse_builds_min_free_gib", DEFAULT_MIN_FREE_GIB))
    probe = _existing(path)
    usage, free_gib = measure(probe)
    if not enabled or usage < refuse_at or free_gib >= floor_gib:
        return GuardDecision(True, usage)
    if env.get(OVERRIDE_ENV) == "1":
        return GuardDecision(True, usage, f"disk {usage:.1f}% full; proceeding because {OVERRIDE_ENV}=1")
    ran = relieve(cleanup_cfg, state_dir, refuse_at, busy_wait, probe=probe)
    if ran is not None:
        usage, free_gib = measure(probe)
        if usage < refuse_at or free_gib >= floor_gib:
            return GuardDecision(True, usage, cleanup_ran=ran)
    ran = bool(ran)
    aborted = last_abort(state_dir)
    return GuardDecision(
        False, usage,
        f"disk is {usage:.1f}% full with {free_gib:.1f} GiB free (limits {refuse_at:g}% / "
        f"{floor_gib:g} GiB): refusing to start a new build. "
        + (f"The last cleanup could not run: {aborted}. " if aborted else "")
        + (f"The last cleanup left a target alone, in use ({last_blocked(state_dir)}). " if last_blocked(state_dir) else "")
        + f"Free space (herdr-eng build-cleanup run) or set {OVERRIDE_ENV}=1 to build anyway.",
        cleanup_ran=ran)
