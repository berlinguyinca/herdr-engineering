#!/usr/bin/env bash
# Install daily unattended controller updates on macOS (launchd counterpart of
# scripts/fleet-auto-update.sh, which is systemd-only because macOS has no systemd).
#
# The job runs the same updater the Linux timer runs - `python -m
# herdr_engineering.fleet_autoupdate` - so behaviour is identical on both platforms,
# including the #572 rule that a dirty or non-main checkout is a *reported skip*
# (exit 0, operator work preserved) rather than a failing service. What a skip costs is
# invisible in `launchctl`, so read it with `herdr-eng memory doctor` (`fleet_update`).
#
#   scripts/fleet-auto-update-macos.sh install [--no-load]   write the LaunchAgent
#   scripts/fleet-auto-update-macos.sh run-now               one immediate run
#   scripts/fleet-auto-update-macos.sh status                loaded / last exit / skip state
#   scripts/fleet-auto-update-macos.sh uninstall             bootout + remove the plist
#
# Options:
#   --no-load    write the plist without bootstrap (tests, staging)
#   --dry-run    print the plist, write nothing, load nothing
#
# Environment:
#   HERDR_ENG_PYTHON        controller python (default: <repo>/.venv/bin/python)
#   HERDR_ENGINEERING_REPO  controller checkout (default: this script's repo root)
#   HERDR_LAUNCH_AGENT_DIR  default ~/Library/LaunchAgents
#   HERDR_ENG_LABEL         default com.herdr-engineering.fleet-auto-update
#   XDG_STATE_HOME          log/state dir base (default: ~/.local/state)
set -euo pipefail

REPO_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${HERDR_ENGINEERING_REPO:-$REPO_DEFAULT}"
PYTHON="${HERDR_ENG_PYTHON:-$REPO/.venv/bin/python}"
LABEL="${HERDR_ENG_LABEL:-com.herdr-engineering.fleet-auto-update}"
AGENT_DIR="${HERDR_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-engineering"

# Same schedule as the systemd timer, read from the template rather than restated here, so
# the two installers cannot drift apart (a test asserts they agree).
HOUR=11
MINUTE=59
timer_template="$REPO_DEFAULT/scripts/herdr-eng-fleet-update.timer"
if [ -f "$timer_template" ]; then
  on_calendar="$(sed -n 's/^OnCalendar=\*-\*-\* \([0-9][0-9]\):\([0-9][0-9]\):.*/\1 \2/p' \
    "$timer_template" | head -1)"
  if [ -n "$on_calendar" ]; then
    HOUR=$((10#${on_calendar% *}))
    MINUTE=$((10#${on_calendar#* }))
  fi
fi

command="${1:-install}"
shift || true
LOAD=1
DRY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-load) LOAD=0 ;;
    --dry-run) DRY=1 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$command" in
  install|run-now|status|uninstall) ;;
  *) echo "Usage: $0 [install|run-now|status|uninstall] [--no-load] [--dry-run]" >&2; exit 2 ;;
esac

plist_path() { printf '%s/%s.plist\n' "$AGENT_DIR" "$LABEL"; }

domain() { printf 'gui/%s\n' "$(id -u)"; }

# Escape the five XML specials; every value we interpolate is a path or label we control.
xml_escape() {
  local value=$1
  value=${value//&/&amp;}
  value=${value//</&lt;}
  value=${value//>/&gt;}
  value=${value//\"/&quot;}
  printf '%s' "${value//\'/&apos;}"
}

render_plist() {
  local xml_repo xml_python xml_log_dir xml_state_home
  xml_repo=$(xml_escape "$REPO")
  xml_python=$(xml_escape "$PYTHON")
  xml_log_dir=$(xml_escape "$STATE_DIR")
  # The module appends `herdr-engineering` itself, so the job must be handed the base.
  xml_state_home=$(xml_escape "$(dirname "$STATE_DIR")")
  # Deliberately NOT the checkout: for `python -m`, sys.path[0] is the working directory, so a
  # WorkingDirectory inside the controller checkout makes the job import the checkout's own
  # `herdr_engineering/` and ignore the installed build. Observed on the Mac: the LaunchAgent
  # reported `{"error": "automatic updates require the main branch", "ok": false}` - the message
  # from before the skip behaviour landed - while the venv it was pointed at had it. The module
  # never reads the cwd; it takes the checkout from HERDR_ENGINEERING_REPO.
  xml_home=$(xml_escape "$HOME")
  # A single heredoc so the output is byte-stable for --dry-run and for tests.
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$(xml_escape "$LABEL")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$PYTHON")</string>
    <string>-m</string>
    <string>herdr_engineering.fleet_autoupdate</string>
  </array>
  <key>WorkingDirectory</key><string>$xml_home</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HERDR_ENGINEERING_REPO</key><string>$xml_repo</string>
    <key>XDG_STATE_HOME</key><string>$xml_state_home</string>
    <key>PATH</key><string>$(xml_escape "$(dirname "$PYTHON"):$REPO/.venv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}")</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$xml_log_dir/fleet-auto-update.out</string>
  <key>StandardErrorPath</key><string>$xml_log_dir/fleet-auto-update.err</string>
</dict>
</plist>
PLIST
}

require_controller() {
  if [ ! -x "$PYTHON" ]; then
    echo "Controller Python is not executable: $PYTHON (set HERDR_ENG_PYTHON)" >&2
    exit 1
  fi
  # `git rev-parse`, not a `.git` directory test: a linked worktree keeps its metadata
  # in the main checkout's `worktrees/`, so the directory test rejects a valid checkout.
  if ! git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Controller checkout is not a git work tree: $REPO" >&2
    exit 1
  fi
}

# The updater skips a dirty or non-main checkout on purpose (#572); say so up front,
# because launchd reports exit 0 and the skip is otherwise invisible.
warn_checkout_state() {
  local branch dirty
  branch="$(git -C "$REPO" branch --show-current 2>/dev/null || true)"
  dirty="$(git -C "$REPO" status --porcelain --untracked-files=all 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$branch" != "main" ] || [ "$dirty" != "0" ]; then
    echo "NOTE  $REPO is on '${branch:-detached HEAD}' with $dirty changed path(s)." >&2
    echo "      The updater skips that checkout (operator work is preserved, exit 0)," >&2
    echo "      so unattended updates will NOT be applied until it is clean on main." >&2
    echo "      Check: herdr-eng memory doctor  (check: fleet_update)" >&2
  fi
}

case "$command" in
  install)
    require_controller
    if [ "$DRY" = 1 ]; then
      render_plist
      warn_checkout_state
      exit 0
    fi
    path="$(plist_path)"
    mkdir -p "$AGENT_DIR" "$STATE_DIR"
    if [ -f "$path" ]; then
      existing="$(plutil -extract Label raw "$path" 2>/dev/null || true)"
      if [ "$existing" = "$LABEL" ]; then
        backup="$path.bak-$(date +%Y%m%d-%H%M%S)"
        cp "$path" "$backup"
        echo "Replaced an agent this script owns; previous copy: $backup"
      else
        echo "Refusing to overwrite $path (Label '${existing:-unreadable}' is not $LABEL)." >&2
        exit 1
      fi
    fi
    render_plist > "$path"
    chmod 0644 "$path"
    echo "Installed LaunchAgent: $path (daily ${HOUR}:$(printf '%02d' "$MINUTE"))"
    warn_checkout_state
    if [ "$LOAD" = 1 ]; then
      launchctl bootout "$(domain)" "$path" 2>/dev/null || true
      launchctl bootstrap "$(domain)" "$path"
      launchctl enable "$(domain)/$LABEL"
      launchctl print "$(domain)/$LABEL" | sed -n '1,4p'
    else
      echo "Not loaded (--no-load): launchctl bootstrap $(domain) $path"
    fi
    ;;
  run-now)
    launchctl kickstart -k "$(domain)/$LABEL"
    echo "Started $LABEL now; read the outcome with: $0 status"
    ;;
  status)
    if launchctl print "$(domain)/$LABEL" >/dev/null 2>&1; then
      echo "loaded: $(plist_path)"
      launchctl print "$(domain)/$LABEL" | grep -E "state|last exit code|program =" | head -5
    else
      echo "not loaded: $(plist_path)"
    fi
    state_file="$STATE_DIR/fleet-auto-update.json"
    if [ -f "$state_file" ]; then
      echo "last recorded run:"
      python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(' ', d.get('outcome'), '-', d.get('reason') or '', d.get('detail') or '')" "$state_file"
    else
      echo "no unattended fleet update has recorded a run ($state_file)"
    fi
    ;;
  uninstall)
    path="$(plist_path)"
    launchctl bootout "$(domain)" "$path" 2>/dev/null || true
    if [ -f "$path" ]; then
      rm -f "$path"
      echo "Removed $path"
    else
      echo "No LaunchAgent at $path"
    fi
    ;;
esac
