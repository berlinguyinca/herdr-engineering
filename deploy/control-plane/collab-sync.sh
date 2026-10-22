#!/usr/bin/env bash
# Refresh the files the (credential-less, offline) collab container reads:
#   jwks.json    - the Cognito pool's signing keys
#   members.json - usernames in the required group, stamped with the refresh time
# Runs on the host with the operator's AWS credentials; the container only sees the output.
# The pool and group come from the central fleet config (control_plane.collab), via the
# slice deploy.sh renders with `herdr-eng fleet config render-collab`.
# Usage: collab-sync.sh once|install [--config SLICE] [--dir SYNC_DIR]
set -euo pipefail
ACTION="${1:-once}"; [ $# -gt 0 ] && shift
CONFIG="$HOME/.config/herdr-engineering/collab/collab-config.json"
DIR="$HOME/.local/share/herdr-engineering/collab-sync"
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="${2:?--config needs a value}"; shift 2 ;;
    --dir) DIR="${2:?--dir needs a value}"; shift 2 ;;
    *) echo "usage: $0 once|install [--config SLICE] [--dir SYNC_DIR]" >&2; exit 2 ;;
  esac
done
read -r POOL GROUP < <(python3 - "$CONFIG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["sections"]["control_plane"]["values"]["collab"]
print(c["cognito_pool"], c.get("cognito_group") or "local.dev")
PY
) || { echo "cannot read control_plane.collab from $CONFIG (run deploy.sh)" >&2; exit 1; }
REGION="${POOL%%_*}"

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
}

install_timer() {
  local u="$HOME/.config/systemd/user" bin="$HOME/.local/share/herdr-engineering/collab-sync.sh"
  mkdir -p "$u"; install -m 0755 "$0" "$bin"   # stable copy: deploy.sh runs from a temporary worktree
  cat > "$u/herdr-collab-sync.service" <<UNIT
[Unit]
Description=Refresh collab Cognito membership and keys
[Service]
Type=oneshot
ExecStart=$bin once --config $CONFIG --dir $DIR
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
}

case "$ACTION" in once) once ;; install) once; install_timer ;; *) echo "usage: $0 once|install" >&2; exit 2 ;; esac
