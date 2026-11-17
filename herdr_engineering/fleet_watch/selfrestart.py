"""Watcher self-restart (issue #865).

After a fleet-watch rollout has **succeeded** and it updated this host's own
herdr-engineering install (the checkout fast-forwarded and the new build was
installed), the long-running loop would otherwise keep executing the pre-update
build forever - the fleet runs a build it has itself just updated past. The loop
therefore requests a restart of its own user service so the next pass runs the
build it just shipped.

The decision is a pure function over the saved rollout (`needs_self_restart`):
only a *succeeded* rollout whose *this host* `herdr-engineering` row was actually
`updated` qualifies - `no_change`/`skipped` rows leave the install untouched, and a
`halted`/`running` rollout has not converged the fleet yet, so the old loop keeps
working (it is what handles the halt). Dispatch is injectable; the default asks
`systemctl --user restart` for the unit, detached (the service is about to be
stopped), and only when this process is actually running under that unit
(`/proc/self/cgroup`), so dev runs and tests are never affected. The
`HERDR_FLEET_WATCH_RESTART_CMD` seam replaces the command for tests and dev.

A restart request that fails is logged and never raises: the loop keeps running
the old build and the operator restarts by hand.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

SELF_RESTART_REPO = "herdr-engineering"
DEFAULT_UNIT = "herdr-eng-fleet-watch.service"
SEAM = "HERDR_FLEET_WATCH_RESTART_CMD"
#: The pass outcome the loop prints when it is about to exit for the requested restart.
RESTART_OUTCOME = "succeeded (watcher self-restart requested)"


def needs_self_restart(rollout: dict | None, local_host: str | None) -> bool:
    """True when a succeeded rollout updated this host's herdr-engineering row.

    `rollout` is one saved `state["rollouts"][id]` dict; `local_host` is the
    watcher's own fleet host id (None when every host is reached over ssh). Only
    rows that actually changed (`updated` is True and the row is `ok`) qualify.
    """
    if not rollout or not local_host:
        return False
    if rollout.get("status") != "succeeded":
        return False
    row = rollout.get("rows", {}).get(f"{local_host}/{SELF_RESTART_REPO}")
    return bool(row and row.get("status") == "ok" and row.get("updated") is True)


def _read_cgroup() -> str:
    return Path("/proc/self/cgroup").read_text()


def _running_as_unit(unit: str, cgroup: Callable[[], str] | None) -> bool:
    """True when this process's cgroup path is inside the given systemd unit."""
    try:
        text = (cgroup or _read_cgroup)()
    except OSError:
        return False
    marker = f"/{unit}"
    return any(marker in line for line in text.splitlines() if line.strip())


def _spawn_detached(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd="/",
    )


def request_self_restart(
    *,
    unit: str = DEFAULT_UNIT,
    env: Mapping[str, str] | None = None,
    spawn: Callable[[list[str]], None] | None = None,
    cgroup: Callable[[], str] | None = None,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Request the restart (detached). Returns True when the request was dispatched.

    The `HERDR_FLEET_WATCH_RESTART_CMD` seam (if set) replaces the command
    outright; otherwise this process must be running under `unit` and
    `systemctl` must be on PATH. Never raises: a failed request is logged and
    reported False so the caller keeps running.
    """
    env = os.environ if env is None else env
    log = (lambda line: print(line, file=sys.stderr)) if log is None else log
    seam = env.get(SEAM)
    if seam:
        argv = shlex.split(seam)
    elif not _running_as_unit(unit, cgroup):
        log(
            f"fleet-watch loop: herdr-engineering self-updated on this host, but the loop is not running "
            f"under {DEFAULT_UNIT}; restart the unit by hand to run the new build"
        )
        return False
    else:
        systemctl = shutil.which("systemctl")
        if not systemctl:
            log(
                f"fleet-watch loop: systemctl not found; restart {DEFAULT_UNIT} by hand to run the new build"
            )
            return False
        argv = [systemctl, "--user", "restart", unit]
    try:
        (spawn or _spawn_detached)(argv)
    except (OSError, ValueError) as exc:
        log(f"fleet-watch loop: self-restart request failed: {type(exc).__name__}")
        return False
    return True
