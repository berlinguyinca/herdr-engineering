"""Watcher self-restart (issue #865): after the loop's rollout has successfully updated
this host's herdr-engineering install, the loop requests a restart of its own user
service so the fleet runs the build it just shipped.

No mocks: the decision is a pure function tested directly; the dispatch is tested with
an injectable spawn/cgroup seam; the loop tests drive real subprocesses with the Fleet
fixture of tests/test_fleet_watch_run (real bare repos, real HTTP "GitHub", a real
executable ssh stand-in, throwaway HOMEs) and observe the restart through the
HERDR_FLEET_WATCH_RESTART_CMD seam (a real executable writing a marker file). Nothing
here starts or enables a real service.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path

from herdr_engineering.fleet_watch import selfrestart
from herdr_engineering.fleet_watch.selfrestart import (
    DEFAULT_UNIT,
    needs_self_restart,
    request_self_restart,
)
from tests.test_fleet_watch import git
from tests.test_fleet_watch_run import BOOT, HE, fleet, script  # noqa: F401  (fixture)


def rollout(host: str = "beast", updated: bool = True, status: str = "succeeded") -> dict:
    """A saved rollout with one row for this host's herdr-engineering."""
    sha, before = "x" * 40, "y" * 40
    row = {"status": "ok", "sha": sha, "before_sha": before, "after_sha": sha, "checks": [],
           "reason": "", "updated": updated, "rolled_back": False, "rollback_reason": "",
           "attempts": 1}
    return {"id": "r-1", "repo": HE, "sha": sha, "before_sha": before, "reason": "merged",
            "status": status, "rows": {f"{host}/herdr-engineering": row},
            "halt_reason": "", "halt_emitted": False, "announced": False}


# ---------------------------------------------------------------- decision (pure)

def test_needs_self_restart_only_for_a_succeeded_rollout_that_updated_this_host():
    assert needs_self_restart(rollout(), "beast") is True
    assert needs_self_restart(rollout(updated=False), "beast") is False       # no_change / skipped row
    assert needs_self_restart(rollout(status="halted"), "beast") is False     # the fleet did not converge
    assert needs_self_restart(rollout(status="running"), "beast") is False
    assert needs_self_restart(rollout(host="bender"), "beast") is False       # another host's row
    assert needs_self_restart(rollout(), None) is False                       # an all-ssh watcher
    assert needs_self_restart(None, "beast") is False
    empty = rollout()
    empty["rows"] = {}
    assert needs_self_restart(empty, "beast") is False
    failed = rollout()
    failed["rows"]["beast/herdr-engineering"]["status"] = "failed"
    assert needs_self_restart(failed, "beast") is False


# ---------------------------------------------------------------- dispatch (injectable)

def test_request_runs_the_seam_instead_of_systemctl():
    spawned: list[list[str]] = []
    out: list[str] = []
    ok = request_self_restart(env={"HERDR_FLEET_WATCH_RESTART_CMD": "/bin/touch /tmp/marker"},
                              spawn=spawned.append, log=out.append)
    assert ok is True and spawned == [["/bin/touch", "/tmp/marker"]] and out == []


def test_request_uses_systemctl_when_running_under_the_unit(monkeypatch):
    monkeypatch.setattr(selfrestart.shutil, "which", lambda name: "/usr/bin/systemctl")
    spawned: list[list[str]] = []
    ok = request_self_restart(env={}, spawn=spawned.append, log=lambda line: None,
                              cgroup=lambda: f"0::/user.slice/user-1000.slice/user@1000.service/{DEFAULT_UNIT}")
    assert ok is True
    assert spawned == [["/usr/bin/systemctl", "--user", "restart", DEFAULT_UNIT]]


def test_request_is_a_logged_noop_outside_the_unit():
    spawned: list[list[str]] = []
    out: list[str] = []
    ok = request_self_restart(env={}, spawn=spawned.append, log=out.append,
                              cgroup=lambda: "0::/user.slice/user-1000.slice/user@1000.service/bash.service")
    assert ok is False and spawned == []
    assert any("not running under" in line for line in out)


def test_request_refuses_without_systemctl_on_path(monkeypatch):
    monkeypatch.setattr(selfrestart.shutil, "which", lambda name: None)
    out: list[str] = []
    ok = request_self_restart(env={}, spawn=lambda argv: None, log=out.append,
                              cgroup=lambda: f"0::/{DEFAULT_UNIT}")
    assert ok is False and any("systemctl not found" in line for line in out)


def test_request_never_raises_when_the_spawn_fails():
    def boom(argv: list[str]) -> None:
        raise OSError("nope")

    out: list[str] = []
    ok = request_self_restart(env={}, spawn=boom, log=out.append, cgroup=lambda: f"0::/{DEFAULT_UNIT}")
    assert ok is False and any("self-restart request failed" in line for line in out)


def test_a_missing_cgroup_counts_as_not_under_the_unit():
    def no_proc() -> str:
        raise OSError("no /proc")

    out: list[str] = []
    ok = request_self_restart(env={}, spawn=lambda argv: None, log=out.append, cgroup=no_proc)
    assert ok is False and any("not running under" in line for line in out)


# ---------------------------------------------------------------- loop integration (fixture)

def as_beast(fleet) -> None:
    """Make the runner (throwaway HOME) look like fleet host `beast`: host.json + installed binary."""
    fleet.identify("beast")
    shim = fleet.runner_home / ".local" / "bin" / "herdr-eng"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(f"#!/bin/sh\nexec {str(fleet.shims['beast'])} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)


def loop(fleet, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    argv = ["fleet-watch", "loop", "--lock", str(fleet.lock), "--local-host", "beast",
            "--repo", HE, "--interval", "1", *args]
    return subprocess.run([sys.executable, "-I", "-c", BOOT, *argv], capture_output=True, text=True,
                          env=fleet.env(), timeout=timeout)


def seam(fleet, name: str) -> tuple[Path, Path]:
    """A real executable restart seam; returns (marker file it writes, seam script)."""
    marker = fleet.runner_home / name
    seam_path = script(fleet.root / f"{name}.sh", f'printf "%s" "$*" > "{marker}"')
    return marker, seam_path


def wait_for(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def test_loop_requests_a_self_restart_after_updating_this_hosts_herdr_engineering(fleet, monkeypatch):
    as_beast(fleet)
    marker, seam_path = seam(fleet, "restart.marker")
    monkeypatch.setenv(selfrestart.SEAM, str(seam_path))
    out = loop(fleet, "--enable-rollouts", "--max-passes", "1")
    assert out.returncode == 0, out.stderr
    assert fleet.saved()["converged"] == {HE: fleet.forge.head(HE)}
    assert fleet.installed("beast") == fleet.forge.head(HE)
    assert fleet.installed("bender") == fleet.forge.head(HE)
    assert "self-restart" in out.stdout                                   # the pass line says why it exits
    assert wait_for(marker)                                               # the detached request went out


def test_loop_keeps_running_when_the_restart_cannot_be_requested(fleet):
    as_beast(fleet)
    out = loop(fleet, "--enable-rollouts", "--max-passes", "2")
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("pass ") == 2                                 # no early exit after pass 1
    assert "not running under" in out.stderr                              # logged, not fatal
    assert fleet.saved()["converged"] == {HE: fleet.forge.head(HE)}


def test_loop_does_not_request_a_restart_when_this_host_was_not_updated(fleet, monkeypatch):
    as_beast(fleet)
    head = fleet.forge.head(HE)
    checkout = fleet.checkout("beast")
    git(checkout, "fetch", str(fleet.forge.root / f"{HE}.git"), "+refs/heads/main:refs/remotes/origin/main")
    git(checkout, "merge", "--ff-only", "origin/main")                    # this host is already at the head
    receipt = fleet.home("beast") / "state/herdr-engineering/fleet-watch/installed-herdr-engineering.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"sha": head}))                        # ...and the installed build is the head too
    marker, seam_path = seam(fleet, "restart2.marker")
    monkeypatch.setenv(selfrestart.SEAM, str(seam_path))
    out = loop(fleet, "--enable-rollouts", "--max-passes", "1")
    assert out.returncode == 0, out.stderr
    (rid,) = fleet.saved()["rollouts"]
    row = fleet.saved()["rollouts"][rid]["rows"]["beast/herdr-engineering"]
    assert row["updated"] is False
    assert fleet.installed("bender") == fleet.forge.head(HE)              # the fleet still converged
    assert not marker.exists()
    assert "self-restart" not in out.stdout


def test_loop_does_not_request_a_restart_for_a_halted_rollout(fleet, monkeypatch):
    as_beast(fleet)
    (fleet.checkout("bender") / "dirty").write_text("x")
    marker, seam_path = seam(fleet, "restart3.marker")
    monkeypatch.setenv(selfrestart.SEAM, str(seam_path))
    out = loop(fleet, "--enable-rollouts", "--max-passes", "1")
    assert out.returncode == 0, out.stderr
    assert "halted" in out.stdout
    assert not marker.exists()
