"""Daily controller fleet maintenance, invoked by the user systemd timer."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

from .fleet import fleet_update


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"checkout validation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def run_update() -> dict:
    """Run the normal updater only for an explicitly configured clean main checkout."""
    configured_root = os.environ.get("HERDR_ENGINEERING_REPO")
    if not configured_root:
        raise RuntimeError("HERDR_ENGINEERING_REPO must name the controller checkout")
    root = Path(configured_root).resolve(strict=True)
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    lock_path = state / "herdr-engineering" / "fleet-auto-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another automatic fleet update is already running") from exc
        if _git(root, "branch", "--show-current") != "main":
            raise RuntimeError("automatic updates require the main branch")
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise RuntimeError("controller checkout is dirty; preserve local work before updating")
        # fleet_update performs the origin/main fast-forward, installation,
        # service convergence and status verification, failing on any error.
        return fleet_update()


def main() -> int:
    try:
        report = run_update()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
