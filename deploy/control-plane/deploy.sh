#!/usr/bin/env bash
# Build the control-plane web image from a git ref and roll it, with rollback.
#
#   deploy/control-plane/deploy.sh [--ref origin/main] [--dry-run] [--no-fetch]
#                                  [--fleet-config PATH]
#
# What it does, in order:
#   1. checks the central fleet config (default
#      ~/.config/herdr-engineering/fleet-config.yaml, created by
#      `herdr-eng fleet config init`) exists and validates
#   2. resolves REF (default origin/main) to a commit and builds
#      herdr-engineering:<sha12> from a clean detached worktree of exactly that commit
#   3. renders the variables docker compose itself needs (images' passwords, ports,
#      bind address) from the central file into a transient 0600 env file inside a
#      0700 directory (`herdr-eng fleet config render-compose`), removed on exit
#   4. recreates ONLY control-plane-web (postgres, rustfs, worker, gateway untouched)
#      using the compose files from that same commit; the central file is mounted
#      read-only into the containers at /etc/herdr/fleet-config.yaml
#   5. waits for /api/v1/health to report the new commit; if it does not, puts the
#      previous image back and exits non-zero
#
# The central file is the only configuration source: no env file is authored or
# kept. Nothing from it is printed by this script or written to a receipt.
set -euo pipefail

REF="origin/main"; DRY=0; FETCH=1
CENTRAL="$HOME/.config/herdr-engineering/fleet-config.yaml"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    --fleet-config) CENTRAL="${2:?--fleet-config needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
PROJECT="${HERDR_CP_PROJECT:-herdr-control-plane}"
HEALTH_URL="${HERDR_CP_HEALTH_URL:-}"
STATE_DIR="$HOME/.local/state/herdr-engineering/deploys"
MOUNT_TARGET="/etc/herdr/fleet-config.yaml"
# Collaboration network (HCN) is opt-in per host: it is layered on only when this
# marker file exists, so an ordinary deploy never switches it on elsewhere.
COLLAB_MARKER="$HOME/.config/herdr-engineering/collab-enabled"

# The mounted central file is a bind source, so it must be an absolute path.
case "$CENTRAL" in /*) ;; *) CENTRAL="$PWD/$CENTRAL" ;; esac
INIT_HINT="create it with: herdr-eng fleet config init --path $CENTRAL"
[ -r "$CENTRAL" ] || { echo "central fleet config not readable: $CENTRAL ($INIT_HINT)" >&2; exit 1; }

WORK=""; ENV_DIR=""
cleanup() {
  [ -z "$ENV_DIR" ] || rm -rf "$ENV_DIR"
  if [ -n "$WORK" ]; then git -C "$REPO" worktree remove --force "$WORK" >/dev/null 2>&1 || rm -rf "$WORK"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

[ "$FETCH" = 1 ] && git -C "$REPO" fetch -q origin
FULL="$(git -C "$REPO" rev-parse --verify "$REF^{commit}")"
SHA="${FULL:0:12}"
TAG="$SHA"

WORK="$(mktemp -d)"
git -C "$REPO" worktree add -q --detach "$WORK" "$FULL"
# herdr-eng from exactly the commit being deployed, so the rendered variables match
# the compose files that ship with it.
herdr_eng() { PYTHONPATH="$WORK" python3 -m herdr_engineering.cli "$@"; }

# Validation prints revision and host count only, never a value.
if ! herdr_eng fleet config validate --path "$CENTRAL" >/dev/null 2>&1; then
  echo "central fleet config is invalid: $CENTRAL (check it with: herdr-eng fleet config validate --path $CENTRAL; or $INIT_HINT)" >&2
  exit 1
fi

echo "deploying $REF = $SHA"
echo "central config: $CENTRAL -> mounted read-only at $MOUNT_TARGET"
COLLAB_FILES=()
if [ -e "$COLLAB_MARKER" ]; then
  COLLAB_FILES=(-f "$WORK/deploy/control-plane/compose.collab.yaml")
  echo "collaboration network: enabled (marker $COLLAB_MARKER)"
fi

# Transient compose variables: 0600 file in a 0700 directory, gone on any exit.
ENV_DIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/herdr-cp-env.XXXXXX")"
ENV_FILE="$ENV_DIR/compose.env"
herdr_eng fleet config render-compose --path "$CENTRAL" --out "$ENV_FILE" >/dev/null
chmod 600 "$ENV_FILE"

COMPOSE_FILES=(-f "$WORK/deploy/control-plane/compose.yaml" "${COLLAB_FILES[@]}")

if [ "$DRY" = 1 ]; then
  echo "[dry-run] rendered the compose variables to a 0600 file in a 0700 temp dir (removed on exit)"
  if docker compose version >/dev/null 2>&1; then
    # -q prints nothing on success; errors are withheld so no value can leak.
    if HERDR_CP_FLEET_CONFIG_FILE="$CENTRAL" HERDR_CP_IMAGE_TAG="$TAG" HERDR_CP_GIT_COMMIT="$SHA" \
        docker compose --env-file "$ENV_FILE" -p "$PROJECT" "${COMPOSE_FILES[@]}" config -q >/dev/null 2>&1; then
      echo "[dry-run] docker compose config: valid"
    else
      echo "[dry-run] docker compose config FAILED (details withheld; they could contain values)" >&2
      exit 1
    fi
  else
    echo "[dry-run] docker compose not available: skipped offline compose validation"
  fi
  echo "[dry-run] would run: herdr-eng fleet config render-compose --path <central> --out <file>"
  echo "[dry-run] control-plane-web, worker and llm-proxy mount the central file read-only at $MOUNT_TARGET"
  echo "[dry-run] would build herdr-engineering:$TAG and recreate control-plane-web"
  exit 0
fi

PREVIOUS_IMAGE="$(docker inspect -f '{{.Config.Image}}' herdr-cp-web 2>/dev/null || true)"
echo "currently running: ${PREVIOUS_IMAGE:-nothing}"

if [ -z "$HEALTH_URL" ]; then
  # The UI is published on HERDR_CP_WEB_BIND:HERDR_CP_WEB_PUBLIC_PORT.
  BIND="$(grep -E '^HERDR_CP_WEB_BIND=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  PORT="$(grep -E '^HERDR_CP_WEB_PUBLIC_PORT=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  HEALTH_URL="http://${BIND:-127.0.0.1}:${PORT:-80}/api/v1/health"
fi

docker build -q -f "$WORK/deploy/fabric-stack/Dockerfile" --build-arg "GIT_COMMIT=$SHA" \
  -t "herdr-engineering:$TAG" "$WORK" >/dev/null

compose() {
  HERDR_CP_FLEET_CONFIG_FILE="$CENTRAL" HERDR_CP_IMAGE_TAG="$1" HERDR_CP_GIT_COMMIT="$SHA" \
    docker compose --env-file "$ENV_FILE" -p "$PROJECT" \
    "${COMPOSE_FILES[@]}" \
    up -d --no-deps --no-build control-plane-web
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
  # The receipt names the ref, commit and images only - never configuration.
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
