#!/usr/bin/env bash
# Build the control-plane web image from a git ref and roll it, with rollback.
#
#   deploy/control-plane/deploy.sh [--ref origin/main] [--dry-run] [--no-fetch]
#
# What it does, in order:
#   1. resolves REF (default origin/main) to a commit and builds
#      herdr-engineering:<sha12> from a clean detached worktree of exactly that commit
#   2. recreates ONLY control-plane-web (postgres, rustfs, worker, gateway untouched)
#      using the compose files from that same commit, so config ships with the code
#   3. waits for /api/v1/health to report the new commit; if it does not, puts the
#      previous image back and exits non-zero
#
# Secrets stay on the host in $HERDR_CP_ENV_FILE
# (default ~/.config/herdr-engineering/control-plane.env, mode 0600); nothing secret
# is read from, written to, or printed by this script.
set -euo pipefail

REF="origin/main"; DRY=0; FETCH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
PROJECT="${HERDR_CP_PROJECT:-herdr-control-plane}"
ENV_FILE="${HERDR_CP_ENV_FILE:-$HOME/.config/herdr-engineering/control-plane.env}"
HEALTH_URL="${HERDR_CP_HEALTH_URL:-}"
STATE_DIR="$HOME/.local/state/herdr-engineering/deploys"
# Collaboration network (HCN) is opt-in per host: it is layered on only when this
# marker file exists, so an ordinary deploy never switches it on elsewhere.
COLLAB_MARKER="$HOME/.config/herdr-engineering/collab-enabled"

[ -r "$ENV_FILE" ] || { echo "env file not readable: $ENV_FILE (see generate-env.sh)" >&2; exit 1; }
[ "$FETCH" = 1 ] && git -C "$REPO" fetch -q origin
FULL="$(git -C "$REPO" rev-parse --verify "$REF^{commit}")"
SHA="${FULL:0:12}"
TAG="$SHA"

if [ -z "$HEALTH_URL" ]; then
  # The UI is published on HERDR_CP_WEB_BIND:HERDR_CP_WEB_PUBLIC_PORT.
  BIND="$(grep -E '^HERDR_CP_WEB_BIND=' "$ENV_FILE" | cut -d= -f2-)"
  PORT="$(grep -E '^HERDR_CP_WEB_PUBLIC_PORT=' "$ENV_FILE" | cut -d= -f2-)"
  HEALTH_URL="http://${BIND:-127.0.0.1}:${PORT:-80}/api/v1/health"
fi

echo "deploying $REF = $SHA"
PREVIOUS_IMAGE="$(docker inspect -f '{{.Config.Image}}' herdr-cp-web 2>/dev/null || true)"
echo "currently running: ${PREVIOUS_IMAGE:-nothing}"

WORK="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WORK" >/dev/null 2>&1 || rm -rf "$WORK"; }
trap cleanup EXIT
git -C "$REPO" worktree add -q --detach "$WORK" "$FULL"
COLLAB_FILES=()
EDGE_SERVICE=()
if [ -e "$COLLAB_MARKER" ]; then
  COLLAB_FILES=(-f "$WORK/deploy/control-plane/compose.collab.yaml")
  EDGE_DIR="$HOME/.config/herdr-engineering/collab-edge"
  mkdir -p "$EDGE_DIR"
  # Write in place (same inode): the container bind-mounts this single file, and a replaced file
  # would leave it reading the old config.
  [ -e "$EDGE_DIR/nginx.conf" ] || install -m 0644 /dev/null "$EDGE_DIR/nginx.conf"
  if ! cmp -s "$WORK/deploy/control-plane/collab-edge/nginx.conf" "$EDGE_DIR/nginx.conf"; then
    cat "$WORK/deploy/control-plane/collab-edge/nginx.conf" > "$EDGE_DIR/nginx.conf"
    EDGE_CONF_CHANGED=1
  fi
  export HERDR_COLLAB_EDGE_CONF="$EDGE_DIR/nginx.conf"
  export HERDR_COLLAB_DATA_DIR="$HOME/.local/share/herdr-engineering/collab"
  mkdir -p "$HERDR_COLLAB_DATA_DIR"
  chmod 700 "$HERDR_COLLAB_DATA_DIR"
  export HERDR_COLLAB_SYNC_DIR="$HOME/.local/share/herdr-engineering/collab-sync"
  "$WORK/deploy/control-plane/collab-sync.sh" install   # keys + membership files and the 1-minute timer
  EDGE_SERVICE=(collab-public collab-edge)
  echo "collaboration network: enabled (marker $COLLAB_MARKER)"
fi

if [ "$DRY" = 1 ]; then
  echo "[dry-run] would build herdr-engineering:$TAG and recreate control-plane-web"
  HERDR_CP_ENV_FILE="$ENV_FILE" HERDR_CP_IMAGE_TAG="$TAG" docker compose --env-file "$ENV_FILE" \
    -p "$PROJECT" -f "$WORK/deploy/control-plane/compose.yaml" \
    -f "$WORK/deploy/control-plane/compose.integrations.yaml" \
    -f "$WORK/deploy/control-plane/compose.github.yaml" "${COLLAB_FILES[@]}" config --quiet
  echo "[dry-run] compose configuration is valid"
  exit 0
fi

docker build -q -f "$WORK/deploy/fabric-stack/Dockerfile" --build-arg "GIT_COMMIT=$SHA" \
  -t "herdr-engineering:$TAG" "$WORK" >/dev/null

compose() {
  HERDR_CP_ENV_FILE="$ENV_FILE" HERDR_CP_IMAGE_TAG="$1" HERDR_CP_GIT_COMMIT="$SHA" \
    docker compose --env-file "$ENV_FILE" -p "$PROJECT" \
    -f "$WORK/deploy/control-plane/compose.yaml" \
    -f "$WORK/deploy/control-plane/compose.integrations.yaml" \
    -f "$WORK/deploy/control-plane/compose.github.yaml" "${COLLAB_FILES[@]}" \
    up -d --no-deps --no-build control-plane-web "${EDGE_SERVICE[@]}"
  if [ "${EDGE_CONF_CHANGED:-0}" = 1 ] && docker inspect herdr-collab-edge >/dev/null 2>&1; then
    docker exec herdr-collab-edge nginx -s reload
  fi
}

healthy_commit() {
  local want="$1" i body
  for i in $(seq 1 30); do
    body="$(curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null || true)"
    if printf '%s' "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('status')=='ok' and d.get('commit','').startswith('$want') else 1)" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

compose "$TAG" >/dev/null
if healthy_commit "$SHA"; then
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  printf '{"ref":"%s","commit":"%s","image":"herdr-engineering:%s","previous":"%s","at":"%s"}\n' \
    "$REF" "$FULL" "$TAG" "$PREVIOUS_IMAGE" "$(date -u +%FT%TZ)" > "$STATE_DIR/$SHA.json"
  echo "ok: control-plane-web is running $SHA (receipt $STATE_DIR/$SHA.json)"
  exit 0
fi

echo "health check did not report $SHA; rolling back" >&2
if [ -n "$PREVIOUS_IMAGE" ]; then
  PREV_TAG="${PREVIOUS_IMAGE#herdr-engineering:}"
  compose "$PREV_TAG" >/dev/null || true
  echo "rolled back to $PREVIOUS_IMAGE" >&2
fi
exit 1
