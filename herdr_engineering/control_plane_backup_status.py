"""Age of the newest control-plane database backup, for ``herdr-eng doctor``.

The backup is ``deploy/control-plane/memory-backup.sh backup --target control-plane`` (daily user timer
``herdr-controlplane-backup.timer``): ``controlplane-<stamp>.dump`` plus ``controlplane-objects-<stamp>.tar`` for the
object store, in ``~/.local/share/herdr-engineering/backups/control-plane``. Only file names and mtimes are read; no
database or container is contacted and no secret is read.

A host that does not run the control plane (no deploy state, no installed timer, no backup directory) gets one
``not applicable`` result. Status vocabulary as in ``memory_hindsight_status``: ``ok | warn | fail | unknown``.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

BACKUP_DIR_REL = Path(".local/share/herdr-engineering/backups/control-plane")
DEPLOY_STATE_REL = Path(".local/state/herdr-engineering/deploys")        # written by deploy.sh on the control-plane host
TIMER_UNIT_REL = Path(".config/systemd/user/herdr-controlplane-backup.timer")
BACKUP_WARN_SECONDS = 36 * 3600        # the timer is daily (+15 min jitter): one missed run is a warning
BACKUP_FAIL_SECONDS = 7 * 24 * 3600
NAME = "control_plane_backup"


def _check(status: str, detail: str, **data: Any) -> dict[str, Any]:
    return {"name": NAME, "status": status, "detail": detail, "data": data}


def _newest_age(directory: Path, pattern: str, now: float) -> int | None:
    files = sorted((p for p in directory.iterdir() if p.match(pattern)), key=lambda p: p.stat().st_mtime)   # iterdir raises on an unreadable directory, glob would hide it
    return max(0, int(now - files[-1].stat().st_mtime)) if files else None


def check_control_plane_backup(home: Path | None = None, *,
                               now: Callable[[], float] = time.time) -> list[dict[str, Any]]:
    home = Path(home) if home else Path.home()
    directory = home / BACKUP_DIR_REL
    if not any((home / rel).exists() for rel in (DEPLOY_STATE_REL, TIMER_UNIT_REL, BACKUP_DIR_REL)):
        return [_check("ok", "not applicable: this host does not run the control plane", applicable=False)]
    try:
        # `controlplane-2*` is the daily series: pre-deploy dumps (`predeploy-...`) and `-objects-` archives do not count
        age = _newest_age(directory, "controlplane-2*.dump", now()) if directory.is_dir() else None
        objects_age = _newest_age(directory, "controlplane-objects-*.tar", now()) if directory.is_dir() else None
    except OSError:
        return [_check("unknown", "backup directory not readable")]
    if age is None:
        return [_check("warn", "no control-plane backup exists yet (run memory-backup.sh backup --target control-plane)")]
    hours = age // 3600
    if age > BACKUP_FAIL_SECONDS:
        return [_check("fail", f"newest control-plane backup is {hours} h old", age_seconds=age)]
    if age > BACKUP_WARN_SECONDS:
        return [_check("warn", f"newest control-plane backup is {hours} h old (daily expected)", age_seconds=age)]
    if objects_age is None or objects_age > BACKUP_WARN_SECONDS:
        # a database dump without its objects is a partial backup
        why = "no object store archive" if objects_age is None else f"newest object store archive is {objects_age // 3600} h old"
        return [_check("warn", f"newest database dump is {hours} h old but {why}", age_seconds=age,
                       objects_age_seconds=objects_age)]
    return [_check("ok", f"newest control-plane backup is {hours} h old", age_seconds=age, objects_age_seconds=objects_age)]
