#!/usr/bin/env bash
# Refresh the files the (credential-less, offline) collab container reads:
#   jwks.json    - the Cognito pool's signing keys
#   members.json - usernames in the required group, stamped with the refresh time
# Runs on the host with the operator's AWS credentials; the container only sees the output.
# The pool and group come from the central fleet config (control_plane.collab), via the
# slice deploy.sh renders with `herdr-eng fleet config render-collab`.
# It also hands the edge its TLS certificate (certs) and, on install, a renew timer for that
# one certbot lineage. Paths are flags (no environment): deploy.sh passes --cert-dir from
# control_plane.compose.HERDR_COLLAB_CERT_DIR (the directory collab-edge mounts), and install
# writes every flag into the units' ExecStart.
# The operator invite provisioner (collab-provision.py) gets the operator, pool and group from
# the same slice, and reads collab-public's log under --data-dir (the directory collab-public
# mounts at /data; deploy.sh passes it).
# Usage: collab-sync.sh once|install|renew-check [--config SLICE] [--dir SYNC_DIR] [--data-dir DIR]
#          [--cert-dir DIR] [--cert-src LIVE_DIR] [--acme-dir DIR] [--certbot BIN] [--cert-name NAME]
set -euo pipefail
USAGE="usage: $0 once|install|renew-check [--config SLICE] [--dir SYNC_DIR] [--data-dir DIR] [--cert-dir DIR] [--cert-src LIVE_DIR] [--acme-dir DIR] [--certbot BIN] [--cert-name NAME]"
ACTION="${1:-once}"; [ $# -gt 0 ] && shift
CONFIG="$HOME/.config/herdr-engineering/collab/collab-config.json"
DIR="$HOME/.local/share/herdr-engineering/collab-sync"
DATA_DIR="$HOME/.local/share/herdr-engineering/collab"
CERT_DIR="$HOME/.local/share/herdr-engineering/collab-edge-certs"
CERT_NAME="localdev.mcp.metabolomics.us"
ACME="$HOME/.local/share/inferweave/bender-failover/acme"
CERT_SRC=""; CERTBOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="${2:?--config needs a value}"; shift 2 ;;
    --dir) DIR="${2:?--dir needs a value}"; shift 2 ;;
    --data-dir) DATA_DIR="${2:?--data-dir needs a value}"; shift 2 ;;
    --cert-dir) CERT_DIR="${2:?--cert-dir needs a value}"; shift 2 ;;
    --cert-src) CERT_SRC="${2:?--cert-src needs a value}"; shift 2 ;;
    --acme-dir) ACME="${2:?--acme-dir needs a value}"; shift 2 ;;
    --certbot) CERTBOT="${2:?--certbot needs a value}"; shift 2 ;;
    --cert-name) CERT_NAME="${2:?--cert-name needs a value}"; shift 2 ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
done
CERT_SRC="${CERT_SRC:-$ACME/config/live/$CERT_NAME}"
CERTBOT="${CERTBOT:-$ACME/../acme-venv/bin/certbot}"
read -r POOL GROUP OPERATOR < <(python3 - "$CONFIG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["sections"]["control_plane"]["values"]["collab"]
print(c["cognito_pool"], c.get("cognito_group") or "local.dev", c.get("operator_user") or "")
PY
) || { echo "cannot read control_plane.collab from $CONFIG (run deploy.sh)" >&2; exit 1; }
REGION="${POOL%%_*}"

# People the operator invites are added to the group here, on the host, because the public
# container has no AWS credentials. Runs first so a new member shows up in this very refresh.
provision() {
  local p; p="$(dirname "$0")/collab-provision.py"
  [ -f "$p" ] || p="$HOME/.local/share/herdr-engineering/collab-provision.py"
  [ -f "$p" ] || return 0
  [ -n "$OPERATOR" ] || return 0
  timeout 120 python3 "$p" --operator "$OPERATOR" --pool "$POOL" --group "$GROUP" --data-dir "$DATA_DIR" || echo "WARNING: collab-provision failed; membership refresh continues" >&2
}

once() {
  exec 9>"${DIR}.lock"; flock -n 9 || { echo "another collab-sync is running" >&2; return 0; }   # no overlapping runs
  provision
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
  local src="$CERT_SRC" dst="$CERT_DIR" changed=0 f
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
install_renew_timer() {
  local u="$HOME/.config/systemd/user"
  [ -x "$CERTBOT" ] || { echo "certbot not found at $CERTBOT (pass --certbot or --acme-dir)" >&2; return 1; }
  cat > "$u/herdr-collab-cert-renew.service" <<UNIT
[Unit]
Description=Renew the collab edge TLS certificate
After=network-online.target
[Service]
Type=oneshot
UMask=0077
TimeoutStartSec=15min
ExecStart="$CERTBOT" renew --non-interactive --cert-name "$CERT_NAME" --config-dir "$ACME/config" --work-dir "$ACME/work" --logs-dir "$ACME/logs"
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
  install -m 0755 "$(dirname "$0")/collab-provision.py" "$HOME/.local/share/herdr-engineering/collab-provision.py"
  cat > "$u/herdr-collab-sync.service" <<UNIT
[Unit]
Description=Refresh collab Cognito membership and keys
[Service]
Type=oneshot
ExecStart="$bin" once --config "$CONFIG" --dir "$DIR" --data-dir "$DATA_DIR" --cert-dir "$CERT_DIR" --cert-src "$CERT_SRC"
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

case "$ACTION" in once) once ;; install) once; install_timer ;; renew-check) certbot_dry_run ;; *) echo "$USAGE" >&2; exit 2 ;; esac
