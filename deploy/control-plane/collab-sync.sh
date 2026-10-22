#!/usr/bin/env bash
# Refresh the files the (credential-less, offline) collab container reads:
#   jwks.json    - the Cognito pool's signing keys
#   members.json - usernames in the required group, stamped with the refresh time
# Runs on the host with the operator's AWS credentials; the container only sees the output.
# Usage: collab-sync.sh once | install
set -euo pipefail
POOL="${HERDR_COLLAB_POOL:-us-west-2_GjtcM0PCp}"
GROUP="${HERDR_COLLAB_GROUP:-local.dev}"
REGION="${POOL%%_*}"
DIR="${HERDR_COLLAB_SYNC_DIR:-$HOME/.local/share/herdr-engineering/collab-sync}"

once() {
  mkdir -p "$DIR"; chmod 755 "$DIR"
  local tmp; tmp="$(mktemp -d "$DIR/.tmp.XXXXXX")"; trap 'rm -rf "$tmp"' RETURN
  curl -fsS -m 10 "https://cognito-idp.${REGION}.amazonaws.com/${POOL}/.well-known/jwks.json" -o "$tmp/jwks.json"
  python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["keys"]' "$tmp/jwks.json"
  aws cognito-idp list-users-in-group --region "$REGION" --user-pool-id "$POOL" --group-name "$GROUP" \
    --query 'Users[].Username' --output json > "$tmp/users.json"
  python3 - "$tmp/users.json" "$tmp/members.json" "$GROUP" <<'PY'
import json, sys, time
users = json.load(open(sys.argv[1]))
json.dump({"updated": time.time(), "groups": {sys.argv[3]: users}}, open(sys.argv[2], "w"))
PY
  chmod 644 "$tmp"/*.json
  mv -f "$tmp/jwks.json" "$DIR/jwks.json"; mv -f "$tmp/members.json" "$DIR/members.json"   # atomic per file
  certs
}

# Hand the edge ONLY its own certificate. Mounting certbot's whole config dir would give the
# internet-facing container every lineage's private key and the ACME account key.
certs() {
  local src="${HERDR_COLLAB_CERT_SRC:-$HOME/.local/share/inferweave/bender-failover/acme/config/live/localdev.mcp.metabolomics.us}"
  local dst="${HERDR_COLLAB_CERT_DIR:-$HOME/.local/share/herdr-engineering/collab-edge-certs}" changed=0 f
  mkdir -p "$dst"; chmod 700 "$dst"
  for f in fullchain.pem privkey.pem; do
    if ! cmp -s "$src/$f" "$dst/$f"; then
      install -m 0600 "$src/$f" "$dst/$f.new"; mv -f "$dst/$f.new" "$dst/$f"; changed=1
    fi
  done
  # renewal starts 30 days out; still unrenewed at 14 days means it is failing: say so in the journal
  openssl x509 -in "$dst/fullchain.pem" -noout -checkend $((14 * 86400)) >/dev/null \
    || echo "WARNING: collab edge certificate expires within 14 days; check herdr-collab-cert-renew" >&2
  if [ "$changed" = 1 ] && docker inspect herdr-collab-edge >/dev/null 2>&1; then
    docker exec herdr-collab-edge nginx -s reload || true
  fi
}

# Renews ONLY this lineage. The old bender-failover renew timer was stopped when that TLS front was
# retired, and its deploy hook execs into that stopped container, so nothing renewed this cert.
# No deploy hook is needed: the minute timer above copies a renewed cert to the edge and reloads it.
ACME="${HERDR_COLLAB_ACME_DIR:-$HOME/.local/share/inferweave/bender-failover/acme}"
CERTBOT="${HERDR_COLLAB_CERTBOT:-$ACME/../acme-venv/bin/certbot}"
CERT_NAME="${HERDR_COLLAB_CERT_NAME:-localdev.mcp.metabolomics.us}"

install_renew_timer() {
  local u="$HOME/.config/systemd/user"
  [ -x "$CERTBOT" ] || { echo "certbot not found at $CERTBOT (set HERDR_COLLAB_CERTBOT)" >&2; return 1; }
  cat > "$u/herdr-collab-cert-renew.service" <<UNIT
[Unit]
Description=Renew the collab edge TLS certificate
After=network-online.target
[Service]
Type=oneshot
UMask=0077
TimeoutStartSec=15min
ExecStart=$CERTBOT renew --non-interactive --cert-name $CERT_NAME --config-dir $ACME/config --work-dir $ACME/work --logs-dir $ACME/logs
UNIT
  cat > "$u/herdr-collab-cert-renew.timer" <<UNIT
[Unit]
Description=Check collab edge certificate renewal twice daily
[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-collab-cert-renew.timer
}

certbot_dry_run() {   # proves the renewal path end to end against Let's Encrypt staging
  "$CERTBOT" renew --dry-run --non-interactive --cert-name "$CERT_NAME" --config-dir "$ACME/config" --work-dir "$ACME/work" --logs-dir "$ACME/logs"
}

install_timer() {
  local u="$HOME/.config/systemd/user" bin="$HOME/.local/share/herdr-engineering/collab-sync.sh"
  mkdir -p "$u"; install -m 0755 "$0" "$bin"   # stable copy: deploy.sh runs from a temporary worktree
  cat > "$u/herdr-collab-sync.service" <<UNIT
[Unit]
Description=Refresh collab Cognito membership and keys
[Service]
Type=oneshot
ExecStart=$bin once
UNIT
  cat > "$u/herdr-collab-sync.timer" <<UNIT
[Unit]
Description=Refresh collab Cognito membership every minute
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-collab-sync.timer
  install_renew_timer
}

case "${1:-once}" in once) once ;; install) once; install_timer ;; renew-check) certbot_dry_run ;; *) echo "usage: $0 once|install|renew-check" >&2; exit 2 ;; esac
