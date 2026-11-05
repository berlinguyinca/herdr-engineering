#!/usr/bin/env bash
# Sourced by deploy.sh; uses its one deploy lock, snapshots and rollback.
# Profiles are private runtime artifacts, never image build inputs.

memory_client_cmd() {
  local tree="$1"; shift
  (cd "$tree" && PYTHONPATH="$tree" python3 -m herdr_engineering.memory_client_profile "$@")
}

memory_client_preflight() {
  MEMORY_CLIENT_ENABLED=0
  MEMORY_CLIENT_MODE=unmanaged
  MEMORY_CLIENT_SUPPORTED=0
  MEMORY_CLIENT_STAGED=""
  MEMORY_CLIENT_CANDIDATE=""
  MEMORY_CLIENT_PREVIOUS=""
  MEMORY_CLIENT_CURRENT="$STATE_DIR/memory-client/profile.tar"
  MEMORY_CLIENT_BUILD_ARGS=()
  if [ ! -f "$WORK/herdr_engineering/memory_client_profile.py" ] \
      || [ ! -f "$WORK/deploy/control-plane/prepare-memory-client.sh" ]; then
    if [ -n "${MEMORY_CLIENT_CONFIG:-}" ]; then
      echo "selected commit does not support memory client profiles" >&2
      return 1
    fi
    return 0
  fi
  MEMORY_CLIENT_SUPPORTED=1
  local info="$ENV_DIR/memory-client-policy.json" flags
  local override=()
  [ -z "${MEMORY_CLIENT_CONFIG:-}" ] || override=(--profile-override)
  memory_client_cmd "$WORK" policy-info --fleet-config "$CENTRAL" "${override[@]}" >"$info" || return 1
  flags="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert type(d.get("enabled")) is bool and d.get("mode") in ("install","clear","unmanaged"); print(int(d["enabled"]), d["mode"])' "$info")" \
    || return 1
  read -r MEMORY_CLIENT_ENABLED MEMORY_CLIENT_MODE <<<"$flags"
  if [ "$MEMORY_CLIENT_ENABLED" = 1 ]; then
    local source=()
    [ -z "${MEMORY_CHECKOUT:-}" ] || source=(--source "$MEMORY_CHECKOUT")
    # Pure lock validation: no Git fetch, cache creation or Docker activity.
    "$WORK/deploy/control-plane/prepare-memory-client.sh" --dry-run "${source[@]}" >/dev/null || return 1
    MEMORY_CLIENT_STAGED="$ENV_DIR/memory-client/profile.tar"
    local profile=()
    [ -z "${MEMORY_CLIENT_CONFIG:-}" ] || profile=(--source "$MEMORY_CLIENT_CONFIG")
    memory_client_cmd "$WORK" prepare --fleet-config "$CENTRAL" "${profile[@]}" \
      --out "$MEMORY_CLIENT_STAGED" >"$ENV_DIR/memory-client-prepared.json" || return 1
    if [ ! -f "$MEMORY_CLIENT_STAGED" ]; then
      echo "memory profile preparation produced no private artifact" >&2
      return 1
    fi
  fi
}

memory_client_build() {
  MEMORY_CLIENT_BUILD_ARGS=()
  [ "$MEMORY_CLIENT_ENABLED" = 1 ] || return 0
  local source=() image
  [ -z "${MEMORY_CHECKOUT:-}" ] || source=(--source "$MEMORY_CHECKOUT")
  image="$("$WORK/deploy/control-plane/prepare-memory-client.sh" "${source[@]}")" || return 1
  [[ "$image" =~ ^herdr-memory-client:[0-9a-f]{40}$ ]] || {
    echo "invalid pinned memory client image receipt" >&2
    return 1
  }
  MEMORY_CLIENT_BUILD_ARGS=(--build-arg "RUNTIME_BASE=$image")
}

memory_client_snapshot() {
  [ "$MEMORY_CLIENT_SUPPORTED" = 1 ] || return 0
  # Never place credentials beside the fleet file's mounted YAML directory.
  backup_file "$MEMORY_CLIENT_CURRENT"
  if [ -e "$MEMORY_CLIENT_CURRENT" ] || [ -L "$MEMORY_CLIENT_CURRENT" ]; then
    memory_client_cmd "$WORK" check --archive "$MEMORY_CLIENT_CURRENT" >/dev/null || return 1
    MEMORY_CLIENT_PREVIOUS="$ATTEMPT/previous-memory-client/profile.tar"
    install_private "$MEMORY_CLIENT_CURRENT" "$MEMORY_CLIENT_PREVIOUS" || return 1
  fi
  if [ -n "$MEMORY_CLIENT_STAGED" ]; then
    memory_client_cmd "$WORK" check --archive "$MEMORY_CLIENT_STAGED" >/dev/null || return 1
    MEMORY_CLIENT_CANDIDATE="$ATTEMPT/candidate-memory-client/profile.tar"
    install_private "$MEMORY_CLIENT_STAGED" "$MEMORY_CLIENT_CANDIDATE" || return 1
  fi
}

memory_client_apply() {  # forward|rollback <the matching source tree>
  local direction="$1" tree="$2" archive="" mode="$MEMORY_CLIENT_MODE"
  [ "$MEMORY_CLIENT_SUPPORTED" = 1 ] || return 0
  if [ "$direction" = rollback ]; then
    archive="$MEMORY_CLIENT_PREVIOUS"
    mode=clear
    [ -z "$archive" ] || mode=install
  elif [ "$direction" = forward ]; then
    archive="$MEMORY_CLIENT_CANDIDATE"
  else
    echo "invalid memory profile deployment direction" >&2
    return 1
  fi
  [ "$mode" != unmanaged ] || return 0
  if [ ! -f "$tree/herdr_engineering/memory_client_profile.py" ]; then
    # A first activation may roll back to a pre-feature image. Never send
    # forward data to it. A previous profile implies a supported image.
    [ -z "$archive" ] && return 0
    echo "previous runtime cannot restore its private memory profile" >&2
    return 1
  fi
  local container
  container="$(container_of control-plane-web)"
  if [ "$mode" = install ]; then
    [ -n "$archive" ] && [ -f "$archive" ] || return 1
    memory_client_cmd "$tree" verify --container "$container" || return 1
    memory_client_cmd "$tree" install --archive "$archive" --container "$container" || return 1
  else
    memory_client_cmd "$tree" install --container "$container" || return 1
  fi
}

memory_client_forward_connectivity() {
  [ "${MEMORY_PRIVATE_EGRESS:-0}" = 1 ] || return 0
  [ "${MEMORY_PRIVATE_EGRESS_CHANGED:-0}" = 1 ] || return 0
  [ "${MEMORY_CLIENT_SUPPORTED:-0}" = 1 ] || return 0
  [ "${MEMORY_CLIENT_ENABLED:-0}" = 1 ] || return 0
  [ "${MEMORY_CLIENT_MODE:-unmanaged}" = install ] || return 0
  memory_client_cmd "$1" connectivity --container "$(container_of control-plane-web)"
}

memory_client_promote() {
  [ "$MEMORY_CLIENT_SUPPORTED" = 1 ] || return 0
  if [ "$MEMORY_CLIENT_MODE" = install ]; then
    install_private "$MEMORY_CLIENT_CANDIDATE" "$MEMORY_CLIENT_CURRENT"
  elif [ "$MEMORY_CLIENT_MODE" = clear ] && [ -e "$MEMORY_CLIENT_CURRENT" ]; then
    memory_client_cmd "$WORK" check --archive "$MEMORY_CLIENT_CURRENT" >/dev/null || return 1
    rm -f "$MEMORY_CLIENT_CURRENT"
  fi
}
