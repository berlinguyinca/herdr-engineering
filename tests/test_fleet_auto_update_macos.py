"""scripts/fleet-auto-update-macos.sh — the launchd counterpart of the systemd installer.

Only launchctl-free paths are exercised (``--dry-run``, ``install --no-load``,
``status``, ``uninstall``), so the suite never touches a real ``~/Library/LaunchAgents``
or a live launchd domain. What these tests hold is the contract the updater's skip
behaviour depends on: the same module runs on both platforms, the checkout state is
warned about, and the recorded skip is readable where `herdr-eng memory doctor` looks.
"""
from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fleet-auto-update-macos.sh"
LABEL = "com.herdr-engineering.test-fleet-auto-update"


def env(tmp_path: Path, *, branch: str = "main", dirty: bool = False) -> dict:
    """A throwaway controller checkout, agent dir and state dir."""
    checkout = tmp_path / "checkout"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@example.invalid"],
                   check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True)
    (checkout / "file.txt").write_text("content\n")
    subprocess.run(["git", "-C", str(checkout), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "seed"], check=True)
    if branch != "main":
        subprocess.run(["git", "-C", str(checkout), "checkout", "-qb", branch], check=True)
    if dirty:
        (checkout / "file.txt").write_text("operator work\n")

    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "HERDR_ENGINEERING_REPO": str(checkout),
        "HERDR_ENG_PYTHON": str(python),
        "HERDR_LAUNCH_AGENT_DIR": str(tmp_path / "LaunchAgents"),
        "HERDR_ENG_LABEL": LABEL,
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def run(environment: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], env=environment, capture_output=True,
                          text=True, timeout=60)


def test_dry_run_prints_a_plist_and_writes_nothing(tmp_path):
    environment = env(tmp_path)
    result = run(environment, "install", "--dry-run")
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout.encode())
    assert plist["Label"] == LABEL
    # The same updater the systemd timer runs, so behaviour cannot diverge per platform.
    assert plist["ProgramArguments"][1:] == ["-m", "herdr_engineering.fleet_autoupdate"]
    assert plist["EnvironmentVariables"]["HERDR_ENGINEERING_REPO"] == environment["HERDR_ENGINEERING_REPO"]
    # cwd is sys.path[0] for `python -m`: pointing it at the checkout would run the checkout's
    # own source and silently ignore the installed build.
    assert plist["WorkingDirectory"] == environment["HOME"]
    assert plist["WorkingDirectory"] != environment["HERDR_ENGINEERING_REPO"]
    assert plist["EnvironmentVariables"]["XDG_STATE_HOME"] == environment["XDG_STATE_HOME"]
    # Parity with the systemd template is asserted separately; shape matters here.
    assert plist["StartCalendarInterval"] == {"Hour": 11, "Minute": 59}
    assert plist["RunAtLoad"] is False
    assert not (Path(environment["HERDR_LAUNCH_AGENT_DIR"])).exists()


def test_install_without_load_writes_the_agent_and_keeps_the_previous_copy(tmp_path):
    environment = env(tmp_path)
    first = run(environment, "install", "--no-load")
    assert first.returncode == 0, first.stderr
    path = Path(environment["HERDR_LAUNCH_AGENT_DIR"]) / f"{LABEL}.plist"
    assert plistlib.loads(path.read_bytes())["Label"] == LABEL
    assert path.stat().st_mode & 0o777 == 0o644

    second = run(environment, "install", "--no-load")
    assert second.returncode == 0, second.stderr
    assert "previous copy" in second.stdout
    backups = list(path.parent.glob(f"{LABEL}.plist.bak-*"))
    assert len(backups) == 1
    assert plistlib.loads(backups[0].read_bytes())["Label"] == LABEL


def test_install_refuses_to_overwrite_someone_elses_agent(tmp_path):
    environment = env(tmp_path)
    agent_dir = Path(environment["HERDR_LAUNCH_AGENT_DIR"])
    agent_dir.mkdir(parents=True)
    foreign = agent_dir / f"{LABEL}.plist"
    foreign.write_text("<plist><dict><key>Label</key><string>com.other.agent</string></dict></plist>\n")

    result = run(environment, "install", "--no-load")
    assert result.returncode == 1
    assert "Refusing to overwrite" in result.stderr
    assert "com.other.agent" in foreign.read_text()


def test_install_requires_an_executable_controller_python(tmp_path):
    environment = env(tmp_path)
    environment["HERDR_ENG_PYTHON"] = str(tmp_path / "does-not-exist")
    result = run(environment, "install", "--no-load")
    assert result.returncode == 1
    assert "Controller Python is not executable" in result.stderr


def test_a_checkout_the_updater_will_skip_is_warned_about_loudly(tmp_path):
    """A skip exits 0, so launchd shows nothing; the operator must hear it at install time."""
    for branch, dirty, expected in (("feature/x", False, "on 'feature/x'"),
                                    ("main", True, "with 1 changed path(s)")):
        environment = env(tmp_path / (branch.replace("/", "_") or "main"), branch=branch, dirty=dirty)
        result = run(environment, "install", "--dry-run")
        assert result.returncode == 0, result.stderr
        assert expected in result.stderr
        assert "skips that checkout" in result.stderr
        assert "herdr-eng memory doctor" in result.stderr


def test_status_surfaces_the_recorded_skip(tmp_path):
    environment = env(tmp_path)
    state = Path(environment["XDG_STATE_HOME"]) / "herdr-engineering"
    state.mkdir(parents=True)
    # The schema `fleet_autoupdate.record_run()` actually writes.
    (state / "fleet-auto-update.json").write_text(json.dumps(
        {"outcome": "skipped",
         "reason": "automatic updates require the main branch (checkout is on feat/x)",
         "detail": {"branch": "feat/x", "checkout": "/somewhere"}}))

    result = run(environment, "status")
    assert result.returncode == 0, result.stderr
    assert "not loaded" in result.stdout
    assert "skipped" in result.stdout
    assert "feat/x" in result.stdout


def test_status_reports_no_recorded_run(tmp_path):
    result = run(env(tmp_path), "status")
    assert result.returncode == 0, result.stderr
    assert "no unattended fleet update has recorded a run" in result.stdout


def test_uninstall_removes_the_agent(tmp_path):
    environment = env(tmp_path)
    run(environment, "install", "--no-load")
    path = Path(environment["HERDR_LAUNCH_AGENT_DIR"]) / f"{LABEL}.plist"
    assert path.exists()
    result = run(environment, "uninstall")
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert "Removed" in result.stdout


def test_the_schedule_matches_the_systemd_timer_template(tmp_path):
    """The two installers must not drift: launchd reads the time out of the unit template."""
    calendar = (REPO / "scripts" / "herdr-eng-fleet-update.timer").read_text()
    hour, minute = [int(part) for part in calendar.split("OnCalendar=*-*-* ")[1].split(":")[:2]]
    plist = plistlib.loads(run(env(tmp_path), "install", "--dry-run").stdout.encode())
    assert plist["StartCalendarInterval"] == {"Hour": hour, "Minute": minute}


def test_a_linked_worktree_is_a_valid_controller_checkout(tmp_path):
    """`.git` is a file in a linked worktree; a directory test would reject a real checkout."""
    environment = env(tmp_path)
    checkout = Path(environment["HERDR_ENGINEERING_REPO"])
    worktree = tmp_path / "linked"
    subprocess.run(["git", "-C", str(checkout), "worktree", "add", "-q", str(worktree),
                    "-b", "linked-branch"], check=True)
    environment["HERDR_ENGINEERING_REPO"] = str(worktree)
    assert (worktree / ".git").is_file()
    result = run(environment, "install", "--no-load")
    assert result.returncode == 0, result.stderr
    assert (Path(environment["HERDR_LAUNCH_AGENT_DIR"]) / f"{LABEL}.plist").exists()


def test_a_non_repository_is_rejected(tmp_path):
    environment = env(tmp_path)
    environment["HERDR_ENGINEERING_REPO"] = str(tmp_path / "not-a-repo")
    result = run(environment, "install", "--no-load")
    assert result.returncode == 1
    assert "not a git work tree" in result.stderr


def test_unknown_option_and_command_are_rejected(tmp_path):
    environment = env(tmp_path)
    assert run(environment, "install", "--force").returncode == 2
    bad = run(environment, "enable")
    assert bad.returncode == 2
    assert "Usage:" in bad.stderr
