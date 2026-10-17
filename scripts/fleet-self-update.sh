#!/usr/bin/env bash
#
# Self-update the whole HerdR fleet to the current version, and verify parity.
# Run from the controller checkout.
#
# What it does (idempotent, non-disruptive to running agents):
#   1. Brings the controller checkout up to date (`git pull --ff-only`) and
#      rebuilds/installs the local herdr-eng, so the controller's build-gate.ts
#      and scripts match what will be pushed to the fleet.
#   2. Runs the fleet-sync playbook against every inventory host (or just
#      `--host <h>`): each host pulls the public repo origin/main and reinstalls
#      herdr-eng, refreshes fabric.env + the auto-registration agent, and
#      re-copies the build-gate extension.
#   3. Runs the read-only fleet-status playbook and reports version parity.
#
# Usage:
#   scripts/fleet-self-update.sh [--host <h>] [--no-pull] [--no-local-reinstall]
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY="$REPO_ROOT/ansible/inventory/hosts.yml"
HOST=""
PULL=1
REINSTALL_LOCAL=1
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --no-pull) PULL=0; shift ;;
    --no-local-reinstall) REINSTALL_LOCAL=0; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

# --- 1/3 controller self-update ------------------------------------------
step "1/3 updating the controller checkout + local herdr-eng"
if [ "$PULL" = 1 ] && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$REPO_ROOT" pull --ff-only --quiet || warn "git pull failed (uncommitted changes?); continuing with local checkout."
fi
if [ "$REINSTALL_LOCAL" = 1 ] && [ -f "$REPO_ROOT/pyproject.toml" ]; then
  python3 -m pip install --quiet --upgrade "$REPO_ROOT" 2>/dev/null \
    && echo "  local herdr-eng: $(herdr-eng --version 2>/dev/null || echo '?')" \
    || warn "local pip install failed; continuing with existing herdr-eng."
fi

# --- 2/3 converge the fleet (or one host) ---------------------------------
step "2/3 running fleet-sync playbook (${HOST:-all inventory hosts})"
command -v ansible-playbook >/dev/null 2>&1 || { echo "ansible-playbook not found on the controller." >&2; exit 1; }
if [ -n "$HOST" ]; then
  ansible-playbook -i "$INVENTORY" "$REPO_ROOT/ansible/playbooks/fleet-sync.yml" -l "$HOST"
else
  ansible-playbook -i "$INVENTORY" "$REPO_ROOT/ansible/playbooks/fleet-sync.yml"
fi

# --- 3/3 verify parity ----------------------------------------------------
step "3/3 fleet version parity (read-only)"
ansible-playbook -i "$INVENTORY" "$REPO_ROOT/ansible/playbooks/fleet-status.yml" ${HOST:+-l "$HOST"}
echo
echo "Controller baseline: $(git -C "$REPO_ROOT" log -1 --format='%h %s' 2>/dev/null || echo '?')"
echo "All hosts should report the same herdr-eng version and build-gate checksum."
