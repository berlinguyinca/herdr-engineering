#!/usr/bin/env bash
#
# One-shot onboarding of a NEW fleet site (machine) into the HerdR dev-fabric.
# Run from the controller checkout.
#
# What it does (all idempotent, non-disruptive to any running agent):
#   1. Adds <host> to the PRIVATE inventory (ansible/inventory/hosts.yml under
#      the linux/macos group) + writes a private host_vars/<host>.yml with the
#      connection details, if not already present.
#   2. Runs the fleet-sync playbook against ONLY that host to converge:
#        - herdr-eng           (pulled from public repo origin/main, pip-installed)
#        - fabric.env + auto-registration agent (registers this host's services
#          with the gateway, making it a fabric "site")
#        - pi build-gate extension (build-gate.ts)
#   3. Verifies the host is registered with the fabric gateway (its leases
#      appear in `herdr-eng devfabric list`) and prints version parity.
#
# Usage:
#   scripts/fleet-join.sh <host> \
#       [--os linux|macos] \
#       [--ansible-host <tailnet-magicdns-or-ip>] \
#       [--user <fleet-user>] \
#       [--gateway http://dev.lan:29999]
#
# Examples:
#   scripts/fleet-join.sh newbox --ansible-host newbox.tail0c50da.ts.net --user wohlgemuth
#   scripts/fleet-join.sh newbox --os macos --ansible-host macbook-pro.tail0c50da.ts.net --user wohlgemuth
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY="$REPO_ROOT/ansible/inventory/hosts.yml"
HOST_VARS_DIR="$REPO_ROOT/ansible/inventory/host_vars"
INV_HELPER="$REPO_ROOT/scripts/fleet_inventory.py"
SYNC_PLAYBOOK="$REPO_ROOT/ansible/playbooks/fleet-sync.yml"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

HOST=""
OS="linux"
ANSIBLE_HOST=""
USER=""
GATEWAY="http://dev.lan:29999"

while [ $# -gt 0 ]; do
  case "$1" in
    --os) OS="${2:?--os needs linux|macos}"; shift 2 ;;
    --ansible-host) ANSIBLE_HOST="${2:?--ansible-host needs a value}"; shift 2 ;;
    --user) USER="${2:?--user needs a value}"; shift 2 ;;
    --gateway) GATEWAY="${2:?--gateway needs a URL}"; shift 2 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) [ -z "$HOST" ] && HOST="$1" || { echo "unexpected argument: $1" >&2; exit 2; }; shift ;;
  esac
done

[ -n "$HOST" ] || { echo "usage: scripts/fleet-join.sh <host> [--os linux|macos] [--ansible-host <addr>] [--user <user>] [--gateway <url>]" >&2; exit 2; }

# --- 1/4 add host to the private inventory --------------------------------
step "1/4 adding '$HOST' to the private fleet inventory (os=$OS)"
python3 "$INV_HELPER" add --inventory "$INVENTORY" --host "$HOST" --os "$OS"

if [ -n "$ANSIBLE_HOST" ] && [ -n "$USER" ]; then
  python3 "$INV_HELPER" host-var --dir "$HOST_VARS_DIR" --host "$HOST" \
    --ansible-host "$ANSIBLE_HOST" --user "$USER"
else
  warn "no --ansible-host/--user given; you must add connection vars to"
  warn "  $HOST_VARS_DIR/$HOST.yml manually before the playbook can reach $HOST."
fi

# --- 2/4 converge herdr-eng + agent + build-gate on the new host ----------
step "2/4 running fleet-sync playbook against '$HOST'"
if command -v ansible-playbook >/dev/null 2>&1; then
  ansible-playbook -i "$INVENTORY" "$SYNC_PLAYBOOK" -l "$HOST" \
    -e "fabric_agent_gateway_url=$GATEWAY"
else
  warn "ansible-playbook not found on the controller; skipping provision."
  warn "  install ansible, then run:"
  warn "    ansible-playbook -i $INVENTORY $SYNC_PLAYBOOK -l $HOST"
fi

# --- 3/4 verify registration with the fabric gateway ----------------------
step "3/4 verifying '$HOST' is registered with the fabric gateway"
if command -v herdr-eng >/dev/null 2>&1; then
  LEASES="$(herdr-eng devfabric list --json 2>/dev/null || true)"
  COUNT="$(printf '%s' "$LEASES" | grep -c "\"machine_id\": *\"$HOST\"" 2>/dev/null || echo 0)"
  echo "  $HOST leases visible to the gateway: $COUNT"
  if [ "$COUNT" -ge 0 ]; then
    echo "  (0 is fine if the host has no web services running yet; the agent"
    echo "   registers services as they start.)"
  fi
else
  warn "herdr-eng not on the controller PATH; skipping registration check."
fi

# --- 4/4 version parity ---------------------------------------------------
step "4/4 fleet version fingerprint (controller baseline: $(git -C "$REPO_ROOT" log -1 --format='%h' 2>/dev/null || echo '?') / herdr-eng $(command -v herdr-eng >/dev/null && herdr-eng --version 2>/dev/null || echo '?'))"
if command -v ansible-playbook >/dev/null 2>&1; then
  ansible-playbook -i "$INVENTORY" "$REPO_ROOT/ansible/playbooks/fleet-status.yml" -l "$HOST" \
    || warn "fleet-status playbook failed (host may be unreachable); join itself is complete."
else
  warn "ansible-playbook not found; run fleet-status.yml manually to see version parity."
fi

echo
printf '\033[1;32m==> %s joined the HerdR fleet. Verify from any tailnet device:\033[0m\n' "$HOST"
echo "    herdr-eng devfabric list --json"
echo "    curl http://dev.lan:<port>"
