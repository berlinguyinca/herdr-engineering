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
working (it is what handles the halt). A positive request makes the loop exit with
`RESTART_EXIT_CODE` (75): the unit's existing `Restart=on-failure` (75 is not in
`RestartPreventExitStatus=2 3`) restarts the service on the newly installed build,
and no detached helper is spawned - the unit's cgroup cleanup would race a child
`systemctl` and could kill it before it queued its job (review of #867). Only
processes actually running under the unit (`/proc/self/cgroup`) take this path, so
dev runs and tests are never affected; the `HERDR_FLEET_WATCH_RESTART_CMD` seam
adds an injectable command for tests and dev.

A restart request that fails is logged and never raises: the loop keeps running
the old build and the operator restarts by hand.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

SELF_RESTART_REPO = "herdr-engineering"
DEFAULT_UNIT = "herdr-eng-fleet-watch.service"
SEAM = "HERDR_FLEET_WATCH_RESTART_CMD"
#: The pass outcome the loop prints when it is about to exit for the requested restart.
RESTART_OUTCOME = "succeeded (watcher self-restart requested)"
#: The loop's exit status after a requested restart. Non-zero so the unit's existing
#: `Restart=on-failure` restarts it (75 is not in `RestartPreventExitStatus=2 3`, and 75 is
#: sysexits EX_TEMPFAIL: a temporary, expected state). It must NOT be 0 (systemd would not
#: restart) and must not spawn a detached `systemctl`: the unit's cgroup cleanup
#: (KillMode=control-group) would SIGTERM that child before it queued its job (review of #867).
RESTART_EXIT_CODE = 75


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
    """Request the self-restart. Returns True when the caller must exit with RESTART_EXIT_CODE.

    When running under `unit` nothing is spawned: systemd's `Restart=on-failure` restarts
    the unit on the non-zero exit, on the newly installed build - no child to be killed by
    the unit's own cgroup cleanup. The `HERDR_FLEET_WATCH_RESTART_CMD` seam (if set) is
    spawned detached for tests and dev, and the caller still exits with RESTART_EXIT_CODE.
    Outside the unit (a dev run) a note is logged and False is returned so the loop keeps
    running. Never raises: a failed request is logged and reported False.
    """
    env = os.environ if env is None else env
    log = (lambda line: print(line, file=sys.stderr)) if log is None else log
    seam = env.get(SEAM)
    if seam:
        try:
            (spawn or _spawn_detached)(shlex.split(seam))
        except (OSError, ValueError) as exc:
            log(f"fleet-watch loop: self-restart request failed: {type(exc).__name__}")
            return False
        return True
    under_unit = _running_as_unit(unit, cgroup)
    if not under_unit:
        log(
            f"fleet-watch loop: herdr-engineering self-updated on this host, but the loop is not running "
            f"under {DEFAULT_UNIT}; restart the unit by hand to run the new build"
        )
    return under_unit
