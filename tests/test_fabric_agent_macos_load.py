"""The macOS fabric-agent loader must fail when launchd did not load the agent.

Why this file exists: `scripts/fabric-agent-macos.sh` ran `launchctl bootout` (killing a running
agent), then `bootstrap`, then swallowed every failure with `|| true`, then only `warn`ed and exited
0 if the agent never came back. The Ansible caller's `launchctl kickstart -k` was best-effort too, so
the 2026-11-05 nightly reported a successful fleet update while leaving the mac's fabric agent
stopped. Nothing noticed until parity failed on the next run.

The contract is in scripts/fabric-agent-load.sh so it can be exercised here, where launchd does not
exist - which is the point: a macOS-only failure path that no test can reach is how this shipped.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOADER = REPO / "scripts" / "fabric-agent-load.sh"
LABEL = "com.herdr-engineering.fabric-agent"

# `$LOADED` is a sentinel FILE: its existence is "the agent is loaded". `print` answers from it, and
# every call is appended to $CALLS. `BOOTSTRAP_RC` models a launchd that refuses, and `BOOTOUT_RC`
# one whose bootout does not take the label away (which is what makes `bootstrap` answer 5).
STUB = """#!/bin/sh
echo "launchctl $*" >> "$CALLS"
case "$1" in
  print)     [ -f "$LOADED" ] && exit 0 || exit 1 ;;
  bootout|remove) [ "${BOOTOUT_RC:-0}" = "0" ] && rm -f "$LOADED"; exit "${BOOTOUT_RC:-0}" ;;
  bootstrap|load)  [ "${BOOTSTRAP_RC:-0}" = "0" ] && echo 1 > "$LOADED"; exit "${BOOTSTRAP_RC:-0}" ;;
  *) exit 0 ;;
esac
"""


def env(tmp_path: Path, *, loaded: str = "0", bootstrap_rc: str = "0", bootout_rc: str = "0"):
    stub = tmp_path / "launchctl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    state = tmp_path / "loaded"
    if loaded == "1":
        state.write_text("1")
    else:
        state.unlink(missing_ok=True)
    plist = tmp_path / "agent.plist"
    plist.write_text("<plist/>")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HERDR_LAUNCHCTL": str(stub),
        "LOADED": str(state),
        "CALLS": str(tmp_path / "calls.log"),
        "BOOTSTRAP_RC": bootstrap_rc,
        "BOOTOUT_RC": bootout_rc,
        "HOME": str(tmp_path),
        # launchd needs a moment to report a fresh load; the stub answers instantly.
        "HERDR_FABRIC_AGENT_SETTLE": "0",
    }
    return environment, plist, tmp_path / "calls.log"


def run(environment, plist):
    return subprocess.run(["bash", str(LOADER), LABEL, str(plist)],
                          capture_output=True, text=True, env=environment, timeout=60)


def test_install_fails_when_launchd_did_not_load_the_agent(tmp_path):
    """The bug: bootstrap fails, the script warns, exits 0, and the fleet's agent stays stopped."""
    environment, plist, calls = env(tmp_path, loaded="0", bootstrap_rc="1")
    result = run(environment, plist)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert LABEL in result.stderr and "NOT loaded" in result.stderr
    assert "fabric agent installed" not in result.stdout


def test_install_succeeds_when_the_agent_ends_up_loaded(tmp_path):
    environment, plist, _ = env(tmp_path, loaded="0", bootstrap_rc="0")
    result = run(environment, plist)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"LaunchAgent running: {LABEL}" in result.stdout


def test_a_bootstrap_refusal_that_leaves_the_label_loaded_is_not_a_failure(tmp_path):
    """launchd answers "5: Input/output error" when the label is already loaded.

    Deciding the outcome from bootstrap's exit status would call a healthy agent a failed install -
    the opposite mistake, and the reason the loader asks `print` instead. Modelled here as a bootout
    that does not take the label away, so bootstrap refuses while the agent is in fact loaded.
    """
    environment, plist, _ = env(tmp_path, loaded="1", bootstrap_rc="1", bootout_rc="1")
    result = run(environment, plist)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_a_fleet_update_leaves_a_loaded_agent_alone(tmp_path):
    """HERDR_FABRIC_AGENT_NO_RESTART=1: the caller restarts it once, after all updates succeeded."""
    environment, plist, calls = env(tmp_path, loaded="1")
    environment["HERDR_FABRIC_AGENT_NO_RESTART"] = "1"
    result = run(environment, plist)
    assert result.returncode == 0, (result.stdout, result.stderr)
    recorded = calls.read_text()
    assert "bootout" not in recorded, recorded


def test_a_fleet_update_still_fails_when_no_agent_could_be_loaded(tmp_path):
    """The defer-restart flag defers a *restart*; it must not turn "no agent" into success."""
    environment, plist, _ = env(tmp_path, loaded="0", bootstrap_rc="1")
    environment["HERDR_FABRIC_AGENT_NO_RESTART"] = "1"
    result = run(environment, plist)
    assert result.returncode == 1, (result.stdout, result.stderr)


def test_a_fleet_update_loads_an_agent_that_is_not_loaded(tmp_path):
    """"Leave a loaded agent alone" means load one that is absent - a host whose agent went missing
    is repaired by the next fleet update rather than staying broken."""
    environment, plist, calls = env(tmp_path, loaded="0")
    environment["HERDR_FABRIC_AGENT_NO_RESTART"] = "1"
    result = run(environment, plist)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "bootstrap" in calls.read_text()


def test_a_missing_plist_fails_rather_than_reporting_success(tmp_path):
    environment, plist, _ = env(tmp_path)
    plist.unlink()
    result = run(environment, plist)
    assert result.returncode == 1
    assert "no launchagent plist" in result.stderr.lower()


def test_the_installer_uses_the_loader_rather_than_its_own_copy():
    """The installer once carried its own inline copy, so the tested path was not the shipped one."""
    installer = (REPO / "scripts" / "fabric-agent-macos.sh").read_text()
    assert "fabric-agent-load.sh" in installer
    # The installer must not re-implement the load/verify it is now supposed to delegate.
    assert "bootstrap" not in installer, "the load contract belongs to fabric-agent-load.sh"
    assert "kickstart" not in installer, "the restart contract belongs to fabric-agent-load.sh"
