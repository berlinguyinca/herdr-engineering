#!/usr/bin/env bash
# Host-side certificate upkeep for the fabric gateway's HTTPS on 443.
#   install   create the private CA (dev.lan), obtain missing certificates, install the hourly user timer
#   renew     renew whatever is due (public Let's Encrypt cert via Route 53, this node's ts.net cert)
# The gateway container only reads the files (it has no certbot / tailscale CLI) and reloads them when they change.
# Needs: herdr-eng with the [acme] extra on PATH (or HERDR_ENG=...), AWS credentials able to change
# _acme-challenge TXT records, and `tailscale cert` permission (run as the tailscale operator or root).
set -euo pipefail

ENG="${HERDR_ENG:-herdr-eng}"
# Unattended renewal uses its OWN least-privilege AWS key (deploy/fabric-stack/iam-acme-renewal-policy.json), never the
# operator's personal credentials. Create the file with a [default] profile holding that user's key.
ACME_AWS_CREDS="${FABRIC_ACME_AWS_CREDENTIALS:-$HOME/.config/herdr-engineering/aws-acme-credentials}"
DOMAIN="${FABRIC_ACME_DOMAIN:-local.metabolomics.us}"
TS_NAME="${FABRIC_TS_NAME:-$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)}"
BASE="$HOME/.config/herdr-engineering"
say() { printf '\033[1;34m%s\033[0m\n' "$*"; }

renew() {
  if [ -r "$ACME_AWS_CREDS" ]; then
    AWS_SHARED_CREDENTIALS_FILE="$ACME_AWS_CREDS" AWS_CONFIG_FILE=/dev/null AWS_DEFAULT_REGION=us-east-1 \
      "$ENG" devfabric cert renew --domain "$DOMAIN"
  elif [ "${FABRIC_ACME_ALLOW_AMBIENT_AWS:-0}" = 1 ]; then
    "$ENG" devfabric cert renew --domain "$DOMAIN"        # operator-run install only: the operator's own credentials
  else
    # Fail closed: a timer must never quietly fall back to a personal or broader key.
    echo "ERROR: $ACME_AWS_CREDS is missing or unreadable; refusing to renew with ambient AWS credentials" >&2
    return 1
  fi
  if [ -n "$TS_NAME" ]; then
    mkdir -p "$BASE/tailnet-cert"; chmod 700 "$BASE/tailnet-cert"
    tailscale cert --cert-file "$BASE/tailnet-cert/$TS_NAME.crt" --key-file "$BASE/tailnet-cert/$TS_NAME.key" "$TS_NAME"
    chmod 600 "$BASE/tailnet-cert/$TS_NAME.key"
  fi
  # private CA leaf: reissue when within 30 days of expiry
  if "$ENG" devfabric ca status | grep -q '"renewal_due": true'; then "$ENG" devfabric ca issue >/dev/null; fi
}

install_timer() {
  local u="$HOME/.config/systemd/user"; mkdir -p "$u"
  cat > "$u/herdr-fabric-certs.service" <<UNIT
[Unit]
Description=Renew HerdR fabric gateway certificates
[Service]
Type=oneshot
Environment=HERDR_ENG=$ENG
Environment=FABRIC_ACME_AWS_CREDENTIALS=$ACME_AWS_CREDS
ExecStart=$(readlink -f "$0") renew
UNIT
  cat > "$u/herdr-fabric-certs.timer" <<UNIT
[Unit]
Description=Check HerdR fabric certificates hourly
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
RandomizedDelaySec=300
[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-fabric-certs.timer
}

case "${1:-}" in
  install)
    "$ENG" devfabric ca status | grep -q '"ca": true' || "$ENG" devfabric ca init
    [ -f "$BASE/ca/gateway.pem" ] || "$ENG" devfabric ca issue
    "$ENG" devfabric cert issue --domain "$DOMAIN"
    FABRIC_ACME_ALLOW_AMBIENT_AWS=1 renew
    install_timer
    say "installed. Export the dev.lan root for devices with: $ENG devfabric ca export --out <dir>"
    ;;
  renew) renew ;;
  *) echo "usage: $0 install|renew" >&2; exit 2 ;;
esac
