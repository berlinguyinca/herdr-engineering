#!/usr/bin/env bash
# Install the default-on disk-hygiene jobs for this user (no root, nothing outside $HOME):
#   worktrees  every 6 h   herdr-eng disk-hygiene run --only worktrees   (finished worktrees + targets)
#   docker     nightly     herdr-eng disk-hygiene run --only docker      (labelled test containers etc.)
# Linux: systemd user timers. macOS: launchd LaunchAgents. Re-running is idempotent.
#
#   scripts/install-disk-hygiene.sh [install|uninstall|status] [--dry-run] [--no-load]
#
# Opt out: HERDR_DISK_HYGIENE=0, or `disk_hygiene: {enabled: false}` in the herdr-eng config
# (the jobs then exit immediately), or `scripts/install-disk-hygiene.sh uninstall`.
# Environment: HERDR_ENG (binary, default: herdr-eng on PATH, else <repo>/.venv/bin/herdr-eng).
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
JOBS="worktrees docker"

write() { # path, content
  if [ "$DRY" = 1 ]; then printf '== %s\n%s\n' "$1" "$2"; else mkdir -p "$(dirname "$1")"; printf '%s\n' "$2" > "$1"; fi
}

linux() {
  local dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" job cal
  for job in $JOBS; do
    case "$job" in worktrees) cal='*-*-* 00/6:15:00' ;; docker) cal='*-*-* 03:30:00' ;; esac
    if [ "$ACTION" = uninstall ]; then
      systemctl --user disable --now "herdr-eng-disk-hygiene-$job.timer" 2>/dev/null || true
      rm -f "$dir/herdr-eng-disk-hygiene-$job.service" "$dir/herdr-eng-disk-hygiene-$job.timer"
      continue
    fi
    write "$dir/herdr-eng-disk-hygiene-$job.service" "[Unit]
Description=HerdR Engineering disk hygiene ($job)

[Service]
Type=oneshot
Environment=\"HERDR_ENGINEERING_CONFIG=%h/.config/herdr-engineering/config.yaml\"
Environment=\"PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\"
ExecStart=\"$HERDR_ENG\" disk-hygiene run --only $job --json
Nice=19
IOSchedulingClass=idle
UMask=0077"
    write "$dir/herdr-eng-disk-hygiene-$job.timer" "[Unit]
Description=HerdR Engineering disk hygiene ($job) timer

[Timer]
OnCalendar=$cal
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target"
  done
  [ "$DRY" = 1 ] && return 0
  if [ "$ACTION" = status ]; then
    systemctl --user list-timers 'herdr-eng-disk-hygiene-*' --no-pager || true; return 0
  fi
  systemctl --user daemon-reload
  if [ "$ACTION" = install ] && [ "$LOAD" = 1 ]; then
    for job in $JOBS; do systemctl --user enable --now "herdr-eng-disk-hygiene-$job.timer"; done
  fi
}

macos() {
  local dir="${HERDR_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}" job label schedule plist
  local logs="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-engineering"
  for job in $JOBS; do
    label="com.herdr-engineering.disk-hygiene-$job"; plist="$dir/$label.plist"
    if [ "$ACTION" = uninstall ]; then
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true; rm -f "$plist"; continue
    fi
    if [ "$ACTION" = status ]; then launchctl print "gui/$(id -u)/$label" 2>/dev/null | sed -n '1,4p' || echo "$label: not loaded"; continue; fi
    case "$job" in
      worktrees) schedule="<key>StartInterval</key><integer>21600</integer>" ;;
      docker) schedule="<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>" ;;
    esac
    write "$plist" "<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$HERDR_ENG</string><string>disk-hygiene</string><string>run</string>
    <string>--only</string><string>$job</string><string>--json</string>
  </array>
  $schedule
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>19</integer>
  <key>StandardOutPath</key><string>$logs/disk-hygiene-$job.log</string>
  <key>StandardErrorPath</key><string>$logs/disk-hygiene-$job.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict></plist>"
    if [ "$DRY" = 0 ] && [ "$LOAD" = 1 ]; then
      mkdir -p "$logs"
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$plist"
    fi
  done
}

case "$(uname -s)" in
  Linux) linux ;;
  Darwin) macos ;;
  *) echo "unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac
[ "$DRY" = 1 ] || echo "disk-hygiene: $ACTION done ($(uname -s))"
