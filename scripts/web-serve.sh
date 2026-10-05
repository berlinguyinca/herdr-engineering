#!/usr/bin/env bash
#
# Install + start the private HerdR Engineering web UI as a persistent systemd
# USER service and expose it tailnet-only via `tailscale serve`.
#
# Model (spec 0060 + dev-fabric-gateway):
#   - The UI binds LOOPBACK ONLY (private-by-default invariant; web.py defaults
#     are NOT changed, and the unit pins --host 127.0.0.1 so a machine-local
#     config cannot turn it into a public listener).
#   - ONE designated gateway host (bender today) runs this service. The
#     unit is copied to ~/.config/systemd/user/, linger is enabled so it runs
#     without a login session, and `tailscale serve --bg 8787` maps
#     https://<gateway-magicdns> -> 127.0.0.1:8787.
#   - Other fleet hosts use that stable URL or an SSH tunnel. No public
#     listeners, ever.
#
# Idempotent: safe to re-run (re-copies the unit, daemon-reload, enable --now
# is a no-op when already enabled). Graceful when `tailscale serve` is not yet
# available/enabled: prints the exact command and continues (exit 0).
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_ROOT/scripts/herdr-eng-web.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/herdr-eng-web.service"
PORT="${HERDR_ENGINEERING_WEB_PORT:-8787}"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

[ -f "$UNIT_SRC" ] || { echo "missing unit template: $UNIT_SRC" >&2; exit 1; }

# --- 1. install the systemd user unit -------------------------------------
step "1/4 installing systemd user unit -> $UNIT_DST"
mkdir -p "$UNIT_DIR"
cp "$UNIT_SRC" "$UNIT_DST"
systemctl --user daemon-reload
echo "  installed herdr-eng-web.service"

# --- 2. enable linger (run without a login session) -----------------------
step "2/4 enabling linger for $USER"
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
  warn "could not enable linger (need root/sudo) — the service stops when you log out."
  warn "  run manually: $SUDO loginctl enable-linger $USER"
fi

# --- 3. enable + start/restart the service --------------------------------
# `enable` makes it start at boot; `restart` converges an already-running
# instance onto the freshly installed unit (idempotent re-runs apply updates).
step "3/4 enabling + (re)starting herdr-eng-web.service"
systemctl --user enable herdr-eng-web.service >/dev/null 2>&1 || true
systemctl --user restart herdr-eng-web.service
systemctl --user is-active herdr-eng-web.service >/dev/null 2>&1 \
  && echo "  active: herdr-eng-web.service" \
  || warn "  service not active yet — check: systemctl --user status herdr-eng-web.service"

# --- 4. expose via tailscale serve (tailnet-only https) -------------------
step "4/4 tailscale serve (tailnet-only https, port $PORT)"
if command -v tailscale >/dev/null 2>&1; then
  if tailscale serve --bg "$PORT" 2>/dev/null; then
    DNS_NAME="$(tailscale status --json 2>/dev/null \
      | python3 -c 'import sys,json;d=json.load(sys.stdin);print((d.get("Self",{}).get("DNSName") or "").rstrip("."))' 2>/dev/null || true)"
    if [ -n "$DNS_NAME" ]; then
      echo "  serving https://$DNS_NAME/  (tailnet-only)"
    else
      echo "  tailscale serve enabled on port $PORT — find the URL with: tailscale serve status"
    fi
  else
    warn "tailscale serve not enabled (is Tailscale up?). After 'tailscale up', run:"
    warn "  tailscale serve --bg $PORT"
  fi
else
  warn "tailscale not installed. Install it, 'sudo tailscale up', then:"
  warn "  tailscale serve --bg $PORT"
fi

echo
echo "herdr-eng web service installed. Status:"
systemctl --user status herdr-eng-web.service --no-pager 2>/dev/null | head -12 || true
echo
echo "Verify tailnet reachability on any tailnet device:"
echo "  curl https://<gateway-magicdns>/   # or an SSH tunnel: ssh -L 8787:127.0.0.1:8787 <gateway>"
