#!/usr/bin/env bash
# Sourced by deploy.sh: no separate lock, rollout coordinator, or root daemon.
web_egress_direction() {
  case "$1" in forward|rollback) return 0 ;; esac
  echo "invalid private egress callback direction" >&2; return 1
}

web_egress_cmd() {
  (cd "$REPO" && PYTHONPATH="$REPO" python3 -m herdr_engineering.web_egress_runtime "$@")
}

web_egress_preflight() {
  WEB_EGRESS_MANAGED=0
  WEB_EGRESS_COMPOSE_ENABLED=0
  MEMORY_PRIVATE_EGRESS_CHANGED=0
  WEB_EGRESS_CURRENT="$STATE_DIR/web-egress/current.json"
  WEB_EGRESS_OVERLAY="$HERE/compose.web-egress.yaml"
  WEB_EGRESS_GENERATION=00000000000000000000000000000000
  case "$MEMORY_PRIVATE_EGRESS" in auto|0|1) ;; *) return 1 ;; esac
  # Omitted flags preserve an approved policy; untouched hosts still do nothing.
  if [ "$MEMORY_PRIVATE_EGRESS" != 1 ] && [ ! -e "$WEB_EGRESS_CURRENT" ] && [ ! -L "$WEB_EGRESS_CURRENT" ] \
      && [ ! -e "$STATE_DIR/web-egress/pending.json" ] && [ ! -L "$STATE_DIR/web-egress/pending.json" ]; then
    MEMORY_PRIVATE_EGRESS=0; return 0
  fi
  if [ "$MEMORY_PRIVATE_EGRESS" = auto ]; then
    local approved="$ENV_DIR/web-egress-approved.json" values
    web_egress_cmd approved --current "$WEB_EGRESS_CURRENT" >"$approved" || return 1
    values="$(python3 - "$approved" <<'PYAPPROVED'
import json,sys
v=json.load(open(sys.argv[1]))
print(int(v['enabled']),v.get('cidr','disabled'),v.get('destination','disabled'),v.get('generation','disabled'))
PYAPPROVED
)" || return 1
    read -r MEMORY_PRIVATE_EGRESS MEMORY_PRIVATE_EGRESS_CIDR MEMORY_PRIVATE_EGRESS_GATEWAY WEB_EGRESS_GENERATION <<<"$values"
  fi
  WEB_EGRESS_COMPOSE_ENABLED="$MEMORY_PRIVATE_EGRESS"
  [ "$PROJECT" = herdr-control-plane ] || {
    echo "private egress supports only the canonical private control-plane project" >&2; return 1;
  }
  local flags=() info="$ENV_DIR/web-egress-preview.json"
  if [ "$MEMORY_PRIVATE_EGRESS" = 1 ]; then
    [ -n "$MEMORY_PRIVATE_EGRESS_GATEWAY" ] || {
      echo "--memory-private-egress requires an explicit assigned private --memory-private-egress-gateway IPv4/32" >&2; return 1;
    }
    flags=(--enabled --cidr "$MEMORY_PRIVATE_EGRESS_CIDR" --gateway-ip "$MEMORY_PRIVATE_EGRESS_GATEWAY")
  fi
  web_egress_cmd preview --current "$WEB_EGRESS_CURRENT" "${flags[@]}" >"$info" || return 1
  WEB_EGRESS_MANAGED="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["managed"]))' "$info")" || return 1
  if [ "$MEMORY_PRIVATE_EGRESS" = 1 ]; then
    [ -r "$WEB_EGRESS_OVERLAY" ] || return 1
    echo "private egress opt-in validated; gateway TCP 443 host-input permission only"
  fi
}

web_egress_runtime() {
  web_egress_cmd "$@" --current "$WEB_EGRESS_CURRENT" --attempt "$ATTEMPT/web-egress" \
    --lock-fd "$DEPLOY_LOCK_FD" --lock-path "$STATE_DIR/rollout.lock"
}

web_egress_snapshot() {
  [ "$WEB_EGRESS_MANAGED" = 1 ] || return 0
  backup_file "$WEB_EGRESS_CURRENT"
  local flags=()
  if [ "$MEMORY_PRIVATE_EGRESS" = 1 ]; then
    flags=(--enabled --cidr "$MEMORY_PRIVATE_EGRESS_CIDR" --gateway-ip "$MEMORY_PRIVATE_EGRESS_GATEWAY")
  fi
  web_egress_runtime snapshot "${flags[@]}"
}

web_egress_select() {
  web_egress_direction "$1" || return 1
  local direction="$1" selected="$ENV_DIR/web-egress-selector.json"
  web_egress_runtime selector --direction "$direction" >"$selected" || return 1
  local values
  values="$(python3 - "$selected" <<'PY'
import json,sys
v=json.load(open(sys.argv[1]))
assert type(v['activation_changed']) is bool
print(int(v['enabled']), v.get('cidr','disabled'), v.get('generation','disabled'), int(v['activation_changed']))
PY
)" || return 1
  read -r WEB_EGRESS_COMPOSE_ENABLED MEMORY_PRIVATE_EGRESS_CIDR WEB_EGRESS_GENERATION MEMORY_PRIVATE_EGRESS_CHANGED <<<"$values"
}

web_egress_before() {
  web_egress_direction "$1" || return 1
  [ "$WEB_EGRESS_MANAGED" = 1 ] || return 0
  web_egress_runtime before --direction "$1" || return 1
  web_egress_select "$1"
}

web_egress_after() {
  web_egress_direction "$1" || return 1
  [ "$WEB_EGRESS_MANAGED" = 1 ] || return 0
  # Called only after Compose AND its matching memory profile are restored.
  web_egress_runtime after --direction "$1"
}

web_egress_commit() {
  web_egress_direction "$1" || return 1
  [ "$WEB_EGRESS_MANAGED" = 1 ] || return 0
  web_egress_runtime commit --direction "$1"
}

web_egress_cleanup() {
  web_egress_direction "$1" || return 1
  [ "$WEB_EGRESS_MANAGED" = 1 ] || return 0
  [ "$WEB_EGRESS_COMPOSE_ENABLED" = 0 ] || return 0
  local record=previous
  [ "$1" != rollback ] || record=candidate
  # Exact ID/labels and an empty network only. Never detach another member.
  web_egress_runtime prune --record "$record"
}
