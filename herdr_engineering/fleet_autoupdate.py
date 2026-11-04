"""Daily controller fleet maintenance, invoked by the user systemd timer.

An unattended run distinguishes two kinds of bad outcome:

* **skip** - a precondition the operator must resolve first (the checkout holds local
  work, is not on `main`, ...). The updater must never touch that work, so the run
  reports the reason and exits 0: a refused update is not a broken timer, and a red
  systemd unit every night hides the real failures.
* **failure** - the update itself went wrong (fetch, install, playbook, ...), which
  keeps exiting 1.

Both outcomes are recorded in a small state file so `herdr-eng memory doctor` can
surface them; before that, a permanently skipped updater looked exactly like a
permanently failed one.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
from pathlib import Path

# Imported before `.fleet`, which rewrites sys.path at import time (see herdr_engineering/_entry.py).
from . import _entry
from .fleet import fleet_update

#: The interpreter's own `sys.path[0]`, captured before `.fleet` could replace it with the checkout's
#: `scripts/` directory. This is what `shadowed()` is allowed to reason about.
ENTRY_PATH_ZERO = _entry.PATH_ZERO

#: How many dirty paths to name in a skip report (the count is always exact).
SKIP_PREVIEW_FILES = 8
STATE_NAME = "fleet-auto-update.json"


class SkipUpdate(RuntimeError):
    """A precondition the operator must resolve; reported as a skip, not a failure."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"checkout validation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_optional(root: Path, *args: str) -> str | None:
    """Best-effort git probe used only to enrich a report; never raises."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def state_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return state / "herdr-engineering" / STATE_NAME


def read_state() -> dict | None:
    """The last recorded unattended run, or None when there is none."""
    try:
        record = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def record_run(outcome: str, *, reason: str | None = None, revision: str | None = None,
               detail: dict | None = None) -> dict:
    """Persist this run so drift is visible without reading the journal."""
    record: dict = {
        "last_run": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "outcome": outcome,                       # updated | skipped | failed
        "reason": (reason or "")[:500] or None,
        "revision": revision,
        "ok": outcome == "updated",
        "skipped": outcome == "skipped",
    }
    if detail:
        record["detail"] = detail
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        # Losing the state file must not turn a skip into a failure.
        pass
    return record


def _divergence(root: Path) -> dict:
    """How the checkout sits relative to `origin/main`; probes never fail the run."""
    detail: dict = {}
    ahead = _git_optional(root, "rev-list", "--count", "origin/main..HEAD")
    behind = _git_optional(root, "rev-list", "--count", "HEAD..origin/main")
    if ahead is not None:
        detail["unpublished_commits"] = int(ahead)
    if behind is not None:
        detail["behind_origin_main"] = int(behind)
    return detail


def package_root() -> Path:
    """The directory holding the `herdr_engineering` package this process imported."""
    return Path(__file__).resolve().parent.parent


def shadowed(module_root: Path, entry: str, target: Path) -> bool:
    """Whether the code running this update came from the checkout it is about to update.

    For `python -m`, `sys.path[0]` is the working directory. This project is flat-layout
    (`herdr_engineering/` at the repository root), so a unit with `WorkingDirectory=<checkout>`
    imports the checkout rather than the installed build, and the updater that is meant to
    fast-forward a checkout becomes whatever revision that checkout happens to hold. A checkout
    behind then runs an outdated updater, and a fix shipped inside the updater can only arrive by
    fast-forwarding the very checkout it exists to unblock - observed on bender (exit 1 reporting
    the pre-#572 message while the installed build already reported a skip) and on the Mac before
    #583 gave the launchd counterpart a neutral working directory (#584).

    Only cwd shadowing is refused: an editable install reached through the `herdr-eng` console
    script (or `python -m` from a neutral directory) is the operator's own checkout by choice.

    `sys.path[0]` is the sentinel `""` for `python -m` and `python -c` on CPython 3.12 - read back on
    bender, the host this defect was found on - and the sentinel *means* the working directory. So an
    empty entry is resolved against `Path.cwd()` rather than excused: the first version of this
    predicate returned False for it, which let the exact production shape (`-m`, a unit whose working
    directory is inside the checkout) walk past the guard on a real host.
    """
    try:
        # Not strict: the working directory can be a path that no longer exists, and a checkout
        # that has vanished cannot be updated by anyone.
        origin = Path(entry).resolve() if entry else Path.cwd().resolve()
        return origin == target and module_root.resolve() == target
    except OSError:
        return False


def runner_identity(root: Path) -> dict:
    """Which code is performing this update, and whether it is the checkout being updated.

    Two independent routes make the nightly job run a checkout's own code. One is `python -m` with
    the checkout as the process working directory: `sys.path[0]` then wins over every install, and
    that is what the unit used to do (fixed by `WorkingDirectory=%h`, and refused by `shadowed()`).
    The other is an **editable install of the controller checkout** - bender's controller `.venv`
    carries `__editable__.herdr_engineering-0.1.0.pth` - so its runner is the checkout whatever the
    working directory is. That route is not refused: for a clean checkout the fast-forward simply
    updates the code the next run uses. But it is why a dirty checkout is permanently self-blocking
    (the runner cannot be refreshed from a checkout the updater will not touch), so every run records
    it instead of leaving the condition invisible - the mistake D23 warns against is asserting a
    mechanism from how installs *should* be laid out rather than from what actually ran.
    """
    module = package_root().resolve()
    return {"runner_module": str(module), "runner_is_checkout": module == root.resolve()}


def controller_root() -> Path:
    """The controller checkout the unattended updater is configured to update."""
    configured_root = os.environ.get("HERDR_ENGINEERING_REPO")
    if not configured_root:
        raise RuntimeError("HERDR_ENGINEERING_REPO must name the controller checkout")
    return Path(configured_root).resolve(strict=True)


def run_update() -> dict:
    """Run the normal updater only for an explicitly configured clean main checkout."""
    root = controller_root()
    if shadowed(package_root(), ENTRY_PATH_ZERO, root):
        raise RuntimeError(
            f"the fleet updater is running from {package_root()}, the checkout it would update; "
            "run the installed `herdr-eng fleet update`, or give the unit a working directory "
            "outside the checkout (see scripts/herdr-eng-fleet-update.service)")
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    lock_path = state / "herdr-engineering" / "fleet-auto-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another automatic fleet update is already running") from exc
        branch = _git(root, "branch", "--show-current")
        if branch != "main":
            raise SkipUpdate(
                f"automatic updates require the main branch "
                f"(checkout is on {branch or 'a detached HEAD'})",
                {"branch": branch, "checkout": str(root)},
            )
        dirty = [line for line in _git(root, "status", "--porcelain",
                                       "--untracked-files=all").splitlines() if line]
        if dirty:
            detail = {
                "checkout": str(root),
                "dirty_paths": len(dirty),
                "tracked_changes": sum(1 for line in dirty if not line.startswith("?? ")),
                "examples": [line[:120] for line in dirty[:SKIP_PREVIEW_FILES]],
                "hint": "preserve this work elsewhere, or point HERDR_ENGINEERING_REPO at a "
                        "dedicated clean checkout for unattended updates",
            }
            detail.update(_divergence(root))
            raise SkipUpdate(
                "controller checkout is dirty; local work is preserved and this update is skipped",
                detail,
            )
        # fleet_update performs the origin/main fast-forward, installation,
        # service convergence and status verification, failing on any error.
        return fleet_update()


def main() -> int:
    runner: dict = {}
    try:
        runner = runner_identity(controller_root())
    except (OSError, RuntimeError):
        pass  # no controller configured (or unreachable): nothing to attribute this run to
    try:
        report = run_update()
    except SkipUpdate as exc:
        report = {"ok": True, "skipped": True, "reason": str(exc)}
        detail = {**runner, **(exc.detail or {})}
        if detail:
            report["detail"] = detail
        record_run("skipped", reason=str(exc), detail=detail or None)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report = {"ok": False, "error": str(exc)}
        record_run("failed", reason=str(exc), detail=runner or None)
    else:
        record_run("updated" if report.get("ok") else "failed",
                   reason=report.get("error"), revision=report.get("revision"),
                   detail=runner or None)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
