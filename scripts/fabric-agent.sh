#!/usr/bin/env bash
#
# Install + start the dev-fabric auto-registration agent as a persistent
# systemd USER service on this host (spec 0110).
#
# Model: the agent watches THIS host for web services that start and
# auto-registers them with the fabric gateway, so they appear at a stable
# dev:<port> URL fleet-wide (and in the gateway host's web UI) with zero
# manual steps. Loopback-only services get a tailnet-bound forwarder in
# front of them; leases are closed when a service disappears. No public
# listeners, ever.
#
# The gateway control URL is written to ~/.config/herdr-engineering/fabric.env
# as HERDR_ENGINEERING_FABRIC_URL (only if not already set there), so the
# agent knows where the gateway's control API is. Machine-local, no secrets.
#
# Idempotent: safe to re-run (re-renders the unit, daemon-reload, enable
# --now is a no-op when already enabled, restart converges onto the unit).
#
# Usage:
#   scripts/fabric-agent.sh [--gateway http://<gateway-magicdns>:<control-port>]
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_ROOT/scripts/herdr-eng-fabric-agent.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/herdr-eng-fabric-agent.service"
ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-engineering"
ENV_FILE="$ENV_DIR/fabric.env"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

GATEWAY_URL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gateway) GATEWAY_URL="${2:?--gateway needs a URL}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -f "$UNIT_SRC" ] || { echo "missing unit template: $UNIT_SRC" >&2; exit 1; }

# --- 0/4 locate herdr-eng --------------------------------------------------
step "0/4 locating herdr-eng"
HERDR_ENG="$(command -v herdr-eng 2>/dev/null || true)"
[ -n "$HERDR_ENG" ] || HERDR_ENG="$HOME/.local/bin/herdr-eng"
[ -x "$HERDR_ENG" ] || HERDR_ENG="$HOME/.venvs/herdr-engineering/bin/herdr-eng"
if [ ! -x "$HERDR_ENG" ]; then
  warn "herdr-eng not found on this host; agent NOT installed."
  warn "  install it first (this repo's scripts/install.sh) and re-run."
  exit 0
fi
echo "  herdr-eng: $HERDR_ENG"

# --- 1/4 gateway control URL -> env file -----------------------------------
step "1/4 fabric gateway control URL -> $ENV_FILE"
if [ -n "$GATEWAY_URL" ] && ! grep -q '^HERDR_ENGINEERING_FABRIC_URL=' "$ENV_FILE" 2>/dev/null; then
  mkdir -p "$ENV_DIR"
  printf 'HERDR_ENGINEERING_FABRIC_URL=%s\n' "$GATEWAY_URL" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  wrote HERDR_ENGINEERING_FABRIC_URL=$GATEWAY_URL"
else
  echo "  unchanged (no --gateway given, or already set)"
fi

# --- 2/4 render + install the systemd user unit ----------------------------
step "2/4 installing systemd user unit -> $UNIT_DST"
mkdir -p "$UNIT_DIR"
# Replace the __HERDR_ENG__ placeholder with the resolved binary path.
sed "s|__HERDR_ENG__|$HERDR_ENG|g" "$UNIT_SRC" > "$UNIT_DST"
systemctl --user daemon-reload
echo "  installed herdr-eng-fabric-agent.service"

# --- 3/4 enable linger (run without a login session) -----------------------
step "3/4 enabling linger for $USER"
if [ "$(id -u)" = "0" ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi
if $SUDO loginctl enable-linger "$USER" 2>/dev/null; then
  echo "  linger enabled for $USER"
else
  warn "could not enable linger (needs sudo) — the agent stops when you log out."
fi

# --- 4/4 enable + (re)start the service ------------------------------------
step "4/4 enabling + (re)starting herdr-eng-fabric-agent.service"
systemctl --user enable herdr-eng-fabric-agent.service >/dev/null 2>&1 || true
systemctl --user restart herdr-eng-fabric-agent.service
systemctl --user is-active herdr-eng-fabric-agent.service >/dev/null 2>&1 \
  && echo "  active: herdr-eng-fabric-agent.service" \
  || warn "  service not active yet — check: systemctl --user status herdr-eng-fabric-agent.service"

echo
echo "fabric agent installed. Status:"
systemctl --user status herdr-eng-fabric-agent.service --no-pager 2>/dev/null | head -10 || true
