#!/usr/bin/env bash
# Build the control-plane web image from a git ref and roll it, with rollback.
#
#   deploy/control-plane/deploy.sh [--ref origin/main] [--dry-run] [--no-fetch]
#                                  [--fleet-config PATH] [--collab-lock-wait SECONDS]
#                                  [--deploy-lock-wait SECONDS]
#
# What it does, in order:
#   1. checks the central fleet config (default
#      ~/.config/herdr-engineering/fleet/fleet-config.yaml, created by
#      `herdr-eng fleet config init`) exists, validates, and sits ALONE in its
#      directory (symlinks are resolved): that directory is what gets mounted
#   2. resolves REF (default origin/main) to a commit and builds
#      herdr-engineering:<sha12> from a clean detached worktree of exactly that commit
#   3. renders the variables docker compose itself needs (images' passwords, ports,
#      bind address) from the central file into a transient 0600 env file inside a
#      0700 directory (`herdr-eng fleet config render-compose`), removed on exit
#   4. recreates control-plane-web, control-plane-worker and llm-proxy on that one
#      image tag (postgres, rustfs, coredns, gateway, herdr-web untouched) using the
#      compose files from that same commit; the central file's DIRECTORY is
#      mounted read-only at /etc/herdr/fleet/ (a single-file mount would keep the old
#      inode after a rename-replace, so edits would never reach the server).
#      With the collaboration marker it also renders control_plane.collab (and only
#      that) to a stable 0600 slice, writes the edge nginx.conf in place at a stable
#      path (reloading the edge when it changed), refreshes the Cognito key/membership
#      files, the edge certificate and their timers (collab-sync.sh), and recreates
#      collab-public + collab-edge
#      Share links: control_plane.share is always validated (render-share; `enabled: true` without a
#      signing_secret is refused before anything runs, the secret never printed). Only when it is enabled
#      (which needs the collaboration marker) is its slice installed at a stable 0600 path, the edge
#      nginx.conf rendered with the /s/ location, and share-public force-recreated once, before the edge;
#      disabled, a running share-public is stopped and the edge conf is the repo file unchanged. A rollback
#      recreates share-public from the previous commit when the restored edge conf routes /s/, else stops it
#   5. waits for /api/v1/health to report the new commit AND for every recreated
#      container (web, worker, llm-proxy, collab-public/edge) to be running, not
#      restarting and not unhealthy. Nothing on the host changes before the image is
#      built; slices, nginx.conf and the collab-sync units are installed after it and
#      restored from a backup on any failure. If an install step, compose up or the
#      checks fail, rolls back: the commit recorded in the
#      running image's receipt is checked out in a temporary worktree and ITS compose
#      files are run with pinned image IDs and a protected previous config snapshot.
#      Recovery metadata and 0600 snapshots persist under STATE_DIR before service changes.
#      Successful deployments mount the selected canonical source, never a temporary copy. Prints
#      "rolled back ... and healthy" (exit 1) only when the old commit answers health
#      again, otherwise FAILED TO ROLL BACK with the manual command (exit 3). A first
#      deploy, or one whose running commit predates the directory-mounted fleet
#      config, has no automatic rollback and says so.
#      A service container that is MISSING when the deploy starts (an interrupted
#      recreate removed it) is recorded as absent, not an error: the deploy recreates it,
#      and a rollback recreates it from the previous web's pinned image.
#
# One deploy at a time: every real deploy holds STATE_DIR/deploy.lock from before it
# changes anything until it exits (rollback included), waiting --deploy-lock-wait
# seconds (default 600) for another deploy to finish, else failing untouched.
# collab-public is recreated exactly ONCE per deploy (forced only when its slice
# changed); the 2026-10-29 "removal of container ... is already in progress" came most
# probably from recreating it twice back to back (or from two concurrent deploys).
# Defence in depth: with collaboration enabled the deploy also holds collab-sync's own
# lock (<collab-sync dir>.lock, the one `collab-sync.sh once` takes; that run only
# reloads the edge's nginx, it never removes a container), waiting --collab-lock-wait
# seconds (default 300). The kernel drops both locks on any exit, even SIGKILL, so
# nothing is left paused. A compose up that still hits that removal error waits for
# the removal to finish and is retried (bounded); any other failure is not retried.
#
# GitHub App key (optional, fleet config `github.app`): ~/.config/herdr-engineering/github-app/
# is mounted read-only at /etc/herdr/github-app/ into control-plane-web (the only service
# that reads GitHub). It must be a real 0700 directory owned by the deploying user holding
# nothing or exactly one regular file `github-app.pem` (no symlink, owned by the deploying
# user, mode exactly 0600 or 0400), else the deploy refuses before building. A missing directory is created empty
# (0700) on a real deploy. The container runs as root (rootless docker: as the deploying
# user), so an owner-only file is readable there, exactly like the fleet file: never
# loosen its mode. Set `github.app.private_key_file: /etc/herdr/github-app/github-app.pem`.
#
# Deployment definitions (fleet config `control_plane.deployment_definitions: /etc/herdr/pipelines`):
# ~/.local/share/herdr-engineering/pipelines/ is mounted read-only at /etc/herdr/pipelines/ into
# control-plane-web (the only reader). It must be a real directory owned by the deploying user, not
# writable by group/other, holding only regular *.yaml files (no symlinks, at most 256, each at most
# 64 KiB), else the deploy refuses before building. A missing one is created empty (0755) on a real
# deploy. Its content is kept equal to InferWeave's deploy/pipelines on origin/main by the
# herdr-pipelines-sync timer (pipelines-sync.sh), installed by every real deploy.
#
#   HERDR_CP_HEALTH_URL / _TRIES (900) / _INTERVAL (2s) tune the health check.
#
# The central file is the only configuration source: no env file is authored or
# kept. Nothing from it is printed by this script or written to a receipt.
set -euo pipefail

REF="origin/main"; DRY=0; FETCH=1; COLLAB_LOCK_WAIT=300; DEPLOY_LOCK_WAIT=600
CENTRAL="$HOME/.config/herdr-engineering/fleet/fleet-config.yaml"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    --fleet-config) CENTRAL="${2:?--fleet-config needs a value}"; shift 2 ;;
    --collab-lock-wait) COLLAB_LOCK_WAIT="${2:?--collab-lock-wait needs a value}"; shift 2 ;;
    --deploy-lock-wait) DEPLOY_LOCK_WAIT="${2:?--deploy-lock-wait needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/p}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$COLLAB_LOCK_WAIT" =~ ^[0-9]+$ ]] || { echo "--collab-lock-wait needs a whole number of seconds" >&2; exit 2; }
[[ "$DEPLOY_LOCK_WAIT" =~ ^[0-9]+$ ]] || { echo "--deploy-lock-wait needs a whole number of seconds" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
PROJECT="${HERDR_CP_PROJECT:-herdr-control-plane}"
HEALTH_URL="${HERDR_CP_HEALTH_URL:-}"
STATE_DIR="$HOME/.local/state/herdr-engineering/deploys"
MOUNT_TARGET="/etc/herdr/fleet/fleet-config.yaml"
# Collaboration network (HCN) is opt-in per host: it is layered on only when this
# marker file exists, so an ordinary deploy never switches it on elsewhere.
COLLAB_MARKER="$HOME/.config/herdr-engineering/collab-enabled"

# The mounted central file is a bind source, so it must be an absolute path.
case "$CENTRAL" in /*) ;; *) CENTRAL="$PWD/$CENTRAL" ;; esac
INIT_HINT="create it with: herdr-eng fleet config init --path $CENTRAL"
[ -r "$CENTRAL" ] || { echo "central fleet config not readable: $CENTRAL ($INIT_HINT)" >&2; exit 1; }
# The containers get the file's DIRECTORY read-only, so everything in it is exposed:
# it must hold only fleet-config.yaml (writers' transient .fleet-config.yaml.*.tmp aside).
CENTRAL_REAL="$(readlink -f "$CENTRAL")"
FLEET_DIR="$(dirname "$CENTRAL_REAL")"
CENTRAL_HASH="$(sha256sum "$CENTRAL_REAL" | cut -d' ' -f1)"
if [ "$(basename "$CENTRAL_REAL")" != "fleet-config.yaml" ]; then
  echo "central fleet config must be named fleet-config.yaml (it is mounted as $MOUNT_TARGET): $CENTRAL_REAL" >&2
  exit 1
fi
OTHERS="$(find "$FLEET_DIR" -mindepth 1 -maxdepth 1 ! -name fleet-config.yaml ! -name '.fleet-config.yaml.*.tmp' -printf '%f ' 2>/dev/null || true)"
if [ -n "$OTHERS" ]; then
  echo "$FLEET_DIR must hold only fleet-config.yaml (the directory is mounted into the containers); also found: $OTHERS" >&2
  echo "move the file into its own directory (default ~/.config/herdr-engineering/fleet/) and leave a symlink at the old path" >&2
  exit 1
fi

# The GitHub App key gets its OWN read-only mount (the fleet dir holds only the config).
GITHUB_APP_DIR="$HOME/.config/herdr-engineering/github-app"
check_github_app_dir() {   # nothing printed but names; never the key
  local d="$GITHUB_APP_DIR" mode others f
  [ -e "$d" ] || [ -L "$d" ] || return 0
  if [ -L "$d" ] || [ ! -d "$d" ]; then
    echo "$d must be a real directory (not a symlink or a file)" >&2; return 1
  fi
  if [ "$(stat -c %u "$d")" != "$(id -u)" ]; then
    echo "$d must be owned by the deploying user (uid $(id -u))" >&2; return 1
  fi
  mode="$(stat -c %a "$d")"
  if [ "$mode" != 700 ]; then
    echo "$d must be mode 0700 (it is $mode): chmod 700 $d" >&2; return 1
  fi
  others="$(find "$d" -mindepth 1 -maxdepth 1 ! -name github-app.pem -printf '%f ' 2>/dev/null || true)"
  if [ -n "$others" ]; then
    echo "$d must hold only github-app.pem (the directory is mounted into control-plane-web); also found: $others" >&2
    return 1
  fi
  f="$d/github-app.pem"
  [ -e "$f" ] || [ -L "$f" ] || return 0
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    echo "$f must be a regular file (not a symlink)" >&2; return 1
  fi
  if [ "$(stat -c %u "$f")" != "$(id -u)" ]; then
    echo "$f must be owned by the deploying user (uid $(id -u))" >&2; return 1
  fi
  mode="$(stat -c %a "$f")"
  if [ "$mode" != 600 ] && [ "$mode" != 400 ]; then
    echo "$f must be mode 0600 or 0400 (it is $mode): chmod 600 $f" >&2; return 1
  fi
}
check_github_app_dir || exit 1

# Deployment definitions: their OWN read-only mount, refreshed by pipelines-sync.sh.
PIPELINES_DIR="$HOME/.local/share/herdr-engineering/pipelines"
check_pipelines_dir() {   # names only, never content
  local d="$PIPELINES_DIR" bad n
  [ -e "$d" ] || [ -L "$d" ] || return 0
  if [ -L "$d" ] || [ ! -d "$d" ]; then
    echo "$d must be a real directory (not a symlink or a file)" >&2; return 1
  fi
  if [ "$(stat -c %u "$d")" != "$(id -u)" ]; then
    echo "$d must be owned by the deploying user (uid $(id -u))" >&2; return 1
  fi
  if [ $(( 0$(stat -c %a "$d") & 022 )) != 0 ]; then
    echo "$d must not be writable by group or other: chmod 755 $d" >&2; return 1
  fi
  bad="$(find "$d" -mindepth 1 -maxdepth 1 \( ! -type f -o ! -name '*.yaml' -o -perm /022 -o -size +64k \) -printf '%f ' 2>/dev/null || true)"
  if [ -n "$bad" ]; then
    echo "$d must hold only regular *.yaml files of at most 64 KiB, not writable by group/other (it is mounted into control-plane-web); found: $bad" >&2
    return 1
  fi
  n="$(find "$d" -mindepth 1 -maxdepth 1 -printf . | wc -c)"
  if [ "$n" -gt 256 ]; then echo "$d holds $n files (at most 256)" >&2; return 1; fi
}
check_pipelines_dir || exit 1

WORK=""; ENV_DIR=""; PREV_WORK=""
cleanup() {
  if [ -n "$PREV_WORK" ]; then git -C "$REPO" worktree remove --force "$PREV_WORK" >/dev/null 2>&1 || true; fi
  [ -z "$ENV_DIR" ] || rm -rf "$ENV_DIR"
  if [ -n "$WORK" ]; then git -C "$REPO" worktree remove --force "$WORK" >/dev/null 2>&1 || rm -rf "$WORK"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# One deploy at a time (fd 7, released by the kernel on any exit). A dry run changes
# nothing, so it neither takes nor waits for the lock.
if [ "$DRY" = 0 ]; then
  mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
  DEPLOY_LOCK="$STATE_DIR/deploy.lock"
  exec 7>"$DEPLOY_LOCK"
  if ! flock -w "$DEPLOY_LOCK_WAIT" 7; then
    echo "another deploy.sh is still running after ${DEPLOY_LOCK_WAIT}s (lock $DEPLOY_LOCK); nothing was changed. Retry, or raise --deploy-lock-wait" >&2
    exit 1
  fi
fi

[ "$FETCH" = 1 ] && git -C "$REPO" fetch -q origin
FULL="$(git -C "$REPO" rev-parse --verify "$REF^{commit}")"
SHA="${FULL:0:12}"
TAG="$SHA"

WORK="$(mktemp -d)"
git -C "$REPO" worktree add -q --detach "$WORK" "$FULL"
# herdr-eng from exactly the commit being deployed, so the rendered variables match
# the compose files that ship with it.
# Run from inside $WORK: `python3 -m` puts the cwd first on sys.path, so a caller
# standing in another checkout would otherwise get that checkout's code.
herdr_eng() { (cd "$WORK" && PYTHONPATH="$WORK" python3 -m herdr_engineering.cli "$@"); }

# Validation prints revision and host count only, never a value.
if ! herdr_eng fleet config validate --path "$CENTRAL" >/dev/null 2>&1; then
  echo "central fleet config is invalid: $CENTRAL (check it with: herdr-eng fleet config validate --path $CENTRAL; or $INIT_HINT)" >&2
  exit 1
fi

echo "deploying $REF = $SHA"
echo "central config: $CENTRAL -> its directory $FLEET_DIR mounted read-only, file at $MOUNT_TARGET"
COLLAB_FILES=()
EDGE_SERVICE=()
COLLAB=0
if [ -e "$COLLAB_MARKER" ]; then
  COLLAB=1
  COLLAB_FILES=(-f "$WORK/deploy/control-plane/compose.collab.yaml")
  EDGE_SERVICE=(collab-public collab-edge)
  echo "collaboration network: enabled (marker $COLLAB_MARKER)"
fi

# Transient compose variables: 0600 file in a 0700 directory, gone on any exit.
# NOTHING on the host changes before the image is built: the compose env and both
# slices are rendered into this temp dir first and installed by forward() below.
ENV_DIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/herdr-cp-env.XXXXXX")"
ENV_FILE="$ENV_DIR/compose.env"
herdr_eng fleet config render-compose --path "$CENTRAL" --out "$ENV_FILE" >/dev/null
chmod 600 "$ENV_FILE"

# only_file <dir> <name>: the directory (mounted whole into a container) holds nothing else
only_file() {
  [ -d "$1" ] || return 0
  local others
  others="$(find "$1" -mindepth 1 -maxdepth 1 ! -name "$2" ! -name ".$2.*.tmp" -printf '%f ' 2>/dev/null || true)"
  if [ -n "$others" ]; then
    echo "$1 must hold only $2 (it is mounted into a container); also found: $others" >&2
    return 1
  fi
}

# llm-proxy gets ONLY the llm slice (llm + control_plane.llm_proxy), in a directory of
# its own that is mounted read-only; never the central file.
NEW_LLM="$ENV_DIR/new/llm-proxy/llm-config.json"
if ! herdr_eng fleet config render-llm --path "$CENTRAL" --out "$NEW_LLM" >/dev/null; then
  echo "the llm section is missing or invalid in $CENTRAL (needed by llm-proxy)" >&2
  exit 1
fi
if [ "$DRY" = 1 ]; then LLM_DIR="$(dirname "$NEW_LLM")"
else LLM_DIR="$HOME/.config/herdr-engineering/llm-proxy"; only_file "$LLM_DIR" llm-config.json || exit 1; fi
export HERDR_LLM_SLICE_DIR="$LLM_DIR"

# Share links: share-public gets ONLY the render-share slice (control_plane.share serving keys + the Cognito
# identifiers). The slice is always rendered (to validate), installed only when share is enabled. An older
# candidate without the command has no share wiring at all.
SHARE=0; NEW_SHARE=""
if grep -q '"render-share"' "$WORK/herdr_engineering/cli.py" 2>/dev/null; then
  NEW_SHARE="$ENV_DIR/new/share/share-config.json"
  # render-share prints the reason (key names and how to fix it, never a value) on failure
  if ! SHARE_OUT="$(herdr_eng fleet config render-share --path "$CENTRAL" --out "$NEW_SHARE")"; then
    echo "control_plane.share is incomplete or invalid in $CENTRAL; nothing was changed" >&2
    exit 1
  fi
  case "$SHARE_OUT" in *share=enabled) SHARE=1 ;; esac
fi
if [ "$SHARE" = 1 ] && [ "$COLLAB" = 0 ]; then
  echo "control_plane.share.enabled is true but the collaboration network is off: share links are served behind" \
       "the collab edge (create $COLLAB_MARKER), or set control_plane.share.enabled: false" >&2
  exit 1
fi
if [ "$DRY" = 1 ]; then
  SHARE_DIR="$ENV_DIR/new/share"
  export HERDR_SHARES_DIR="$ENV_DIR/shares" HERDR_SHARE_EVENTS_DIR="$ENV_DIR/share-events"
else
  SHARE_DIR="$HOME/.config/herdr-engineering/share"
  if [ "$SHARE" = 1 ]; then only_file "$SHARE_DIR" share-config.json || exit 1; fi
  # bundles (web writes, worker sweeps, share-public reads) and share-public's events file (worker reads)
  export HERDR_SHARES_DIR="$HOME/.local/share/herdr-engineering/shares"
  export HERDR_SHARE_EVENTS_DIR="$HOME/.local/share/herdr-engineering/share-events"
fi
export HERDR_SHARE_CONFIG_DIR="$SHARE_DIR"
SHARE_CONFIG="$SHARE_DIR/share-config.json"
SHARE_SERVICE=()
[ "$SHARE" = 0 ] || SHARE_SERVICE=(share-public)

# The edge bind-mounts this single nginx.conf, so it is written IN PLACE (same inode): a
# rename-replace would leave the running container reading the old file.
EDGE_CONF_CHANGED=0
write_edge_conf() {   # <source file>: in place; sets EDGE_CONF_CHANGED when it changed
  [ -e "$HERDR_COLLAB_EDGE_CONF" ] || install -m 0644 /dev/null "$HERDR_COLLAB_EDGE_CONF"
  if ! cmp -s "$1" "$HERDR_COLLAB_EDGE_CONF"; then
    cat "$1" > "$HERDR_COLLAB_EDGE_CONF"
    EDGE_CONF_CHANGED=1
  fi
}
reload_edge_if_changed() {
  if [ "$EDGE_CONF_CHANGED" = 1 ] && docker inspect herdr-collab-edge >/dev/null 2>&1; then
    docker exec herdr-collab-edge nginx -s reload >/dev/null
  fi
  EDGE_CONF_CHANGED=0
}

NEW_COLLAB=""
if [ "$COLLAB" = 1 ]; then
  # collab-public gets ONLY control_plane.collab (public identifiers), never the central
  # file. The slice's own directory is mounted (not the file), so a re-render is seen.
  NEW_COLLAB="$ENV_DIR/new/collab/collab-config.json"
  if ! herdr_eng fleet config render-collab --path "$CENTRAL" --out "$NEW_COLLAB" >/dev/null; then
    echo "collaboration is enabled ($COLLAB_MARKER) but control_plane.collab is missing or invalid in $CENTRAL" >&2
    exit 1
  fi
  if [ "$DRY" = 1 ]; then COLLAB_DIR="$(dirname "$NEW_COLLAB")"
  else COLLAB_DIR="$HOME/.config/herdr-engineering/collab"; only_file "$COLLAB_DIR" collab-config.json || exit 1; fi
  COLLAB_CONFIG="$COLLAB_DIR/collab-config.json"
  if ! grep -qE '^HERDR_COLLAB_CERT_DIR=.+' "$ENV_FILE"; then
    echo "collaboration is enabled ($COLLAB_MARKER) but control_plane.compose.HERDR_COLLAB_CERT_DIR is not set in $CENTRAL" >&2
    exit 1
  fi
  export HERDR_COLLAB_CONFIG_DIR="$COLLAB_DIR"
  # The edge conf for this config: the repo file, plus the /s/ location only when share is enabled.
  NEW_EDGE="$ENV_DIR/new/collab-edge/nginx.conf"
  EDGE_TEMPLATE="$WORK/deploy/control-plane/collab-edge/nginx.conf"
  if [ -n "$NEW_SHARE" ]; then
    herdr_eng fleet config render-edge --path "$CENTRAL" --template "$EDGE_TEMPLATE" --out "$NEW_EDGE" >/dev/null || exit 1
  else
    mkdir -p "$(dirname "$NEW_EDGE")" && cp "$EDGE_TEMPLATE" "$NEW_EDGE"
  fi
  if [ "$DRY" = 1 ]; then
    # Placeholders so the offline compose validation can resolve the paths.
    export HERDR_COLLAB_EDGE_CONF="$NEW_EDGE"
    export HERDR_COLLAB_DATA_DIR="$ENV_DIR/collab-data"
    export HERDR_COLLAB_SYNC_DIR="$ENV_DIR/collab-sync"
  else
    export HERDR_COLLAB_EDGE_CONF="$HOME/.config/herdr-engineering/collab-edge/nginx.conf"
    export HERDR_COLLAB_DATA_DIR="$HOME/.local/share/herdr-engineering/collab"
    export HERDR_COLLAB_SYNC_DIR="$HOME/.local/share/herdr-engineering/collab-sync"
    # The edge mounts control_plane.compose.HERDR_COLLAB_CERT_DIR; collab-sync copies the
    # certificate there (the timer has no environment, so it gets the path as a flag).
    CERT_DIR_VALUE="$(grep -E '^HERDR_COLLAB_CERT_DIR=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  fi
fi

COMPOSE_FILES=(-f "$WORK/deploy/control-plane/compose.yaml" "${COLLAB_FILES[@]}")
# Every service of ours that runs the herdr-engineering image and reads the fleet
# config: rolled forward (and back) together on one tag.
APP_SERVICES=(control-plane-web control-plane-worker llm-proxy)

if [ "$DRY" = 1 ]; then
  echo "[dry-run] rendered the compose variables to a 0600 file in a 0700 temp dir (removed on exit)"
  if docker compose version >/dev/null 2>&1; then
    # -q prints nothing on success; errors are withheld so no value can leak.
    if HERDR_CP_FLEET_DIR="$FLEET_DIR" HERDR_CP_GITHUB_APP_DIR="$GITHUB_APP_DIR" \
        HERDR_CP_PIPELINES_DIR="$PIPELINES_DIR" HERDR_CP_IMAGE_TAG="$TAG" HERDR_CP_GIT_COMMIT="$SHA" \
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
  echo "[dry-run] control-plane-web and worker mount $FLEET_DIR read-only; the file is $MOUNT_TARGET"
  echo "[dry-run] control-plane-web mounts $GITHUB_APP_DIR read-only at /etc/herdr/github-app$([ -d "$GITHUB_APP_DIR" ] || echo ' (would create it empty, 0700)')"
  echo "[dry-run] control-plane-web mounts $PIPELINES_DIR read-only at /etc/herdr/pipelines$([ -d "$PIPELINES_DIR" ] || echo ' (missing: a real deploy creates it empty, 0755)'); after the build would install the herdr-pipelines-sync timer"
  echo "[dry-run] llm-proxy mounts only the llm slice (rendered to a temp dir; would install it at ~/.config/herdr-engineering/llm-proxy/)"
  if [ "$COLLAB" = 1 ]; then
    echo "[dry-run] rendered control_plane.collab to a temp slice; after the build would install it at ~/.config/herdr-engineering/collab/, the edge nginx.conf, and the collab-sync timer"
  fi
  if [ "$SHARE" = 1 ]; then
    echo "[dry-run] share links enabled: rendered the share slice to a temp dir (would install it at ~/.config/herdr-engineering/share/), the edge conf gains /s/, share-public would be force-recreated before the edge"
  fi
  echo "[dry-run] would build herdr-engineering:$TAG and recreate ${APP_SERVICES[*]} ${EDGE_SERVICE[*]}${SHARE_SERVICE[*]:+ ${SHARE_SERVICE[*]}}"
  exit 0
fi

PREVIOUS_IMAGE="$(docker inspect -f '{{.Config.Image}}' herdr-cp-web 2>/dev/null || true)"
echo "currently running: ${PREVIOUS_IMAGE:-nothing}"
STATE_HELPER="$HERE/deploy-state.py"

# What to roll back to: the commit recorded in the running image's receipt, deployed
# again with THAT commit's compose files. Only a commit whose compose.yaml carries
# `x-herdr-deploy-layout: 2` can run on this script's variables; anything older (or no
# receipt at all) gets no automatic rollback and a manual recipe instead.
PREV_COMMIT=""; PREV_SHA=""; PREV_TAG=""; PREV_COLLAB_HASH=""; ROLLBACK_NOTE=""
PREV_RECEIPT_TAG=""
if [ -n "$PREVIOUS_IMAGE" ]; then
  # Protected rollback uses image IDs as Config.Image. Recover its logical tag
  # from the actual image buildstamp, cross-checked against the existing receipt.
  PREV_RECEIPT_TAG="$(python3 "$STATE_HELPER" resolve-previous "$STATE_DIR" "$PREVIOUS_IMAGE")" || exit 1
fi
if [ -z "$PREVIOUS_IMAGE" ]; then
  ROLLBACK_NOTE="nothing was running (first deploy)"
elif ! [[ "$PREV_RECEIPT_TAG" =~ ^[0-9a-f]{12}$ ]] || [ ! -r "$STATE_DIR/$PREV_RECEIPT_TAG.json" ]; then
  ROLLBACK_NOTE="no deploy receipt for the running image $PREVIOUS_IMAGE"
else
  receipt_field() {
    python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(v if isinstance(v,str) else "")' \
      "$STATE_DIR/$PREV_RECEIPT_TAG.json" "$1" 2>/dev/null || true
  }
  CANDIDATE="$(receipt_field commit)"
  PREV_COLLAB_HASH="$(receipt_field collab_slice_sha256)"
  # (no `git show | grep -q` under pipefail: grep's early exit would SIGPIPE git)
  PREV_COMPOSE=""
  if [[ "$CANDIDATE" =~ ^[0-9a-f]{40}$ ]]; then
    PREV_COMPOSE="$(git -C "$REPO" show "$CANDIDATE:deploy/control-plane/compose.yaml" 2>/dev/null || true)"
  fi
  if ! [[ "$CANDIDATE" =~ ^[0-9a-f]{40}$ ]] \
      || ! git -C "$REPO" cat-file -e "$CANDIDATE^{commit}" 2>/dev/null; then
    ROLLBACK_NOTE="the receipt of $PREVIOUS_IMAGE names no commit present in this repository"
  elif [[ "$PREV_COMPOSE" != *"x-herdr-deploy-layout: 2"* ]]; then
    ROLLBACK_NOTE="the running commit ${CANDIDATE:0:12} predates the current deploy layout (directory-mounted central fleet config, no relative bind mounts)"
    PREV_COMMIT_OLD="$CANDIDATE"
  else
    PREV_COMMIT="$CANDIDATE"; PREV_SHA="${CANDIDATE:0:12}"; PREV_TAG="$PREV_RECEIPT_TAG"
  fi
fi
if [ -n "$PREV_COMMIT" ]; then
  echo "automatic rollback target: $PREVIOUS_IMAGE (commit $PREV_SHA)"
else
  echo "no automatic rollback for this deploy: $ROLLBACK_NOTE"
  if [[ "$PREVIOUS_IMAGE" == sha256:* ]]; then
    echo "cannot validate a rollback target for the pinned running image; refusing deployment" >&2
    exit 1
  fi
fi

if [ -z "$HEALTH_URL" ]; then
  # The UI is published on HERDR_CP_WEB_BIND:HERDR_CP_WEB_PUBLIC_PORT.
  BIND="$(grep -E '^HERDR_CP_WEB_BIND=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  PORT="$(grep -E '^HERDR_CP_WEB_PUBLIC_PORT=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  case "$BIND" in *:*) BIND="[$BIND]" ;; esac   # IPv6 literal
  HEALTH_URL="http://${BIND:-127.0.0.1}:${PORT:-80}/api/v1/health"
fi
HEALTH_TRIES="${HERDR_CP_HEALTH_TRIES:-900}"
HEALTH_INTERVAL="${HERDR_CP_HEALTH_INTERVAL:-2}"

COLLAB_HASH=""
if [ "$COLLAB" = 1 ]; then
  COLLAB_HASH="$(sha256sum "$NEW_COLLAB" | cut -d' ' -f1)"
  # collab-public runs as HERDR_COLLAB_CERT_UID and must be able to read its 0700 slice dir.
  WANT_UID="$(grep -E '^HERDR_COLLAB_CERT_UID=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"" || true)"
  WANT_UID="${WANT_UID:-1000}"
  HAVE_UID="$(stat -c %u "$COLLAB_DIR" 2>/dev/null || id -u)"
  if [ "$HAVE_UID" != "$WANT_UID" ]; then
    echo "WARNING: $COLLAB_DIR is owned by uid $HAVE_UID but collab-public runs as uid $WANT_UID" \
         "(control_plane.compose.HERDR_COLLAB_CERT_UID); it will not be able to read its config." \
         "Fix: run deploy.sh as that user, or set HERDR_COLLAB_CERT_UID to $HAVE_UID." >&2
  fi
  # share-public runs as the same uid: it reads its slice and writes the events directory
  if [ "$SHARE" = 1 ] && [ "$(id -u)" != "$WANT_UID" ]; then
    echo "WARNING: share-public runs as uid $WANT_UID (control_plane.compose.HERDR_COLLAB_CERT_UID) but this deploy" \
         "creates $SHARE_DIR and $HERDR_SHARE_EVENTS_DIR as uid $(id -u); it will not be able to read or write them." >&2
  fi
fi

# A failed build exits here: nothing on the host has been touched yet.
docker build -q -f "$WORK/deploy/fabric-stack/Dockerfile" --build-arg "GIT_COMMIT=$SHA" \
  -t "herdr-engineering:$TAG" "$WORK" >/dev/null

# Use this checkout's helper, not the target commit's possibly older deploy tooling.
# The selected canonical source stays the forward mount. Snapshots live outside
# ENV_DIR so cleanup, logout and a failed rollout cannot destroy recovery inputs.
ATTEMPT="$(python3 "$STATE_HELPER" prepare "$STATE_DIR" "$CENTRAL_REAL" "$FULL" \
  "$PREV_COMMIT" "$PREVIOUS_IMAGE" "$COLLAB")" || exit 1
if [ "$CENTRAL_HASH" != "$(sha256sum "$ATTEMPT/candidate/fleet-config.yaml" | cut -d' ' -f1)" ]; then
  python3 "$STATE_HELPER" outcome "$ATTEMPT" config_changed
  echo "canonical config changed during preparation; refusing to recreate services" >&2
  exit 1
fi
ROLLBACK_IMAGES=""
ABSENT="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["previous"].get("absent", [])))' \
  "$ATTEMPT/metadata.json" 2>/dev/null || true)"
if [ -n "$ABSENT" ]; then
  echo "missing service containers (will be created by this deploy): $ABSENT"
fi

# Keep the timer-driven collab-sync out of this deploy: take the SAME lock its once()
# takes and hold it (fd 8, released by the kernel on any exit) across every host and
# container change below, rollback included.
if [ "$COLLAB" = 1 ]; then
  COLLAB_LOCK="${HERDR_COLLAB_SYNC_DIR}.lock"
  mkdir -p "$(dirname "$COLLAB_LOCK")"
  exec 8>"$COLLAB_LOCK"
  if ! flock -w "$COLLAB_LOCK_WAIT" 8; then
    python3 "$STATE_HELPER" outcome "$ATTEMPT" collab_sync_busy
    echo "collab-sync is still running after ${COLLAB_LOCK_WAIT}s (lock $COLLAB_LOCK); nothing was changed. Retry, or raise --collab-lock-wait" >&2
    exit 1
  fi
  echo "holding the collab-sync lock ($COLLAB_LOCK) until this deploy exits"
fi
# the GitHub App key mount source must exist (empty is fine: github.app then fails closed)
if [ ! -e "$GITHUB_APP_DIR" ]; then
  mkdir -p "$(dirname "$GITHUB_APP_DIR")" && mkdir -m 700 "$GITHUB_APP_DIR"
fi
check_github_app_dir || exit 1
if [ ! -e "$PIPELINES_DIR" ]; then
  mkdir -p "$(dirname "$PIPELINES_DIR")" && mkdir -m 755 "$PIPELINES_DIR"
fi
check_pipelines_dir || exit 1

# ---- host files this deploy may change, backed up so every failure path restores them
BACKUP_DIR="$ENV_DIR/backup"; mkdir -p "$BACKUP_DIR"
BACKED_UP=()
backup_file() {   # <path>: remember its content (or that it did not exist)
  local key; key="$(printf '%s' "$1" | sha256sum | cut -c1-16)"
  if [ -e "$1" ]; then cp -p "$1" "$BACKUP_DIR/$key"; else : > "$BACKUP_DIR/$key.absent"; fi
  BACKED_UP+=("$1")
}
SYNC_STABLE="$HOME/.local/share/herdr-engineering/collab-sync.sh"
UNIT_DIR="$HOME/.config/systemd/user"
SYNC_UNITS=(herdr-collab-sync.service herdr-collab-sync.timer herdr-collab-cert-renew.service herdr-collab-cert-renew.timer)
backup_file "$LLM_DIR/llm-config.json"
PIPELINES_SYNC="$WORK/deploy/control-plane/pipelines-sync.sh"
if [ -f "$PIPELINES_SYNC" ]; then   # the pipelines-sync script, its validator copy and units
  for f in "$HOME/.local/share/herdr-engineering/pipelines-sync.sh" \
      "$HOME/.local/share/herdr-engineering/pipelines-validator/deployment_definitions.py" \
      "$UNIT_DIR/herdr-pipelines-sync.service" "$UNIT_DIR/herdr-pipelines-sync.timer"; do backup_file "$f"; done
fi
if [ "$COLLAB" = 1 ]; then
  backup_file "$COLLAB_CONFIG"
  backup_file "$HERDR_COLLAB_EDGE_CONF"
  backup_file "$SYNC_STABLE"
  for u in "${SYNC_UNITS[@]}"; do backup_file "$UNIT_DIR/$u"; done
fi
if [ "$SHARE" = 1 ]; then backup_file "$SHARE_CONFIG"; fi

# restore_host_state: every backed-up file back to its pre-deploy content. nginx.conf in
# place (same inode, then the edge reloads); the collab-sync units reload in systemd.
restore_host_state() {
  local f key rc=0
  for f in "${BACKED_UP[@]}"; do
    key="$(printf '%s' "$f" | sha256sum | cut -c1-16)"
    if [ -e "$BACKUP_DIR/$key.absent" ]; then
      rm -f "$f" || rc=1
    elif [ "$f" = "${HERDR_COLLAB_EDGE_CONF:-}" ]; then
      write_edge_conf "$BACKUP_DIR/$key" || rc=1
    else
      cp -p "$BACKUP_DIR/$key" "$f.restore.tmp" && mv -f "$f.restore.tmp" "$f" || rc=1
    fi
  done
  if [ "$COLLAB" = 1 ]; then reload_edge_if_changed || rc=1; fi
  if [ "$COLLAB" = 1 ] || [ -f "$PIPELINES_SYNC" ]; then systemctl --user daemon-reload >/dev/null 2>&1 || true; fi
  return "$rc"
}

install_private() {   # <src> <dest>: 0600 file in a 0700 dir, atomic
  mkdir -p "$(dirname "$2")" && chmod 700 "$(dirname "$2")" \
    && install -m 0600 "$1" "$2.install.tmp" && mv -f "$2.install.tmp" "$2"
}

# compose_run <worktree> <env file> <image tag> <commit> <compose args...>: that
# worktree's compose files with the given env file and this deploy's paths. Rollback
# passes the previous commit's worktree and an env file rendered by ITS code.
compose_run() {
  local tree="$1" envf="$2" tag="$3" commit="$4"; shift 4
  local files=(-f "$tree/deploy/control-plane/compose.yaml")
  if [ "$COLLAB" = 1 ]; then files+=(-f "$tree/deploy/control-plane/compose.collab.yaml"); fi
  if [ -n "$ROLLBACK_IMAGES" ]; then files+=(-f "$ROLLBACK_IMAGES"); fi
  HERDR_CP_FLEET_DIR="$FLEET_DIR" HERDR_LLM_SLICE_DIR="$LLM_DIR" \
    HERDR_CP_GITHUB_APP_DIR="$GITHUB_APP_DIR" HERDR_CP_PIPELINES_DIR="$PIPELINES_DIR" HERDR_CP_IMAGE_TAG="$tag" HERDR_CP_GIT_COMMIT="$commit" \
    docker compose --env-file "$envf" -p "$PROJECT" "${files[@]}" "$@"
}

# wait_removals: until no service container is mid-removal (gone or any other state
# is fine: compose creates a missing one). Bounded; true when nothing is removing.
REMOVAL_POLLS=60
wait_removals() {
  local i svc st busy
  for i in $(seq 1 "$REMOVAL_POLLS"); do
    busy=""
    for svc in "${APP_SERVICES[@]}" "${EDGE_SERVICE[@]}" "${SHARE_SERVICE[@]}"; do
      st="$(docker inspect -f '{{.State.Status}}' "$(container_of "$svc")" 2>/dev/null || echo missing)"
      [ "${st%% *}" != removing ] || busy="$busy $svc"
    done
    [ -n "$busy" ] || return 0
    [ "$i" = "$REMOVAL_POLLS" ] || sleep "$HEALTH_INTERVAL"
  done
  echo "still being removed:$busy" >&2
  return 1
}

# compose_up_retry <compose_run args...>: a recreate racing an earlier removal of the
# same container fails with "removal of container ... is already in progress". Wait for
# that removal and retry, a bounded number of times; every other failure returns as is.
REMOVAL_RETRIES=3
compose_up_retry() {
  local attempt rc err="$ENV_DIR/compose-up.err"
  for attempt in $(seq 0 "$REMOVAL_RETRIES"); do
    rc=0
    compose_run "$@" 2>"$err" || rc=$?
    cat "$err" >&2
    [ "$rc" != 0 ] || return 0
    grep -qE 'removal of container .* is already in progress' "$err" || return "$rc"
    [ "$attempt" != "$REMOVAL_RETRIES" ] || break
    echo "a container removal is still in progress; waiting for it, then retrying ($((attempt + 1))/$REMOVAL_RETRIES)" >&2
    wait_removals || return "$rc"
  done
  return "$rc"
}

compose_up() {   # <worktree> <env file> <tag> <commit>
  compose_up_retry "$1" "$2" "$3" "$4" up -d --no-deps --no-build "${APP_SERVICES[@]}" "${EDGE_SERVICE[@]}"
}

healthy_commit() {
  local want="$1" i body
  for i in $(seq 1 "$HEALTH_TRIES"); do
    body="$(curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null || true)"
    if printf '%s' "$body" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('status')=='ok' and str(d.get('commit','')).startswith(sys.argv[1]) else 1)" "$want" 2>/dev/null; then
      return 0
    fi
    [ "$i" = "$HEALTH_TRIES" ] || sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# services_settled: EVERY recreated container is running, not restarting, not unhealthy,
# and its restart count did not move between two consecutive samples (a crash loop
# fails the deploy even when the web answers health).
container_of() {
  case "$1" in
    control-plane-web) echo herdr-cp-web ;; control-plane-worker) echo herdr-cp-worker ;;
    llm-proxy) echo herdr-llm-proxy ;; collab-public) echo herdr-collab-public ;;
    collab-edge) echo herdr-collab-edge ;; share-public) echo herdr-share-public ;; *) echo "$1" ;;
  esac
}
services_settled() {
  local i svc name st status restarting count health ok counts prev="" bad
  for i in $(seq 1 "$HEALTH_TRIES"); do
    ok=1; counts=""; bad=""
    for svc in "${APP_SERVICES[@]}" "${EDGE_SERVICE[@]}" "${SHARE_SERVICE[@]}"; do
      name="$(container_of "$svc")"
      st="$(docker inspect -f '{{.State.Status}} {{.State.Restarting}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo "missing false 0 none")"
      read -r status restarting count health <<< "$st"
      counts="$counts $name=$count"
      if [ "$status" != running ] || [ "$restarting" = true ] \
          || [ "$health" = unhealthy ] || [ "$health" = starting ]; then
        [ "$health" = none ] && health=""
        ok=0; bad="$bad $svc($status${health:+/$health})"
      fi
    done
    if [ "$ok" = 1 ] && [ "$counts" = "$prev" ]; then return 0; fi
    if [ "$ok" = 1 ]; then prev="$counts"; else prev=""; fi
    [ "$i" = "$HEALTH_TRIES" ] || sleep "$HEALTH_INTERVAL"
  done
  echo "not settled:${bad:- restart count still rising}" >&2
  return 1
}

verify() {   # <commit>: the web reports it AND every recreated service is up
  healthy_commit "$1" && services_settled
}

stop_share_public() {   # never fatal; plain docker, so it works whichever compose files define the service
  if docker inspect herdr-share-public >/dev/null 2>&1; then
    docker stop herdr-share-public >/dev/null 2>&1 \
      || echo "WARNING: could not stop herdr-share-public (the edge conf in place does not route /s/)" >&2
  fi
}

forward() {
  install_private "$NEW_LLM" "$LLM_DIR/llm-config.json" || return 1
  # mount sources of web/worker (and share-public): created here, after the build, never before
  mkdir -p "$HERDR_SHARES_DIR" "$HERDR_SHARE_EVENTS_DIR" && chmod 755 "$HERDR_SHARES_DIR" "$HERDR_SHARE_EVENTS_DIR" \
    || return 1
  if [ "$SHARE" = 1 ]; then install_private "$NEW_SHARE" "$SHARE_CONFIG" || return 1; fi
  # the deploy/pipelines refresh timer; its first run is the timer's (a fetch never fails a deploy)
  if [ -f "$PIPELINES_SYNC" ]; then
    "$PIPELINES_SYNC" install --dir "$PIPELINES_DIR" || return 1
  fi
  if [ "$COLLAB" = 1 ]; then
    install_private "$NEW_COLLAB" "$COLLAB_CONFIG" || return 1
    mkdir -p "$HERDR_COLLAB_DATA_DIR" && chmod 700 "$HERDR_COLLAB_DATA_DIR" || return 1
    # keys + membership files, the edge certificate, the 1-minute timer and the cert renew
    # timer (pool, group and the invite provisioner's operator come from the slice; the
    # provisioner reads collab-public's log in the data dir it mounts). Runs BEFORE the new nginx.conf lands, so
    # a reload it triggers (changed certificate) still uses the running conf.
    # This deploy holds the sync lock, so the refresh it runs must be told so (an older
    # candidate's collab-sync.sh lacks the flag: its refresh skips, the timer catches up).
    local held=()
    if grep -q -- '--lock-held' "$WORK/deploy/control-plane/collab-sync.sh"; then held=(--lock-held)
    else echo "WARNING: this commit's collab-sync.sh cannot run under the deploy's lock; its refresh is left to the timer" >&2; fi
    "$WORK/deploy/control-plane/collab-sync.sh" install --config "$COLLAB_CONFIG" \
      --dir "$HERDR_COLLAB_SYNC_DIR" --data-dir "$HERDR_COLLAB_DATA_DIR" --cert-dir "$CERT_DIR_VALUE" "${held[@]}" || return 1
    mkdir -p "$(dirname "$HERDR_COLLAB_EDGE_CONF")" || return 1
    write_edge_conf "$NEW_EDGE" || return 1
  fi
  if [ "$SHARE" = 1 ]; then
    # Stateless and it reads its slice only at startup: recreated once, alone, BEFORE the edge (whose conf
    # now names it as an upstream nginx resolves at start and reload).
    compose_up_retry "$WORK" "$ENV_FILE" "$TAG" "$SHA" up -d --no-deps --no-build --force-recreate share-public >/dev/null || return 1
  fi
  if [ "$COLLAB" = 1 ] && [ "$COLLAB_HASH" != "$PREV_COLLAB_HASH" ]; then
    # A changed slice: collab-public reads it only at startup, so force ITS recreate -
    # once, alone, first - and leave it out of the up below. Recreating it twice back to
    # back is what raced Docker's removal of the old container (2026-10-29).
    compose_up_retry "$WORK" "$ENV_FILE" "$TAG" "$SHA" up -d --no-deps --no-build --force-recreate collab-public >/dev/null || return 1
    compose_up_retry "$WORK" "$ENV_FILE" "$TAG" "$SHA" up -d --no-deps --no-build "${APP_SERVICES[@]}" collab-edge >/dev/null || return 1
  else
    compose_up "$WORK" "$ENV_FILE" "$TAG" "$SHA" >/dev/null || return 1
  fi
  # The edge resolves collab-public once, at (re)load: a recreated collab-public has a
  # new address, so reload the edge after every collab up, not only on a new conf.
  if [ "$COLLAB" = 1 ]; then EDGE_CONF_CHANGED=1; reload_edge_if_changed || return 1; fi
  # Shares disabled: the edge no longer routes /s/; stop a share-public a previous deploy started (its
  # bundles stay on disk). Never fatal.
  if [ "$SHARE" = 0 ] && [ -n "$NEW_SHARE" ]; then stop_share_public; fi
  python3 "$STATE_HELPER" verify-forward "$ATTEMPT" || return 1
  verify "$SHA"
}

if forward; then
  # The receipt names the ref, commits and images (and only a hash of the collab
  # slice) - never configuration. json.dumps: a ref may contain quotes.
  python3 "$STATE_HELPER" receipt "$ATTEMPT" "$STATE_DIR/$SHA.json" "$REF" \
    "herdr-engineering:$TAG" "$PREVIOUS_IMAGE" "$COLLAB_HASH" "$(date -u +%FT%TZ)"
  python3 "$STATE_HELPER" outcome "$ATTEMPT" succeeded
  echo "ok: ${APP_SERVICES[*]}${EDGE_SERVICE[*]:+ ${EDGE_SERVICE[*]}}${SHARE_SERVICE[*]:+ ${SHARE_SERVICE[*]}} running $SHA (receipt $STATE_DIR/$SHA.json)"
  exit 0
fi

echo "deploy of $SHA failed (an install step or compose up failed, the health check did not report $SHA, or a recreated service is not running)" >&2
# Host files go back first (slices rendered by the previous deploy, its nginx.conf and
# collab-sync units), so a rollback - or the old containers left running - read them.
RESTORED=1
if ! restore_host_state; then
  RESTORED=0
  echo "WARNING: could not restore every host file this deploy changed" >&2
fi
if [ -z "$PREV_COMMIT" ]; then
  python3 "$STATE_HELPER" outcome "$ATTEMPT" failed_no_rollback
  echo "NOT rolled back: $ROLLBACK_NOTE." >&2
  if [ -n "${PREV_COMMIT_OLD:-}" ]; then
    echo "manual rollback (that commit's own deploy.sh reads ~/.config/herdr-engineering/control-plane.env):" >&2
    echo "  git -C $REPO worktree add /tmp/herdr-rollback $PREV_COMMIT_OLD && /tmp/herdr-rollback/deploy/control-plane/deploy.sh --ref $PREV_COMMIT_OLD --no-fetch" >&2
  elif [ -n "$PREVIOUS_IMAGE" ]; then
    echo "manual rollback: redeploy the commit $PREVIOUS_IMAGE was built from with that commit's deploy/control-plane/deploy.sh" >&2
  fi
  [ "$RESTORED" = 1 ] || exit 3
  exit 1
fi

echo "rolling back to $PREVIOUS_IMAGE with the compose files and code of commit $PREV_SHA" >&2
# share-public follows the RESTORED edge conf: when it routes /s/ and the previous commit defines the service,
# share-public is recreated from that commit with the restored slice (and verified with the rest); otherwise a
# running one is stopped, so a rollback never reports healthy while /s/ points at the failed release.
rollback_share() {
  local conf="${HERDR_COLLAB_EDGE_CONF:-}"
  SHARE_SERVICE=()
  if [ "$COLLAB" = 1 ] && [ -f "$conf" ] && grep -q 'location ^~ /s/' "$conf" \
      && grep -q '^  share-public:' "$PREV_WORK/deploy/control-plane/compose.collab.yaml" 2>/dev/null; then
    SHARE_SERVICE=(share-public)
    compose_up_retry "$PREV_WORK" "$PREV_ENV" "$PREV_TAG" "$PREV_SHA" \
      up -d --no-deps --no-build --force-recreate share-public >/dev/null
  else
    stop_share_public
  fi
}
PREV_CONFIG="$ATTEMPT/previous/fleet-config.yaml"
FLEET_DIR="$(dirname "$PREV_CONFIG")"
ROLLBACK_IMAGES="$ATTEMPT/rollback-images.json"
printf -v MANUAL '%q --ref %q --no-fetch --fleet-config %q' \
  "$REPO/deploy/control-plane/deploy.sh" "$PREV_COMMIT" "$PREV_CONFIG"
PREV_WORK="$ENV_DIR/rollback-worktree"
PREV_ENV="$ENV_DIR/rollback-compose.env"
# the compose env as the PREVIOUS commit's own code renders it
prev_render() {
  (cd "$PREV_WORK" && PYTHONPATH="$PREV_WORK" python3 -m herdr_engineering.cli \
    fleet config render-compose --path "$PREV_CONFIG" --out "$PREV_ENV") >/dev/null 2>&1
}
if [ "$RESTORED" = 1 ] \
    && git -C "$REPO" worktree add -q --detach "$PREV_WORK" "$PREV_COMMIT" >/dev/null 2>&1 \
    && prev_render \
    && compose_up "$PREV_WORK" "$PREV_ENV" "$PREV_TAG" "$PREV_SHA" >/dev/null \
    && rollback_share \
    && { [ "$COLLAB" = 0 ] || { EDGE_CONF_CHANGED=1; reload_edge_if_changed || true; }; } \
    && python3 "$STATE_HELPER" verify-rollback "$ATTEMPT" \
    && verify "$PREV_SHA"; then
  python3 "$STATE_HELPER" outcome "$ATTEMPT" rolled_back
  echo "rolled back to $PREVIOUS_IMAGE (commit $PREV_SHA) and healthy (all of: ${APP_SERVICES[*]}${EDGE_SERVICE[*]:+ ${EDGE_SERVICE[*]}}); the deploy of $SHA FAILED" >&2
  exit 1
fi
python3 "$STATE_HELPER" outcome "$ATTEMPT" rollback_failed
echo "FAILED TO ROLL BACK to $PREVIOUS_IMAGE (commit $PREV_SHA): the control plane may be down or mixed." >&2
echo "manual rollback: $MANUAL" >&2
echo "pinned rollback images and recovery metadata: $ATTEMPT" >&2
exit 3
