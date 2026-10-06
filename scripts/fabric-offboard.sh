#!/usr/bin/env bash
#
# Remove THIS machine from the shared dev-fabric fleet cleanly and idempotently.
# Cross-platform: stops the local auto-registration agent (systemd USER unit on
# Linux, launchd LaunchAgent on macOS), closes this host's leases on the
# gateway, and drops it from the private fleet inventory. Leases that cannot be
# closed immediately expire via the gateway's probe loop (TTL ~90s), so ports
# are always freed even if this host is already gone.
#
# What it does (best-effort at every step; safe to re-run):
#   1. Stops + removes the local agent unit/agent:
#        Linux: systemctl --user disable --now herdr-eng-fabric-agent.service
#        macOS: launchctl bootout gui/<uid>/com.herdr-engineering.fabric-agent
#   2. Closes every lease whose target is THIS host (via `herdr-eng devfabric
#      close`, so control-API bearer-token auth is handled), if herdr-eng and
#      a gateway are available.
#   3. Removes this host from ansible/inventory/hosts.yml (the private fleet
#      inventory) using a Python with PyYAML if available, else a targeted sed.
#
# Flags:
#   --host <name>    offboard a different host by name (default: this machine)
#   --gateway <url>  gateway control URL (default: fabric.env, else http://dev.lan:29999)
#   --leave-tailnet  also run `tailscale logout` to remove the device from the tailnet
#   --purge          also remove machine-local agent config + state
#
# Usage:
#   scripts/fabric-offboard.sh [--host <name>] [--gateway <url>]
#       [--leave-tailnet] [--purge]
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY="$REPO_ROOT/ansible/inventory/hosts.yml"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-engineering/fabric.env"
STATE_DIR="${HERDR_ENGINEERING_STATE:-$HOME/.local/share/herdr-engineering/state}"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

HOSTNAME="$(hostname -s 2>/dev/null || hostname)"
GATEWAY="http://dev.lan:29999"
LEAVE_TAILNET=0
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host)   HOSTNAME="${2:?--host needs a name}"; shift 2 ;;
    --gateway) GATEWAY="${2:?--gateway needs a URL}"; shift 2 ;;
    --leave-tailnet) LEAVE_TAILNET=1; shift ;;
    --purge)  PURGE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -f "$ENV_FILE" ]; then
  GATEWAY="$(grep '^HERDR_ENGINEERING_FABRIC_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)"
  GATEWAY="${GATEWAY:-http://dev.lan:29999}"
fi

echo "Offboarding host '$HOSTNAME' from the dev fabric (gateway $GATEWAY)"

# --- 1. stop + remove the local agent ---------------------------------------
step "1/4 stopping the local auto-registration agent"
case "$(uname -s)" in
  Linux)
    if systemctl --user is-active herdr-eng-fabric-agent.service >/dev/null 2>&1 \
       || [ -f "$HOME/.config/systemd/user/herdr-eng-fabric-agent.service" ]; then
      systemctl --user disable --now herdr-eng-fabric-agent.service >/dev/null 2>&1 || true
      rm -f "$HOME/.config/systemd/user/herdr-eng-fabric-agent.service"
      systemctl --user daemon-reload 2>/dev/null || true
      echo "  removed systemd user unit herdr-eng-fabric-agent.service"
    else
      echo "  no systemd agent present (nothing to do)"
    fi
    ;;
  Darwin)
    LABEL="com.herdr-engineering.fabric-agent"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      echo "  stopped + removed launchd agent $LABEL"
    else
      echo "  no launchd agent present (nothing to do)"
    fi
    rm -f "$PLIST" "$HOME/bin/herdr-fabric-agent.sh"
    ;;
  *) warn "unsupported OS $(uname -s); skipping agent removal" ;;
esac

# --- 2. close this host's leases on the gateway -----------------------------
step "2/4 closing $HOSTNAME's leases on the gateway"
HERDR_ENG="$(command -v herdr-eng 2>/dev/null || true)"
[ -x "$HERDR_ENG" ] || HERDR_ENG="$HOME/.venvs/herdr-engineering/bin/herdr-eng"
if [ -x "$HERDR_ENG" ] && curl -fsS -m 3 "$GATEWAY/healthz" >/dev/null 2>&1; then
  # Resolve the offboarded host's tailnet IPv4 via `tailscale status` (works
  # for this host and for a remote host). target.host is a tailnet IPv4, so
  # leases are matched by that IP alone.
  TS_IP="$(tailscale status 2>/dev/null \
    | awk -v h="$HOSTNAME" '$2==h || $2 ~ ("^" h "\\.") {print $1; exit}')"
  if [ -z "$TS_IP" ]; then
    warn "could not resolve '$HOSTNAME' in 'tailscale status'; its leases will expire via the probe loop (TTL ~90s)."
  else
    python3 - "$HERDR_ENG" "$HOSTNAME" "$TS_IP" <<'PY'
import json, subprocess, sys
herdr, hostname, ts_ip = sys.argv[1:4]
try:
    out = subprocess.run([herdr, "devfabric", "list", "--json"],
                         capture_output=True, text=True, timeout=20)
    leases = (json.loads(out.stdout) or {}).get("leases", [])
except Exception as exc:  # pragma: no cover
    print("  could not list leases:", exc); sys.exit(0)
mine = [l for l in leases if (l.get("target") or {}).get("host") == ts_ip]
if not mine:
    print(f"  no active leases found for {hostname} ({ts_ip})")
    sys.exit(0)
for l in mine:
    lid = l["id"]
    r = subprocess.run([herdr, "devfabric", "close", lid],
                       capture_output=True, text=True, timeout=20)
    print(f"  closed {lid} (dev port {l.get('external_port')} -> "
          f"{l.get('target',{}).get('host')}:{l.get('target',{}).get('port')})"
          + ("" if r.returncode == 0 else f"  [{r.stderr.strip()}]"))
PY
  fi
else
  warn "herdr-eng unavailable or gateway unreachable — leases will expire via the probe loop (TTL ~90s)."
fi

# --- 3. remove from the private fleet inventory -----------------------------
step "3/4 removing '$HOSTNAME' from the fleet inventory"
if [ -f "$INVENTORY" ]; then
  PY=""
  for cand in "$HOME/.venvs/herdr-engineering/bin/python" python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import yaml' >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  done
  if [ -n "$PY" ]; then
    "$PY" - "$INVENTORY" "$HOSTNAME" <<'PY'
import sys, yaml
path, host = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(path)) or {}
removed = []
for group in ("linux", "macos"):
    hosts = (data.get("all", {}).get("children", {})
                 .get("herdr_fleet", {}).get("children", {})
                 .get(group, {}).get("hosts", {}))
    if host in hosts:
        del hosts[host]; removed.append(group)
if removed:
    header = ("# Private fleet inventory (managed by scripts/install.sh; NOT committed).\n"
              "# Connection users/addresses go in group_vars or ansible_* vars, never here.\n")
    open(path, "w").write(header + yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
    print(f"  removed '{host}' from group(s): {', '.join(removed)}")
else:
    print(f"  '{host}' not in inventory (nothing to do)")
PY
  else
    # Fallback: remove the `hostname: {}` line under a hosts map (targeted sed).
    sed -i.bak "/^[[:space:]]*${HOSTNAME}[[:space:]]*:/d" "$INVENTORY" 2>/dev/null \
      && rm -f "$INVENTORY.bak" \
      && echo "  removed '$HOSTNAME' line from inventory (sed fallback)" \
      || warn "  could not edit $INVENTORY — remove '$HOSTNAME' manually."
  fi
else
  warn "no inventory at $INVENTORY (nothing to edit)"
fi

# --- 4. optional: leave tailnet / purge local state -------------------------
if [ "$LEAVE_TAILNET" = "1" ]; then
  step "4/4 leaving the tailnet (tailscale logout)"
  if [ "$(id -u)" = "0" ]; then SUDO=""; else SUDO="sudo"; fi
  if command -v tailscale >/dev/null 2>&1; then
    $SUDO tailscale logout && echo "  logged out of the tailnet" \
      || warn "  tailscale logout failed — remove the device in the admin console if needed."
  else
    warn "  tailscale not found; skip."
  fi
fi

if [ "$PURGE" = "1" ]; then
  step "4/4 purging machine-local agent state"
  rm -f "$ENV_FILE"
  rm -rf "$STATE_DIR/fabric-agent.json"
  echo "  removed $ENV_FILE"
  echo "  removed $STATE_DIR/fabric-agent.json"
fi

step "done. '$HOSTNAME' is off the dev fabric."
echo "  Remaining leases (if any) expire via the gateway probe loop within ~90s."
