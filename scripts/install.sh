#!/usr/bin/env bash
#
# HerdR Engineering installer (spec 0040/0050).
#
# Assumes `herdr` already exists on this machine and sets up the HerdR
# Engineering integration layer around it. It does NOT install, upgrade, or
# restart Herdr, and it never touches Herdr's own state (~/.config/herdr or
# ~/.local/state/herdr). Your running agent sessions are unaffected.
#
# What it does (idempotent):
#   1. verifies herdr is present (assumption)
#   2. creates a project .venv and installs herdr-engineering (editable + dev)
#   3. creates the machine-local state + artifact directories
#   4. writes a safe private-by-default machine-local config (if absent)
#   5. enrolls THIS machine (detected host + OS) into the private fleet inventory
#      and brings it onto the Tailscale tailnet (auto only if you provide a
#      key via $TS_AUTH_KEY or ~/.config/herdr-engineering/tailscale-auth.key;
#      otherwise it prints the command and continues)
#   6. puts `herdr-eng` on PATH (~/.local/bin) when that directory is writable
#   7. runs `herdr-eng doctor` and prints a summary
#
# Re-running produces no unexpected changes. No secrets are written anywhere.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
PY="$VENV/bin/python"
HERDR_BIN="${HERDR_BIN:-herdr}"

STATE_DIR="${HERDR_ENGINEERING_STATE:-$HOME/.local/share/herdr-engineering/state}"
ARTIFACT_DIR="${HERDR_ENGINEERING_ARTIFACT_ROOT:-$HOME/.local/share/herdr-engineering/artifacts}"
CONFIG_DIR="$HOME/.config/herdr-engineering"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
INVENTORY="$REPO_ROOT/ansible/inventory/hosts.yml"
INVENTORY_EXAMPLE="$REPO_ROOT/ansible/inventory/hosts.example.yml"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  x %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. assume herdr exists ------------------------------------------------
step "1/7 verifying Herdr is present (assumption; will not install/upgrade it)"
if ! command -v "$HERDR_BIN" >/dev/null 2>&1; then
  die "herdr not found on PATH. This installer assumes Herdr already exists. "
      "Install Herdr first, then re-run (or set HERDR_BIN=/path/to/herdr)."
fi
HERDR_VERSION="$("$HERDR_BIN" --version 2>&1 | head -1)"
printf '  herdr %s\n' "$HERDR_VERSION"

# --- 2. venv + package -----------------------------------------------------
step "2/7 Python environment (project .venv; does not touch system Python)"
if [ ! -x "$PY" ]; then
  command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
  python3 -m venv "$VENV"
fi
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e "$REPO_ROOT[dev]"
printf '  installed herdr-engineering into %s\n' "$VENV"

# --- 3. state + artifact directories --------------------------------------
step "3/7 machine-local state + artifact directories"
mkdir -p "$STATE_DIR" "$ARTIFACT_DIR"
printf '  state:    %s\n' "$STATE_DIR"
printf '  artifacts: %s\n' "$ARTIFACT_DIR"

# --- 4. machine-local config (safe defaults) ------------------------------
step "4/7 machine-local config (private-by-default; preserved if present)"
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  printf '  config already present, leaving untouched: %s\n' "$CONFIG_FILE"
else
  cat > "$CONFIG_FILE" <<'YAML'
# HerdR Engineering machine-local config (written by scripts/install.sh).
# Private-by-default. No secrets here; add per-host overrides below.
version: 1

web:
  private_only: true
  host: 127.0.0.1
  port: 8787

artifacts:
  provider: filesystem
  retention_days: 30

dev_fabric:
  enabled: true
  logical_service_name: dev
  external_port_range: [18000, 28999]
  lease_ttl_seconds: 90
  heartbeat_seconds: 30
  private_only: true
  allowed_protocols: [http, https, tcp]

security:
  allow_public_listeners: false
  persist_hidden_reasoning: false
YAML
  printf '  wrote %s\n' "$CONFIG_FILE"
fi

# --- 5. enroll this machine into the private fleet inventory --------------
step "5/7 enrolling this machine into the private fleet inventory"
"$PY" - "$INVENTORY" "$INVENTORY_EXAMPLE" <<'PYCODE'
import socket, sys
from pathlib import Path
import yaml

inv_path, example_path = Path(sys.argv[1]), Path(sys.argv[2])

if inv_path.exists():
    data = yaml.safe_load(inv_path.read_text()) or {}
else:
    data = yaml.safe_load(example_path.read_text()) or {}

fleet = data.setdefault("all", {}) \
             .setdefault("children", {}) \
             .setdefault("herdr_fleet", {}) \
             .setdefault("children", {})

os_name = "linux" if sys.platform.startswith("linux") else "macos"
hosts = fleet.setdefault(os_name, {}).setdefault("hosts", {})

local = socket.gethostname()
added = local not in hosts
hosts[local] = {}

inv_path.parent.mkdir(parents=True, exist_ok=True)
header = ("# Private fleet inventory (managed by scripts/install.sh; NOT committed).\n"
          "# Connection users/addresses go in group_vars or ansible_* vars, never here.\n")
inv_path.write_text(header + yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
print(f"  inventory: {inv_path}")
print(f"  enrolled host '{local}' under group '{os_name}' ({'added' if added else 'already present'})")
print(f"  fleet hosts: " + ", ".join(sorted(h for g in fleet.values()
                                            for h in (g.get('hosts', {}) or {}))))
PYCODE

# --- 5b. Tailscale tailnet reachability (private) -------------------------
# Auto-enrolls only if you've provided an auth key (a secret we never commit):
#   - env var TS_AUTH_KEY, or
#   - the file ~/.config/herdr-engineering/tailscale-auth.key  (gitignored)
# Otherwise it prints the exact command and continues (never blocks, never
# writes a secret to the repo). Requires sudo when not running as root.
step "5b/7 Tailscale (private tailnet reachability)"
TS_KEY_FILE="$HOME/.config/herdr-engineering/tailscale-auth.key"
if command -v tailscale >/dev/null 2>&1; then
  if tailscale status >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null || true)"
    TS_HOST="$(tailscale hostname 2>/dev/null || hostname)"
    printf '  already on tailnet: %s (%s) — nothing to do\n' "${TS_IP:-?}" "${TS_HOST:-?}"
  else
    TS_KEY="${TS_AUTH_KEY:-}"
    if [ -z "$TS_KEY" ] && [ -f "$TS_KEY_FILE" ]; then
      TS_KEY="$(tr -d '[:space:]' < "$TS_KEY_FILE")"
    fi
    if [ -n "$TS_KEY" ]; then
      if [ "$(id -u)" = "0" ]; then
        SUDO=""
      elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
      else
        SUDO=""
      fi
      if [ -n "$SUDO" ] || [ "$(id -u)" = "0" ]; then
        if $SUDO tailscale up --authkey="$TS_KEY" >/dev/null 2>&1; then
          printf '  enrolled on tailnet (tailscale up)\n'
        else
          warn "tailscale up failed — run manually: sudo tailscale up --authkey=***"
        fi
      else
        warn "no sudo available to run 'tailscale up'. Run manually: sudo tailscale up --authkey=***"
      fi
    else
      warn "tailscale is installed but not up."
      warn "  1) get a key: https://login.tailscale.com/admin/settings/security"
      warn "  2) export TS_AUTH_KEY=tskey-... and re-run, OR"
      warn "     save it to $TS_KEY_FILE, OR"
      warn "     run now: sudo tailscale up --authkey=***"
    fi
  fi
else
  warn "tailscale not installed. Install it, then 'sudo tailscale up --authkey=***'"
  warn "  Debian/Ubuntu: sudo apt-get install -y tailscale && sudo systemctl enable --now tailscaled"
  warn "  macOS:         brew install --cask tailscale"
fi

# --- 6. put herdr-eng on PATH ---------------------------------------------
step "6/7 making herdr-eng available on PATH"
LINK_DIR="$HOME/.local/bin"
if [ -d "$LINK_DIR" ] || mkdir -p "$LINK_DIR" 2>/dev/null; then
  if command -v python3 >/dev/null 2>&1 && case "$PATH" in *"$LINK_DIR"*) true;; *) false;; esac; then
    ln -sf "$VENV/bin/herdr-eng" "$LINK_DIR/herdr-eng"
    printf '  linked %s -> %s\n' "$LINK_DIR/herdr-eng" "$VENV/bin/herdr-eng"
  else
    warn "$LINK_DIR is not on PATH; activate the venv instead: source $VENV/bin/activate"
  fi
else
  warn "could not create $LINK_DIR; use: source $VENV/bin/activate"
fi

# --- 7. doctor ------------------------------------------------------------
step "7/7 running herdr-eng doctor"
"$PY" -m herdr_engineering.cli doctor || warn "doctor reported warnings (see above)"

step "Done."
cat <<EOF

  HerdR Engineering is set up on this machine.

  Try:
    herdr-eng doctor            # health/capability/security checks
    herdr-eng name --prompt "add a health endpoint"
    herdr-eng machines --json   # read-only view of saved Herdr machines
    herdr-eng web --port 8787   # private web/mobile control surface

  This machine is now in the private inventory at: $INVENTORY
  Herdr was NOT modified; your running agent sessions are unaffected.
EOF
