#!/usr/bin/env bash
# Install the periodic `herdr-handoff reap --apply` job for this user (no root, nothing outside $HOME):
# Linux: systemd user service + timer. macOS: a launchd LaunchAgent. Re-running is idempotent.
#
#   scripts/install-handoff-reap.sh [install|uninstall|status] [--dry-run] [--no-load]
#
# The unit files are always rendered, but the timer is only enabled/loaded on a host whose
# ~/.config/herdr-engineering/config.yaml says so (default: off; a host that is already running it is
# disabled again when the YAML turns it off):
#   handoff_reap:
#     enabled: true
#     interval_minutes: 15     # divides 60
#     legacy_roots: []         # <project>/.herdr/handoffs directories to import into the shared store
# `herdr-eng handoff-reap status` prints the policy this script reads. The job never passes
# the unrecorded-records flag to reap. Environment: HERDR_ENG (binary; default: herdr-eng on PATH,
# else <repo>/.venv/bin/herdr-eng).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION=install; DRY=0; LOAD=1
for arg in "$@"; do
  case "$arg" in
    install|uninstall|status) ACTION="$arg" ;;
    --dry-run) DRY=1 ;;
    --no-load) LOAD=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
HERDR_ENG="${HERDR_ENG:-$(command -v herdr-eng || true)}"
[ -n "$HERDR_ENG" ] || HERDR_ENG="$REPO_ROOT/.venv/bin/herdr-eng"
[ -x "$HERDR_ENG" ] || { echo "herdr-eng not found (set HERDR_ENG)" >&2; exit 1; }

# The policy comes from the YAML via herdr-eng itself (one parser, fail closed). Anything unreadable = off, 15 min.
ENABLED=false; INTERVAL=15
if [ "$ACTION" = install ]; then
  if policy="$("$HERDR_ENG" handoff-reap status 2>/dev/null)" && [ -n "$policy" ]; then
    parsed="$(printf '%s' "$policy" | python3 -c '
import json, sys
p = json.load(sys.stdin)["policy"]
print(str(bool(p["enabled"])).lower(), int(p["interval_minutes"]))' 2>/dev/null || echo "false 15")"
    read -r ENABLED INTERVAL <<< "$parsed"
  fi
  case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=15; ENABLED=false ;; esac
fi

write() { # path, content
  if [ "$DRY" = 1 ]; then printf '== %s\n%s\n' "$1" "$2"; else mkdir -p "$(dirname "$1")"; printf '%s\n' "$2" > "$1"; fi
}
render() { # template
  sed -e "s|__HERDR_ENG__|\"${HERDR_ENG//|/\\|}\"|g" -e "s|__INTERVAL_MINUTES__|$INTERVAL|g" "$1"
}

linux() {
  local dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" unit=herdr-eng-handoff-reap
  if [ "$ACTION" = uninstall ]; then
    systemctl --user disable --now "$unit.timer" 2>/dev/null || true
    rm -f "${dir:?}/$unit.service" "${dir:?}/$unit.timer"; return 0
  fi
  if [ "$ACTION" = status ]; then systemctl --user list-timers "$unit.timer" --no-pager || true; return 0; fi
  write "$dir/$unit.service" "$(render "$REPO_ROOT/scripts/$unit.service")"
  write "$dir/$unit.timer" "$(render "$REPO_ROOT/scripts/$unit.timer")"
  [ "$DRY" = 1 ] && return 0
  [ "$LOAD" = 1 ] || { echo "handoff-reap: unit files written, not loaded (--no-load); policy enabled=$ENABLED"; return 0; }
  systemctl --user daemon-reload
  if [ "$ENABLED" = true ]; then
    systemctl --user enable --now "$unit.timer"
  else
    systemctl --user disable --now "$unit.timer" 2>/dev/null || true
    echo "handoff-reap: disabled (handoff_reap.enabled is not true in config.yaml); timer left off"
  fi
}

macos() {
  local dir="${HERDR_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}" label=com.herdr-engineering.handoff-reap
  local plist="$dir/com.herdr-engineering.handoff-reap.plist" logs="$HOME/.local/state/herdr-engineering"
  if [ "$ACTION" = uninstall ]; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "${dir:?}/$label.plist"; return 0
  fi
  if [ "$ACTION" = status ]; then launchctl print "gui/$(id -u)/$label" 2>/dev/null | sed -n '1,4p' || echo "$label: not loaded"; return 0; fi
  write "$plist" "$(sed -e "s|__HERDR_ENG__|${HERDR_ENG//|/\\|}|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__INTERVAL_SECONDS__|$((INTERVAL * 60))|g" "$REPO_ROOT/scripts/$label.plist")"
  [ "$DRY" = 1 ] && return 0
  [ "$LOAD" = 1 ] || { echo "handoff-reap: agent written, not loaded (--no-load); policy enabled=$ENABLED"; return 0; }
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  if [ "$ENABLED" = true ]; then
    mkdir -p "$logs"; launchctl bootstrap "gui/$(id -u)" "$plist"
  else
    echo "handoff-reap: disabled (handoff_reap.enabled is not true in config.yaml); agent left unloaded"
  fi
}

case "$(uname -s)" in
  Linux) linux ;;
  Darwin) macos ;;
  *) echo "unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac
[ "$DRY" = 1 ] || echo "handoff-reap: $ACTION done ($(uname -s))"
