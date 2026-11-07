#!/usr/bin/env bash
# Egress lock for the collab `fabric` docker network (docs/operations/fabric-trust.md, "Required hardening").
#
# collab-public must sit on a NON-internal network, or Docker cannot publish the fabric listener's port. A non-internal
# network gives its containers a default route, so a compromised collab-public could open connections to the host's own
# addresses (tailnet, LAN, loopback-bound services via the bridge gateway), to the LAN/internet, and to other containers'
# published ports. This script drops every NEW connection that ORIGINATES from the fabric subnet:
#   * DOCKER-USER  forwarded traffic (LAN, internet, other docker networks)
#   * INPUT        traffic to the host itself (DOCKER-USER never sees that; it is the operator plane's exposure)
# Inbound connections to the published port are unaffected (their source is the caller, not the subnet) and so are their
# replies (not NEW). The network is IPv4 only (no enable_ipv6), so no ip6tables rule exists to write.
#
#   collab-fabric-firewall.sh --install | --check | --remove  [--subnet CIDR] [--iptables CMD] [--dry-run]
#
# --check exits 0 when every rule is present, 1 when one is absent, 2 when the rules cannot be read (not root).
# Rules live in the kernel and vanish on reboot: run --install again at boot (a oneshot unit After=docker.service).
set -euo pipefail

SUBNET="172.30.213.0/24"   # must equal the subnet pinned in compose.collab-fabric.yaml (a test enforces it)
IPT=iptables
MODE=""
DRY=0
TAG="herdr-collab-fabric"
CHAINS=(DOCKER-USER INPUT)

usage() {
  cat <<USAGE
usage: collab-fabric-firewall.sh (--install | --check | --remove) [--subnet CIDR (default $SUBNET)] [--iptables CMD] [--dry-run]
USAGE
}
die() { echo "collab-fabric-firewall: $*" >&2; exit 64; }

while [ $# -gt 0 ]; do
  case "$1" in
    --install|--check|--remove)
      [ -z "$MODE" ] || die "give exactly one of --install, --check, --remove"
      MODE="${1#--}"; shift ;;
    --subnet) [ $# -ge 2 ] || die "--subnet needs a value"; SUBNET="$2"; shift 2 ;;
    --iptables) [ $# -ge 2 ] || die "--iptables needs a value"; IPT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
[ -n "$MODE" ] || { usage >&2; die "a mode is required"; }
[[ "$SUBNET" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || die "not an IPv4 CIDR: $SUBNET"

rule=(-s "$SUBNET" -m conntrack --ctstate NEW -m comment --comment "$TAG" -j DROP)

# present <chain>: 0 present, 1 absent, 2 cannot tell
present() {
  local rc=0
  "$IPT" -C "$1" "${rule[@]}" >/dev/null 2>&1 || rc=$?
  case "$rc" in 0) return 0 ;; 1) return 1 ;; *) return 2 ;; esac
}

run() {
  if [ "$DRY" = 1 ]; then echo "[dry-run] $IPT $*"; else "$IPT" "$@"; fi
}

status=0
for chain in "${CHAINS[@]}"; do
  rc=0; present "$chain" || rc=$?
  case "$MODE" in
    check)
      case "$rc" in
        0) echo "$chain: present" ;;
        1) echo "$chain: absent"; [ "$status" = 2 ] || status=1 ;;
        *) echo "cannot read the $chain rules (need root and $IPT)" >&2; status=2 ;;
      esac ;;
    install)
      [ "$rc" != 2 ] || { echo "cannot read the $chain rules (need root and $IPT)" >&2; exit 2; }
      if [ "$rc" = 1 ]; then run -I "$chain" 1 "${rule[@]}"; echo "$chain: installed"; else echo "$chain: already present"; fi ;;
    remove)
      [ "$rc" != 2 ] || { echo "cannot read the $chain rules (need root and $IPT)" >&2; exit 2; }
      if [ "$DRY" = 1 ]; then
        [ "$rc" != 0 ] || echo "[dry-run] $IPT -D $chain ${rule[*]}"
      else
        while present "$chain"; do "$IPT" -D "$chain" "${rule[@]}"; done
      fi
      echo "$chain: removed" ;;
  esac
done
exit "$status"
