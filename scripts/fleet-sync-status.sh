#!/usr/bin/env bash
#
# Read-only fleet status: report each host's herdr-eng version, build-gate
# extension checksum, fabric-agent state, and whether it is registered with the
# fabric gateway. Also cross-checks the gateway's view (which machines have
# leases) against the inventory. Makes NO changes.
#
#   scripts/fleet-sync-status.sh [--host <h>] [--json]
#
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY="$REPO_ROOT/ansible/inventory/hosts.yml"
HOST=""
JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --json) JSON=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v ansible-playbook >/dev/null 2>&1 || { echo "ansible-playbook not found on the controller." >&2; exit 1; }

# Gateway's view of registered machines (leases).
GW_JSON="$(herdr-eng devfabric list --json 2>/dev/null || echo '[]')"
machines() { printf '%s' "$GW_JSON" | python3 -c "import sys,json;[print(x.get('target',{}).get('machine_id','')) for x in json.load(sys.stdin)]" 2>/dev/null; }

if [ "$JSON" = 1 ]; then
  ansible-playbook -i "$INVENTORY" "$REPO_ROOT/ansible/playbooks/fleet-status.yml" ${HOST:+-l "$HOST"} --json \
    >/dev/null 2>&1 || true
  echo "{\"gateway_machines\": [$(machines | sed 's/.*/"&"/' | paste -sd, -)]}"
  exit 0
fi

printf '%-12s | %-9s | %-10s | %-8s | %s\n' "HOST" "herdr-eng" "build-gate" "agent" "registered"
printf -- '--------------------------------------------------------------------------------\n'

# Run the read-only playbook and reformat its debug "msg" lines into a table.
ansible-playbook -i "$INVENTORY" "$REPO_ROOT/ansible/playbooks/fleet-status.yml" ${HOST:+-l "$HOST"} 2>&1 \
  | sed -n 's/.*msg": "\([^"]*\)".*/\1/p' \
  | while IFS='|' read -r h hv bg ag; do
      h=$(echo "$h" | xargs); hv=$(echo "$hv" | xargs); bg=$(echo "$bg" | xargs); ag=$(echo "$ag" | xargs)
      if machines | grep -qx "$h"; then reg="yes"; else reg="no"; fi
      printf '%-12s | %-9s | %.8s | %-8s | %s\n' "$h" "$hv" "$bg" "$ag" "$reg"
    done
