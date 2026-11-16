#!/usr/bin/env bash
# Load (or reload) the fabric-agent LaunchAgent and PROVE it ended up loaded.
#
# This is the whole contract of scripts/fabric-agent-macos.sh's last step, split out so it can be
# tested where launchd does not exist - the unit gate runs the whole suite on Linux, and a macOS-only
# script whose failure path nobody can run is exactly how the fleet lost its mac agents:
#
#   the installer ran `bootout` (killing a running agent), `bootstrap` then failed, every launchctl
#   call was `|| true`-swallowed, the script warned and exited 0, the Ansible caller's best-effort
#   `kickstart -k` failed against a service that was never loaded, and the nightly fleet update
#   reported success. Parity noticed hours later; nothing else did.
#
# So: `launchctl bootstrap` answers "5: Input/output error" for a label that is already loaded, which
# is not a failure - its exit status decides nothing. Whether the agent is loaded is settled here,
# once, by asking launchd directly, and a "not loaded" answer is exit 1.
#
# Usage: fabric-agent-load.sh <label> <plist>        # reload, then verify
#        HERDR_FABRIC_AGENT_NO_RESTART=1 ...         # leave a loaded agent alone (fleet update)
#        HERDR_LAUNCHCTL=<path>                      # launchd to talk to (tests inject a stub)
set -euo pipefail

LABEL="${1:?usage: fabric-agent-load.sh <label> <plist>}"
PLIST="${2:?usage: fabric-agent-load.sh <label> <plist>}"
LAUNCHCTL="${HERDR_LAUNCHCTL:-launchctl}"
GUI_DOMAIN="gui/$(id -u)"

if [ ! -f "$PLIST" ]; then
  echo "ERROR: no LaunchAgent plist at $PLIST" >&2
  exit 1
fi

if [ "${HERDR_FABRIC_AGENT_NO_RESTART:-}" = "1" ] \
    && "$LAUNCHCTL" print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "  loaded agent left alone (HERDR_FABRIC_AGENT_NO_RESTART=1; caller restarts it)"
  exit 0
fi

"$LAUNCHCTL" bootout "$GUI_DOMAIN/$LABEL" 2>/dev/null || true
"$LAUNCHCTL" bootstrap "$GUI_DOMAIN" "$PLIST" 2>/dev/null \
  || "$LAUNCHCTL" load -w "$PLIST" 2>/dev/null || true
"$LAUNCHCTL" enable "$GUI_DOMAIN/$LABEL" 2>/dev/null || true
"$LAUNCHCTL" kickstart -k "$GUI_DOMAIN/$LABEL" 2>/dev/null || true
sleep "${HERDR_FABRIC_AGENT_SETTLE:-3}"

if "$LAUNCHCTL" print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "  LaunchAgent running: $LABEL"
  exit 0
fi

# The bootout above has already run, so "not loaded" means we stopped the agent and left it stopped.
echo "ERROR: $LABEL is NOT loaded in $GUI_DOMAIN after install; the agent is not running." >&2
echo "  check the log: tail \"\$HOME/.local/share/herdr-engineering/fabric-agent.log\"" >&2
echo "  a GUI login session is required for a LaunchAgent to load (locked/sleeping/headless is not)" >&2
exit 1
