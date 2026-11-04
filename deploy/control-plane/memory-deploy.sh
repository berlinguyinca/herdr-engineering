#!/usr/bin/env bash
# Memory step of deploy.sh (--with-memory). SOURCED by deploy.sh from the commit being deployed; not run on its own.
# Needs deploy.sh's context: WORK ENV_FILE ENV_DIR DRY CENTRAL SHA TAG HEALTH_TRIES HEALTH_INTERVAL ATTEMPT
# MEMORY_CHECKOUT, and its functions herdr_eng / compose_up_retry. Kept separate so it can be tested without a
# Linux host and without a full deploy (tests/test_memory_deploy.py). Every function returns explicitly: callers use
# `||`, which disables `set -e`, so nothing here may rely on it. No value from the rendered file is ever printed.

env_get() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\"" || true; }
memory_preflight() {
  local v
  case "$(uname -m)" in
    x86_64|amd64) ;;
    *) echo "--with-memory refused: this host is $(uname -m). The pinned Hindsight images (pgvector digest, hindsight" \
            "slim digest, ONNX wheels) are verified for linux/amd64 only; arm64 digests have not been verified." \
            "Run the memory step on an amd64 host. Nothing was changed." >&2
       return 1 ;;
  esac
  for v in HERDR_MEMORY_DB_PASSWORD HERDR_MEMORY_TENANT_USERS HERDR_MEMORY_IMAGE_TAG HERDR_MEMORY_MODEL_DIR HERDR_MEMORY_PORT; do
    if [ -z "$(env_get "$v" "$ENV_FILE")" ]; then
      echo "--with-memory refused: $v is not rendered. control_plane.memory must be enabled and complete in $CENTRAL" \
           "(herdr-eng fleet config memory init --enable --endpoint https://... --model-dir /abs/path). Nothing was changed." >&2
      return 1
    fi
  done
  MEM_TAG="$(env_get HERDR_MEMORY_IMAGE_TAG "$ENV_FILE")"
  MEM_PORT="$(env_get HERDR_MEMORY_PORT "$ENV_FILE")"
  MEM_MODEL_DIR="$(env_get HERDR_MEMORY_MODEL_DIR "$ENV_FILE")"
  # the pin: revision (and default checkout path) of the herdr-memory deploy candidate, from THIS commit's lock file
  if ! MEM_LOCK="$(python3 - "$WORK/lock/upstreams.yaml" <<'PY'
import sys, yaml
d = (yaml.safe_load(open(sys.argv[1])) or {}).get("dependencies", {}).get("herdr-memory-deploy") or {}
rev = str(d.get("revision") or "")
if not rev: sys.exit(1)
print(rev); print(d.get("checkout") or "~/.local/share/herdr-engineering/herdr-memory")
PY
)"; then
    echo "--with-memory refused: lock/upstreams.yaml has no herdr-memory-deploy revision pin. Nothing was changed." >&2
    return 1
  fi
  MEM_PIN="$(printf '%s\n' "$MEM_LOCK" | sed -n 1p)"
  [ -n "$MEMORY_CHECKOUT" ] || MEMORY_CHECKOUT="$(printf '%s\n' "$MEM_LOCK" | sed -n 2p)"
  MEM_CK="${MEMORY_CHECKOUT/#\~/$HOME}"
  if ! git -C "$MEM_CK" rev-parse --verify "$MEM_PIN^{commit}" >/dev/null 2>&1 \
      || ! git -C "$MEM_CK" cat-file -e "$MEM_PIN:deploy/build.sh" 2>/dev/null; then
    echo "--with-memory refused: $MEM_CK is not a herdr-memory git checkout holding the pinned revision $MEM_PIN" \
         "(with deploy/build.sh). Clone berlinguyinca/herdr-memory there (or pass --memory-checkout) and fetch. Nothing was changed." >&2
    return 1
  fi
  if [ ! -f "$MEM_MODEL_DIR/onnx/model.onnx" ]; then
    if [ "$DRY" = 1 ]; then
      echo "[dry-run] WARNING: $MEM_MODEL_DIR/onnx/model.onnx is missing; a real --with-memory deploy refuses until the pinned model is prepared" >&2
    else
      echo "--with-memory refused: $MEM_MODEL_DIR/onnx/model.onnx is missing. Prepare the pinned E5 model first" \
           "(python3 -m herdr_memory.model_assets prepare DIR). Nothing was changed." >&2
      return 1
    fi
  fi
  return 0
}
# ---------------------------------------------------------------------------------------------------------------
# Optional memory step. Runs only after the control plane is deployed, verified and receipted. NEVER `set -e`
# territory: every step returns explicitly, and nothing here touches a control-plane container.
# ---------------------------------------------------------------------------------------------------------------
MEM_DB=herdr-hindsight-db; MEM_API=herdr-hindsight
mem_up() { compose_up_retry "$WORK" "${MEM_ENV_FILE:-$ENV_FILE}" "$TAG" "$SHA" --profile memory up -d --no-deps --no-build "$@" >/dev/null; }

# memory_settled <seconds-tries>: both containers running, not restarting, HEALTHY (not "none"), restart counts
# stable across two samples, and the loopback /health answers. Prints what is not settled.
memory_settled() {
  local i name st status restarting count health ok counts prev="" bad=""
  for i in $(seq 1 "$HEALTH_TRIES"); do
    ok=1; counts=""; bad=""
    for name in "$MEM_DB" "$MEM_API"; do
      st="$(docker inspect -f '{{.State.Status}} {{.State.Restarting}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo "missing false 0 none")"
      read -r status restarting count health <<< "$st"
      counts="$counts $name=$count"
      if [ "$status" != running ] || [ "$restarting" = true ] || [ "$health" != healthy ]; then
        ok=0; bad="$bad $name($status/$health)"
      fi
    done
    if [ "$ok" = 1 ] && ! curl -fsS -m 5 "http://127.0.0.1:$MEM_PORT/health" >/dev/null 2>&1; then
      ok=0; bad="$bad /health"
    fi
    if [ "$ok" = 1 ] && [ "$counts" = "$prev" ]; then return 0; fi
    if [ "$ok" = 1 ]; then prev="$counts"; else prev=""; fi
    [ "$i" = "$HEALTH_TRIES" ] || sleep "$HEALTH_INTERVAL"
  done
  echo "memory not settled:${bad:- restart count still rising}" >&2
  return 1
}

memory_forward() {
  local src="$ENV_DIR/memory-src"
  mkdir -p "$src" || return 1
  # exactly the pinned revision, exported (tracked files only): the user's checkout is never modified
  git -C "$MEM_CK" archive "$MEM_PIN" | tar -x -C "$src" || return 1
  echo "memory: building herdr-memory-hindsight:$MEM_TAG from herdr-memory $MEM_PIN (deploy/build.sh)"
  if ! (cd "$src" && HERDR_MEMORY_IMAGE="herdr-memory-hindsight:$MEM_TAG" bash deploy/build.sh) \
        >"$ENV_DIR/memory-build.log" 2>&1; then
    echo "memory image build failed (last lines of the build log; it holds no configuration values):" >&2
    tail -n 15 "$ENV_DIR/memory-build.log" >&2
    return 1
  fi
  MEM_NEW_IMAGE="$(docker image inspect -f '{{.Id}}' "herdr-memory-hindsight:$MEM_TAG" 2>/dev/null)" || return 1
  [ -n "$MEM_NEW_IMAGE" ] || return 1
  # database first (alone, until healthy), then the API
  mem_up hindsight-db || return 1
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$MEM_DB" 2>/dev/null)" = healthy ] && break
    [ "$i" != "$HEALTH_TRIES" ] || { echo "hindsight-db did not become healthy" >&2; return 1; }
    sleep "$HEALTH_INTERVAL"
  done
  mem_up hindsight-api || return 1
  memory_settled
}

memory_rollback() {
  if [ -z "$MEM_PREV_IMAGE" ]; then
    # first memory deploy: there is no earlier version to go back to. Fail safe: stop what this step started.
    # Volumes are never removed.
    docker stop "$MEM_API" "$MEM_DB" >/dev/null 2>&1 || true
    echo "memory step FAILED on its first deploy: there is no previous memory version to roll back to, so the new" \
         "memory containers were STOPPED (the data volume is untouched). The control plane is running as deployed." \
         "Fix the cause and re-run with --with-memory." >&2
    return 0
  fi
  echo "memory step failed; rolling the API back to the previous image (database and volume untouched)" >&2
  MEM_ENV_FILE="$ENV_FILE"
  if [ -r "${ATTEMPT:-}/previous/fleet-config.yaml" ]; then   # the config the previous memory ran with
    if herdr_eng fleet config render-compose --path "$ATTEMPT/previous/fleet-config.yaml" --out "$ENV_DIR/memory-prev.env" >/dev/null 2>&1 \
        && [ -n "$(env_get HERDR_MEMORY_TENANT_USERS "$ENV_DIR/memory-prev.env")" ]; then
      MEM_ENV_FILE="$ENV_DIR/memory-prev.env"; chmod 600 "$MEM_ENV_FILE"
    fi
  fi
  docker tag "$MEM_PREV_IMAGE" "herdr-memory-hindsight:$MEM_TAG" >/dev/null 2>&1 || return 1
  compose_up_retry "$WORK" "$MEM_ENV_FILE" "$TAG" "$SHA" --profile memory up -d --no-deps --no-build --force-recreate hindsight-api >/dev/null || return 1
  memory_settled
}

memory_phase() {
  MEM_PREV_IMAGE="$(docker inspect -f '{{.Image}}' "$MEM_API" 2>/dev/null || true)"
  MEM_NEW_IMAGE=""
  if memory_forward; then
    mkdir -p "$MEM_STATE_DIR" && chmod 700 "$MEM_STATE_DIR" \
      && python3 - "$MEM_STATE_DIR/deployed.json" "$MEM_TAG" "$MEM_NEW_IMAGE" "$MEM_PORT" "$MEM_PIN" "$SHA" <<'PY' || echo "WARNING: could not record the memory deploy marker" >&2
import json, os, sys, time
path, tag, image, port, pin, sha = sys.argv[1:7]
data = {"image_tag": tag, "image_id": image, "port": int(port), "herdr_memory_revision": pin,
        "herdr_engineering_commit": sha, "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh: json.dump(data, fh)
os.replace(path + ".tmp", path)
PY
    # backups are a requirement of a real deploy: the timer is installed (idempotent); a failure is loud, not fatal
    "$WORK/deploy/control-plane/memory-backup.sh" install >/dev/null \
      || echo "WARNING: could not install the memory backup timer; run deploy/control-plane/memory-backup.sh install" >&2
    echo "ok: memory running (hindsight-db, hindsight-api on 127.0.0.1:$MEM_PORT; image $MEM_TAG)"
    return 0
  fi
  echo "memory step FAILED (the control plane is unaffected and running $SHA)" >&2
  if memory_rollback; then
    if [ -n "$MEM_PREV_IMAGE" ]; then echo "memory rolled back to the previous image and healthy; the memory deploy FAILED" >&2; fi
  else
    echo "FAILED TO ROLL BACK memory: hindsight may be down or mixed; the control plane is unaffected." >&2
    echo "inspect: docker ps -a --filter name=herdr-hindsight ; previous image: ${MEM_PREV_IMAGE:-none}" >&2
  fi
  return 1
}
