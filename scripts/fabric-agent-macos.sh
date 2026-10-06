#!/usr/bin/env bash
#
# Install + start the dev-fabric auto-registration agent on macOS as a
# persistent launchd LaunchAgent (spec 0110). This is the macOS counterpart
# of scripts/fabric-agent.sh (which installs a systemd USER unit on Linux);
# macOS has no systemd, so the agent runs as a launchd LaunchAgent in the
# user's GUI login domain.
#
# Model: the agent watches THIS host for web services that start and
# auto-registers them with the fabric gateway, so they appear at a stable
# dev:<port> URL fleet-wide (and in the gateway host's web UI) with zero
# manual steps. Loopback-only services get a tailnet-bound forwarder in
# front of them; leases are closed when a service disappears. No public
# listeners, ever.
#
# What this script ensures (idempotent; safe to re-run):
#   1. `uv` (a self-contained Python manager; needed because macOS system
#      Python is 3.9 and herdr-engineering needs >= 3.12) — installed if
#      missing, no sudo / Homebrew required.
#   2. A Python 3.12 virtualenv (~/.venvs/herdr-engineering) with
#      herdr-engineering installed from the repo (default: the public GitHub
#      repo; override with --repo <git-url>).
#   3. The `tailscale` CLI on PATH (~/.local/bin/tailscale) so the agent can
#      discover this host's tailnet IPv4. The Homebrew CLI lives at
#      /opt/homebrew/bin/tailscale (Apple Silicon) or /usr/local/bin/tailscale
#      (Intel); the first that answers `tailscale ip -4` is symlinked.
#   4. The gateway control URL written to
#      ~/.config/herdr-engineering/fabric.env as HERDR_ENGINEERING_FABRIC_URL
#      (only if not already set there). Machine-local, no secrets.
#   5. A launchd LaunchAgent (com.herdr-engineering.fabric-agent) that runs a
#      wrapper script sourcing fabric.env then `herdr-eng devfabric agent`,
#      with KeepAlive so it restarts on crash and loads at login.
#
# Subcommands:
#   install    (default) ensure everything and start the agent
#   status     show whether the LaunchAgent is running
#   uninstall  stop + remove the LaunchAgent and wrapper (leaves the venv,
#              uv, tailscale symlink and fabric.env in place)
#   uninstall --purge   also remove the venv, fabric.env, tailscale symlink
#
# Usage:
#   scripts/fabric-agent-macos.sh [install|status|uninstall] [--gateway <url>]
#       [--repo <git-url>] [--purge]
#
set -euo pipefail

LABEL="com.herdr-engineering.fabric-agent"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
WRAPPER="$HOME/bin/herdr-fabric-agent.sh"
ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-engineering"
ENV_FILE="$ENV_DIR/fabric.env"
VENV="$HOME/.venvs/herdr-engineering"
VENV_BIN="$VENV/bin"
HERDR_ENG="$VENV_BIN/herdr-eng"
UV_BIN="$HOME/.local/bin/uv"
LOCAL_BIN="$HOME/.local/bin"
TAILSCALE_LINK="$LOCAL_BIN/tailscale"
DEFAULT_REPO="https://github.com/berlinguyinca/herdr-engineering.git"
DEFAULT_GATEWAY="http://dev.lan:29999"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  x %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "this script targets macOS (Darwin); on Linux use scripts/fabric-agent.sh"

# --- parse args -------------------------------------------------------------
CMD="install"
GATEWAY="$DEFAULT_GATEWAY"
REPO="$DEFAULT_REPO"
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    install|status|uninstall) CMD="$1"; shift ;;
    --gateway) GATEWAY="${2:?--gateway needs a URL}"; shift 2 ;;
    --repo)    REPO="${2:?--repo needs a git URL}"; shift 2 ;;
    --purge)   PURGE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# --- status -----------------------------------------------------------------
if [ "$CMD" = "status" ]; then
  if [ ! -f "$PLIST" ]; then echo "LaunchAgent not installed: $PLIST"; exit 1; fi
  UID_="$(id -u)"
  if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
    echo "LaunchAgent running: $LABEL"
    launchctl print "gui/$UID_/$LABEL" 2>/dev/null | grep -E "state =|pid =" | head -2
  else
    echo "LaunchAgent installed but NOT running: $LABEL"
    exit 1
  fi
  exit 0
fi

# --- uninstall --------------------------------------------------------------
if [ "$CMD" = "uninstall" ]; then
  step "stopping + removing LaunchAgent ($LABEL)"
  UID_="$(id -u)"
  launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
  launchctl remove "$LABEL" 2>/dev/null || true
  rm -f "$PLIST" "$WRAPPER"
  echo "  removed $PLIST"
  echo "  removed $WRAPPER"
  if [ "$PURGE" = "1" ]; then
    step "purging machine-local agent state"
    rm -rf "$VENV" "$ENV_FILE" "$TAILSCALE_LINK"
    echo "  removed venv:     $VENV"
    echo "  removed fabric.env: $ENV_FILE"
    echo "  removed tailscale symlink: $TAILSCALE_LINK"
  fi
  step "done. This host no longer runs a dev-fabric agent."
  echo "  Its dev:<port> leases expire via the gateway's probe loop (TTL ~90s)."
  echo "  To also leave the tailnet:  sudo tailscale logout"
  exit 0
fi

# --- 1/6 uv -----------------------------------------------------------------
step "1/6 ensuring uv (self-contained Python manager)"
if [ ! -x "$UV_BIN" ]; then
  echo "  installing uv -> $UV_BIN"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
"$UV_BIN" --version

# --- 2/6 python 3.12 venv + herdr-engineering ------------------------------
step "2/6 ensuring Python 3.12 venv + herdr-engineering"
"$UV_BIN" python install 3.12 >/dev/null 2>&1 || true
if [ ! -x "$HERDR_ENG" ]; then
  "$UV_BIN" venv --python 3.12 "$VENV" >/dev/null
  "$UV_BIN" pip install --python "$VENV_BIN/python" "$REPO"
fi
"$HERDR_ENG" --version

# --- 3/6 tailscale CLI on PATH ---------------------------------------------
step "3/6 ensuring the tailscale CLI is on PATH"
mkdir -p "$LOCAL_BIN"
if ! command -v tailscale >/dev/null 2>&1 && [ ! -x "$TAILSCALE_LINK" ]; then
  for c in /opt/homebrew/bin/tailscale /usr/local/bin/tailscale; do
    if [ -x "$c" ] && [ -n "$("$c" ip -4 2>/dev/null)" ]; then
      ln -sf "$c" "$TAILSCALE_LINK"
      echo "  symlinked $TAILSCALE_LINK -> $c"
      break
    fi
  done
fi
"$TAILSCALE_LINK" ip -4 || warn "tailscale not answering; is the Tailscale app running and logged in?"

# --- 4/6 gateway control URL -> fabric.env ---------------------------------
step "4/6 fabric gateway control URL -> $ENV_FILE"
if ! grep -q '^HERDR_ENGINEERING_FABRIC_URL=' "$ENV_FILE" 2>/dev/null; then
  mkdir -p "$ENV_DIR"
  printf 'HERDR_ENGINEERING_FABRIC_URL=%s\n' "$GATEWAY" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  wrote HERDR_ENGINEERING_FABRIC_URL=$GATEWAY"
else
  echo "  unchanged (already set: $(grep '^HERDR_ENGINEERING_FABRIC_URL=' "$ENV_FILE" | cut -d= -f2-))"
fi

# --- 5/6 wrapper script (mirrors systemd EnvironmentFile) ------------------
step "5/6 writing agent wrapper -> $WRAPPER"
mkdir -p "$HOME/bin"
cat > "$WRAPPER" <<WRAP
#!/bin/bash
set -a
source "$ENV_FILE"
set +a
exec "$HERDR_ENG" devfabric agent
WRAP
chmod +x "$WRAPPER"

# --- 6/6 launchd LaunchAgent plist -----------------------------------------
step "6/6 installing launchd LaunchAgent -> $PLIST"
mkdir -p "$LAUNCH_AGENTS_DIR"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$WRAPPER</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.local/share/herdr-engineering/fabric-agent.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.local/share/herdr-engineering/fabric-agent.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$LOCAL_BIN:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST

# (re)load the agent into the GUI login domain
UID_="$(id -u)"
launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_" "$PLIST" || { launchctl load -w "$PLIST" 2>/dev/null || true; }
launchctl enable "gui/$UID_/$LABEL" 2>/dev/null || true
launchctl kickstart -k "gui/$UID_/$LABEL" 2>/dev/null || true
sleep 3

if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
  echo "  LaunchAgent running: $LABEL"
else
  warn "LaunchAgent did not come up — check the log:"
  warn "  tail ~/.local/share/herdr-engineering/fabric-agent.log"
fi

echo
echo "fabric agent installed (macOS launchd)."
echo "  Status: scripts/fabric-agent-macos.sh status"
echo "  Remove: scripts/fabric-agent-macos.sh uninstall [--purge]"
