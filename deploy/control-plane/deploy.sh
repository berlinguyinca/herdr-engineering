#!/usr/bin/env bash
# Build the control-plane web image from a git ref and roll it, with rollback.
#
#   deploy/control-plane/deploy.sh [--ref origin/main] [--dry-run] [--no-fetch]
#                                  [--fleet-config PATH] [--collab-lock-wait SECONDS]
#                                  [--deploy-lock-wait SECONDS]
#                                  [--checkpoint-lock-wait SECONDS] [--pause-worker]
#                                  [--with-memory [--memory-checkout PATH]]
#                                  [--memory-client-config PATH]
#                                  [--memory-private-egress --memory-private-egress-gateway IPV4/32]
#                                  [--no-memory-private-egress]
#                                  [--memory-private-egress-cidr CIDR]
#                                  [--memory-private-egress-recover SAVED_ATTEMPT]
#                                  [--allow-divergent]
#   HEAD expressions use the caller worktree only after common-repository proof.
#   Normal rollouts must descend from the verified deployed commit; intentional
#   manual rollback requires --allow-divergent (automatic rollback is unchanged).
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
#      nginx.conf rendered with the share host's own server block (control_plane.share.public_url; the edge
#      certificate must name that host, or the deploy is refused), and share-public force-recreated once,
#      before the edge;
#      disabled, a running share-public is stopped and the edge conf is the repo file unchanged. A rollback
#      recreates share-public from the previous commit when the restored edge conf routes /s/, else stops it
#      Artifact host (member-owned artifacts): control_plane.artifact_host is always validated
#      (render-artifact-host; an enabled but incomplete block is refused before anything runs, no value
#      printed). Only when it is enabled (which needs the collaboration marker, and share-public off) is its
#      configuration installed 0644 in a 0700 directory (Compose ignores secret modes; see compose.yaml),
#      its image built from the same commit, the edge conf rendered with its server block and /mcp/artifacts,
#      and the host project (deploy/artifact-host/compose.yaml: db, public, internal) migrated and brought up
#      healthy BEFORE the control plane's compose up, which attaches control-plane-web and the edge to the
#      host's two networks (compose.artifact-host.yaml). `--dry-run` renders and validates all of it.
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
# The checkpoint prewarm needs the per-schema projection materializer lock, which the
# running worker's periodic refresh holds for most of each cycle. By default the prewarm
# waits for it (--checkpoint-lock-wait, default 420s: longer than one 300s refresh cycle
# plus a pass), failing with the lock's reason only after that. --pause-worker (opt-in)
# instead stops herdr-cp-worker for the prewarm and starts it again straight after, on
# success, failure or interrupt; the worker's jobs (artifacts, collab bridge) idle for
# that time. See docs/operations/projection-checkpoints.md.
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
# OPTIONAL Hindsight shared memory (--with-memory, off unless given; docs/operations/memory-hindsight.md):
# after the control plane above is deployed, verified and receipted, a SEPARATE health-gated step builds the pinned
# herdr-memory image (lock/upstreams.yaml `herdr-memory-deploy`: revision, default checkout; --memory-checkout or
# HERDR_MEMORY_CHECKOUT override the path) and recreates ONLY hindsight-db and hindsight-api (compose profile
# `memory`). Refused before anything changes unless: this is an amd64 host (the pinned image digests are linux/amd64),
# control_plane.memory.enabled is true and complete in the central file, the checkout holds the pinned revision, and
# (real deploy) the model directory has onnx/model.onnx. The memory step can never take the control plane down: it
# touches no control-plane container; a failure there exits 4 with the control plane running as deployed.
#   Rollback of the memory step: the previous api image is re-tagged and the api recreated from the PREVIOUS config
#   snapshot, then verified; if it cannot be restored it says so (exit 4, "FAILED TO ROLL BACK memory"). The database
#   service and its volume are never rolled back or deleted. FIRST memory deploy: there is nothing to roll back to,
#   so a failed first deploy STOPS the new memory containers (data volume kept) and says so. --dry-run validates all
#   of it (config, pin, checkout, arch, `docker compose --profile memory config`) and changes nothing.
#
#   HERDR_CP_HEALTH_URL / _TRIES (900) / _INTERVAL (2s) tune the health check.
#
# The central file is the only configuration source: no env file is authored or
# kept. Nothing from it is printed by this script or written to a receipt.
set -euo pipefail

REF="origin/main"; DRY=0; FETCH=1; ALLOW_DIVERGENT=0; COLLAB_LOCK_WAIT=300; DEPLOY_LOCK_WAIT=600
CHECKPOINT_LOCK_WAIT=420; PAUSE_WORKER=0; WORKER_PAUSED=0
MEMORY_PRIVATE_EGRESS=auto; MEMORY_PRIVATE_EGRESS_RECOVER=""
MEMORY_PRIVATE_EGRESS_CIDR="172.31.252.0/29"
MEMORY_PRIVATE_EGRESS_GATEWAY="${HERDR_CP_MEMORY_PRIVATE_EGRESS_GATEWAY:-}"
MEMORY=0; MEMORY_CHECKOUT="${HERDR_MEMORY_CHECKOUT:-}"
MEMORY_CLIENT_CONFIG="${HERDR_CP_MEMORY_CLIENT_CONFIG:-}"
CENTRAL="$HOME/.config/herdr-engineering/fleet/fleet-config.yaml"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    --allow-divergent) ALLOW_DIVERGENT=1; shift ;;
    --fleet-config) CENTRAL="${2:?--fleet-config needs a value}"; shift 2 ;;
    --collab-lock-wait) COLLAB_LOCK_WAIT="${2:?--collab-lock-wait needs a value}"; shift 2 ;;
    --deploy-lock-wait) DEPLOY_LOCK_WAIT="${2:?--deploy-lock-wait needs a value}"; shift 2 ;;
    --checkpoint-lock-wait) CHECKPOINT_LOCK_WAIT="${2:?--checkpoint-lock-wait needs a value}"; shift 2 ;;
    --pause-worker) PAUSE_WORKER=1; shift ;;
    --memory-private-egress-recover) MEMORY_PRIVATE_EGRESS_RECOVER="${2:?needs the saved deploy attempt}"; shift 2 ;;
    --memory-private-egress) MEMORY_PRIVATE_EGRESS=1; shift ;;
    --no-memory-private-egress) MEMORY_PRIVATE_EGRESS=0; shift ;;
    --memory-private-egress-cidr) MEMORY_PRIVATE_EGRESS_CIDR="${2:?needs a CIDR}"; shift 2 ;;
    --memory-private-egress-gateway) MEMORY_PRIVATE_EGRESS_GATEWAY="${2:?needs an explicit IPv4/32}"; shift 2 ;;
    --with-memory) MEMORY=1; shift ;;
    --memory-checkout) MEMORY_CHECKOUT="${2:?--memory-checkout needs a value}"; shift 2 ;;
    --memory-client-config) MEMORY_CLIENT_CONFIG="${2:?--memory-client-config needs a path}"; shift 2 ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/p}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$COLLAB_LOCK_WAIT" =~ ^[0-9]+$ ]] || { echo "--collab-lock-wait needs a whole number of seconds" >&2; exit 2; }
[[ "$CHECKPOINT_LOCK_WAIT" =~ ^[0-9]+$ ]] || { echo "--checkpoint-lock-wait needs a whole number of seconds" >&2; exit 2; }
[[ "$DEPLOY_LOCK_WAIT" =~ ^[0-9]+$ ]] || { echo "--deploy-lock-wait needs a whole number of seconds" >&2; exit 2; }
if [ -n "$MEMORY_CLIENT_CONFIG" ]; then
  MEMORY_CLIENT_CONFIG="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().absolute())' "$MEMORY_CLIENT_CONFIG")"
fi

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
REF_REPO="$REPO"
# The interpreter for every python helper below: the repository's own virtualenv when it has one (the fleet config needs
# PyYAML and pydantic, which a bare system python3 may lack), else python3. HERDR_DEPLOY_PYTHON overrides.
PY="${HERDR_DEPLOY_PYTHON:-}"
[ -n "$PY" ] || { [ -x "$REPO/.venv/bin/python" ] && PY="$REPO/.venv/bin/python"; } || PY=python3
# The artifact host's names, networks and addresses are FIXED: the edge's :8082 deny, the internal role's bind address and
# the project the rollback and the disable path stop are all derived from them. They exist as environment variables only
# so the real-Docker tests can run a second copy beside a live one; a deploy refuses them rather than let a stray export
# make the deny list describe a different network than the one Docker creates.
for v in HERDR_ARTIFACT_HOST_PROJECT HERDR_ARTIFACT_NET_PREFIX HERDR_ARTIFACT_EDGE_SUBNET HERDR_ARTIFACT_INTERNAL_SUBNET \
    HERDR_ARTIFACT_INTERNAL_IP HERDR_ARTIFACT_HOST_DIR HERDR_ARTIFACT_HOST_TAG; do
  if [ -n "${!v:-}" ]; then
    echo "$v is set in the environment: the artifact host's project, networks and addresses are fixed by the repository" \
         "and are not deploy settings (unset $v)" >&2
    exit 2
  fi
done
case "$REF" in
  HEAD|HEAD[~^:@]*|@|@[~^:]*|'@{'*)
    # A canonical tool worktree has its own HEAD. Never substitute that for the
    # caller's revision, and never trust an unrelated clone merely sharing SHAs.
    CALLER_COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
    TOOL_COMMON="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)"
    if [ -z "$CALLER_COMMON" ] || ! [ "$CALLER_COMMON" -ef "$TOOL_COMMON" ]; then
      echo "cannot resolve caller-local HEAD without a shared Git repository; use --ref <published ref> or --ref <full commit SHA>" >&2
      exit 2
    fi
    REF_REPO="$(pwd -P)"
    ;;
esac
PROJECT="${HERDR_CP_PROJECT:-herdr-control-plane}"
HEALTH_URL="${HERDR_CP_HEALTH_URL:-}"
STATE_DIR="$HOME/.local/state/herdr-engineering/deploys"
STATE_HELPER="$HERE/deploy-state.py"
# shellcheck source=deploy-lock.sh
source "$HERE/deploy-lock.sh"

# Permission-only hard-interruption recovery. Reuse the SAME lock and saved
# attempt; never guess or recreate an image/config/profile from partial state.
if [ -n "$MEMORY_PRIVATE_EGRESS_RECOVER" ]; then
  [ "$DRY" = 0 ] || { echo "permission recovery is explicit and cannot run in --dry-run; no changes made" >&2; exit 2; }
  deploy_lock_acquire || exit $?
  trap 'deploy_lock_cleanup' EXIT
  trap '' INT TERM HUP
  deploy_compat_lock_acquire || exit $?
  source "$HERE/web-egress-deploy.sh"
  RECOVER_ATTEMPT="$("$PY" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' "$MEMORY_PRIVATE_EGRESS_RECOVER")" || exit 3
  web_egress_cmd recover --current "$STATE_DIR/web-egress/current.json" \
    --attempt "$RECOVER_ATTEMPT/web-egress" --lock-fd "$DEPLOY_LOCK_FD" \
    --lock-path "$STATE_DIR/rollout.lock" || exit 3
  echo "private egress permission recovery phase finished; network/image/config/profile restoration is NOT completed; preserve and review the saved attempt" >&2
  exit 3
fi
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

# Assistant app connections (personal_assistant.apps): the AES key that encrypts stored Google refresh tokens, in its
# OWN read-only mount. deploy.sh creates the (empty) directory; the operator creates apps.key, never this script.
APPS_DIR="$HOME/.config/herdr-engineering/apps"
check_apps_dir() {   # names and sizes only, never content
  local d="$APPS_DIR" mode others f
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
  others="$(find "$d" -mindepth 1 -maxdepth 1 ! -name apps.key -printf '%f ' 2>/dev/null || true)"
  if [ -n "$others" ]; then
    echo "$d must hold only apps.key (the directory is mounted into control-plane-web); also found: $others" >&2
    return 1
  fi
  f="$d/apps.key"
  [ -e "$f" ] || [ -L "$f" ] || return 0
  if [ -L "$f" ] || [ ! -f "$f" ]; then
    echo "$f must be a regular file (not a symlink)" >&2; return 1
  fi
  if [ "$(stat -c %u "$f")" != "$(id -u)" ]; then
    echo "$f must be owned by the deploying user (uid $(id -u))" >&2; return 1
  fi
  mode="$(stat -c %a "$f")"
  if [ "$mode" != 600 ]; then
    echo "$f must be mode 0600 (it is $mode): chmod 600 $f" >&2; return 1
  fi
  if [ "$(stat -c %s "$f")" != 32 ]; then
    echo "$f must be exactly 32 bytes (it is $(stat -c %s "$f")): head -c 32 /dev/urandom > $f" >&2; return 1
  fi
}
check_apps_dir || exit 1

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
HOST_MUTATED=0
DEPLOY_FINISHED=0
# Start the worker again if --pause-worker stopped it. Idempotent; never fatal.
# shellcheck disable=SC2317
resume_worker() {
  [ "$WORKER_PAUSED" = 1 ] || return 0
  if docker start herdr-cp-worker >/dev/null 2>&1; then
    echo "resumed herdr-cp-worker"
  else
    echo "WARNING: could not start herdr-cp-worker; start it with: docker start herdr-cp-worker" >&2
    return 0
  fi
  WORKER_PAUSED=0
}
# Called by the EXIT trap via finish().
# shellcheck disable=SC2317
cleanup() {
  if [ -n "$PREV_WORK" ]; then git -C "$REPO" worktree remove --force "$PREV_WORK" >/dev/null 2>&1 || true; fi
  [ -z "$ENV_DIR" ] || rm -rf "$ENV_DIR"
  if [ -n "$WORK" ]; then
    if [ "$DRY" = 1 ]; then rm -rf "$WORK"
    else git -C "$REPO" worktree remove --force "$WORK" >/dev/null 2>&1 || rm -rf "$WORK"; fi
  fi
  deploy_lock_cleanup
}
# shellcheck disable=SC2317
finish() {
  local status=$? rollback_status=0
  # Repeated interrupts must not cut short recovery or release ownership early.
  trap '' INT TERM HUP
  trap - EXIT
  resume_worker
  if [ "$HOST_MUTATED" = 1 ] && [ "$DEPLOY_FINISHED" = 0 ]; then
    rollback || rollback_status=$?
    [ "$rollback_status" != 3 ] || status=3
  fi
  deploy_lock_phase cleanup || true
  cleanup
  exit "$status"
}
trap finish EXIT
trap 'trap "" INT TERM HUP; exit 130' INT
trap 'trap "" INT TERM HUP; exit 143' TERM HUP

# One deploy at a time (both locks released by the kernel on any exit). A dry run changes
# nothing, so it neither takes nor waits for the lock.
if [ "$DRY" = 0 ]; then
  deploy_lock_acquire || exit $?
  DEPLOY_LOCK="$STATE_DIR/deploy.lock"
  if ! deploy_compat_lock_acquire; then
    echo "another deploy.sh is still running after ${DEPLOY_LOCK_WAIT}s (lock $DEPLOY_LOCK); nothing was changed. Retry, or raise --deploy-lock-wait" >&2
    exit 1
  fi
fi

if [ "$DRY" = 0 ] && [ "$FETCH" = 1 ]; then git -C "$REPO" fetch -q origin; fi
FULL="$(git -C "$REF_REPO" rev-parse --verify "$REF^{commit}")"
SHA="${FULL:0:12}"
TAG="$SHA"

WORK="$(mktemp -d)"
if [ "$DRY" = 1 ]; then
  git -C "$REPO" archive "$FULL" | tar -x -C "$WORK"
else
  git -C "$REPO" worktree add -q --detach "$WORK" "$FULL"
fi
# herdr-eng from exactly the commit being deployed, so the rendered variables match
# the compose files that ship with it.
# Run from inside $WORK: `"$PY" -m` puts the cwd first on sys.path, so a caller
# standing in another checkout would otherwise get that checkout's code.
herdr_eng() { (cd "$WORK" && PYTHONPATH="$WORK" "$PY" -m herdr_engineering.cli "$@"); }

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

# ---- optional Hindsight memory: validated BEFORE anything changes (also in --dry-run) ----
MEM_PIN=""; MEM_CK=""; MEM_TAG=""; MEM_PORT=""; MEM_MODEL_DIR=""; MEM_STATE_DIR="$HOME/.local/state/herdr-engineering/memory"
if [ "$MEMORY" = 1 ]; then
  # the memory functions come from the commit being deployed (same rule as herdr_eng): sourced, not forked
  MEM_LIB="$WORK/deploy/control-plane/memory-deploy.sh"
  [ -r "$MEM_LIB" ] || { echo "--with-memory: $MEM_LIB is missing in $SHA; nothing was changed" >&2; exit 1; }
  # shellcheck source=memory-deploy.sh
  . "$MEM_LIB"
fi
if [ "$MEMORY" = 1 ]; then memory_preflight || exit 1; fi

# The read-only explorer's CLI/profile use the app's existing rollout, lock
# and rollback, independently of the optional backend memory step.
MEMORY_CLIENT_LIB="$WORK/deploy/control-plane/memory-client-deploy.sh"
[ -f "$MEMORY_CLIENT_LIB" ] || MEMORY_CLIENT_LIB="$HERE/memory-client-deploy.sh"
. "$MEMORY_CLIENT_LIB"
memory_client_preflight || exit 1
# Private bridge activation is an explicit, separately reviewed opt-in.
source "$HERE/web-egress-deploy.sh"
web_egress_preflight || exit 1

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
  export HERDR_COLLAB_MIRROR_DIR="$ENV_DIR/collab-mirror"
  export HERDR_COLLAB_OUTBOX_DIR="$ENV_DIR/collab-outbox"
  export HERDR_COLLAB_SPOOL_DIR="$ENV_DIR/collab-spool"
else
  SHARE_DIR="$HOME/.config/herdr-engineering/share"
  if [ "$SHARE" = 1 ]; then only_file "$SHARE_DIR" share-config.json || exit 1; fi
  # bundles (web writes, worker sweeps, share-public reads) and share-public's events file (worker reads)
  export HERDR_SHARES_DIR="$HOME/.local/share/herdr-engineering/shares"
  export HERDR_SHARE_EVENTS_DIR="$HOME/.local/share/herdr-engineering/share-events"
  # collaborator bridge, beside them on the same large storage: the worker's mirror (collab-public reads it) and
  # spool (assembled uploads, worker-private), and the outbox collab-public writes. Pairwise disjoint siblings.
  export HERDR_COLLAB_MIRROR_DIR="$HOME/.local/share/herdr-engineering/collab-mirror"
  export HERDR_COLLAB_OUTBOX_DIR="$HOME/.local/share/herdr-engineering/collab-outbox"
  export HERDR_COLLAB_SPOOL_DIR="$HOME/.local/share/herdr-engineering/collab-spool"
fi
export HERDR_SHARE_CONFIG_DIR="$SHARE_DIR"
SHARE_CONFIG="$SHARE_DIR/share-config.json"
SHARE_SERVICE=()
[ "$SHARE" = 0 ] || SHARE_SERVICE=(share-public)

# Artifact host: a DIRECTORY rendered from control_plane.artifact_host, the Cognito identifiers of control_plane.collab and
# the digest of control_plane.artifact_library.host_key: one config slice per role (public.json, internal.json,
# migrate.json), the database admin's password and roles.sql. Always rendered (to validate), installed only when enabled.
# An older candidate without the command has no host wiring at all.
ARTIFACT_HOST=0; NEW_AH=""
AH_PROJECT="herdr-artifact-host"
if grep -q '"render-artifact-host"' "$WORK/herdr_engineering/cli.py" 2>/dev/null \
    && [ -f "$WORK/deploy/artifact-host/compose.yaml" ] && [ -f "$WORK/deploy/control-plane/compose.artifact-host.yaml" ]; then
  NEW_AH="$ENV_DIR/new/artifact-host"
  if ! AH_OUT="$(herdr_eng fleet config render-artifact-host --path "$CENTRAL" --out "$NEW_AH")"; then
    echo "control_plane.artifact_host is incomplete or invalid in $CENTRAL; nothing was changed" >&2
    exit 1
  fi
  case "$AH_OUT" in *artifact_host=enabled) ARTIFACT_HOST=1 ;; esac
fi
if [ "$ARTIFACT_HOST" = 1 ] && [ "$COLLAB" = 0 ]; then
  echo "control_plane.artifact_host.enabled is true but the collaboration network is off: the host is served behind" \
       "the collab edge (create $COLLAB_MARKER), or set control_plane.artifact_host.enabled: false" >&2
  exit 1
fi
if [ "$DRY" = 1 ]; then AH_DIR="$NEW_AH"; else AH_DIR="$HOME/.config/herdr-engineering/artifact-host"; fi
AH_FILES=(public.json internal.json migrate.json db-admin-password roles.sql)
AH_PUBLIC_HOST=""
[ "$ARTIFACT_HOST" = 1 ] && { AH_PUBLIC_HOST="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["public_host"])' "$NEW_AH/public.json")" \
  && [ -n "$AH_PUBLIC_HOST" ] || { echo "cannot read the artifact host's public name from its rendered slice" >&2; exit 1; }; }
# What the PREVIOUS deploy left running: the marker it wrote on success, else (older deploys) whether the project has
# containers. A rollback restores THIS, not whatever the new configuration says.
AH_STATE="$HOME/.config/herdr-engineering/artifact-host.state"
PREV_AH=0
if [ -f "$AH_STATE" ]; then [ "$(cat "$AH_STATE" 2>/dev/null)" != enabled ] || PREV_AH=1
elif [ -n "$(docker ps -q --filter "label=com.docker.compose.project=$AH_PROJECT" 2>/dev/null || true)" ]; then PREV_AH=1; fi
# ah_run <worktree> <image tag> <compose args...>: the artifact host's own project (that worktree's compose file, none of the
# control plane's), with this deploy's config directory. The only place the host's variables are set (see the refusal above).
ah_run() {
  local tree="$1" tag="$2"; shift 2
  HERDR_ARTIFACT_HOST_TAG="$tag" HERDR_ARTIFACT_HOST_DIR="$AH_DIR" \
    docker compose -p "$AH_PROJECT" -f "$tree/deploy/artifact-host/compose.yaml" "$@"
}
ah_compose() { ah_run "$WORK" "$TAG" "$@"; }
AH_MIGRATED=0        # set when the deploy may have changed the SCHEMA (a failed migration, or the applied-migration count moved)
AH_MANUAL=0          # a rollback that could not revert the host and says so
AH_BACKUP_FILE=""

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
  # The edge conf for this config: the repo file, plus the share host server only when share is enabled.
  NEW_EDGE="$ENV_DIR/new/collab-edge/nginx.conf"
  EDGE_TEMPLATE="$WORK/deploy/control-plane/collab-edge/nginx.conf"
  if [ -n "$NEW_SHARE" ] || [ -n "$NEW_AH" ]; then
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

# Fabric-trust listener (SPEC 6a): only with the collab overlay AND control_plane.collab.fabric_trust.listen_* set.
# The address was validated (tailnet range) by the config; docker publishes exactly that host:port.
FABRIC=0
FABRIC_SERVICE_ORPHAN=0
FABRIC_SERVICE=()   # the services only the fabric overlay defines; rebuilt per checkout on rollback
# An older candidate (a rollback target, a pinned --ref) has neither the helper nor the overlay: no listener then.
if [ "$COLLAB" = 1 ] && grep -q 'fabric-listener' "$WORK/herdr_engineering/cli.py" 2>/dev/null \
    && [ -f "$WORK/deploy/control-plane/compose.collab-fabric.yaml" ]; then
  if ! FABRIC_LISTEN="$(herdr_eng fleet config fabric-listener --path "$CENTRAL")"; then
    echo "control_plane.collab.fabric_trust is invalid in $CENTRAL" >&2
    exit 1
  fi
  if [ -n "$FABRIC_LISTEN" ]; then
    FABRIC=1
    read -r FABRIC_HOST FABRIC_PORT <<< "$FABRIC_LISTEN"
    export HERDR_COLLAB_FABRIC_BIND="$FABRIC_HOST" HERDR_COLLAB_FABRIC_PORT="$FABRIC_PORT"
    if [ "$DRY" = 1 ]; then export HERDR_COLLAB_FABRIC_WHOIS_DIR="$ENV_DIR/fabric-whois"
    else export HERDR_COLLAB_FABRIC_WHOIS_DIR="$HOME/.local/share/herdr-engineering/collab-fabric-whois"; fi
    COLLAB_FILES+=(-f "$WORK/deploy/control-plane/compose.collab-fabric.yaml")
    EDGE_SERVICE=(fabric-whois "${EDGE_SERVICE[@]}")
    FABRIC_SERVICE=(fabric-whois)
    echo "fabric-trust listener: $FABRIC_HOST:$FABRIC_PORT (whois sidecar fabric-whois)"
    # Host hardening the listener relies on but a deploy cannot install (root): warn, never fail.
    FABRIC_FW="$WORK/deploy/control-plane/collab-fabric-firewall.sh"
    if [ -x "$FABRIC_FW" ] && ! "$FABRIC_FW" --check >/dev/null 2>&1; then
      echo "WARNING: fabric network firewall is not confirmed present (or not readable without root): collab-public's network has a default route" \
        "and can reach this host's own addresses. As root run: $FABRIC_FW --install  (docs/operations/fabric-trust.md, Required hardening)" >&2
    fi
    if [ ! -e /etc/systemd/system/docker.service.d/after-tailscale.conf ]; then
      echo "WARNING: docker is not ordered after tailscaled: after a reboot the published fabric port may fail to bind and the PUBLIC collab site" \
        "stay down. Install $WORK/deploy/control-plane/docker-after-tailscale.conf as /etc/systemd/system/docker.service.d/after-tailscale.conf" >&2
    fi
  fi
fi

COMPOSE_FILES=(-f "$WORK/deploy/control-plane/compose.yaml" "${COLLAB_FILES[@]}")
if [ "$WEB_EGRESS_COMPOSE_ENABLED" = 1 ]; then
  COMPOSE_FILES+=(-f "$WEB_EGRESS_OVERLAY")
fi
if [ "$ARTIFACT_HOST" = 1 ] && [ -f "$WORK/deploy/control-plane/compose.artifact-host.yaml" ]; then
  COMPOSE_FILES+=(-f "$WORK/deploy/control-plane/compose.artifact-host.yaml")
fi
export HERDR_CP_WEB_EGRESS_CIDR="$MEMORY_PRIVATE_EGRESS_CIDR"
export HERDR_CP_WEB_EGRESS_GENERATION="$WEB_EGRESS_GENERATION"
# Every service of ours that runs the herdr-engineering image and reads the fleet
# config: rolled forward (and back) together on one tag.
APP_SERVICES=(control-plane-web control-plane-worker llm-proxy)

if [ "$DRY" = 1 ]; then
  echo "[dry-run] rendered the compose variables to a 0600 file in a 0700 temp dir (removed on exit)"
  if docker compose version >/dev/null 2>&1; then
    # -q prints nothing on success; errors are withheld so no value can leak.
    if HERDR_CP_FLEET_DIR="$FLEET_DIR" HERDR_CP_GITHUB_APP_DIR="$GITHUB_APP_DIR" \
        HERDR_CP_APPS_DIR="$APPS_DIR" HERDR_CP_PIPELINES_DIR="$PIPELINES_DIR" HERDR_CP_IMAGE_TAG="$TAG" HERDR_CP_GIT_COMMIT="$SHA" \
        docker compose --env-file "$ENV_FILE" -p "$PROJECT" "${COMPOSE_FILES[@]}" config -q >/dev/null 2>&1; then
      echo "[dry-run] docker compose config: valid"
    else
      echo "[dry-run] docker compose config FAILED (details withheld; they could contain values)" >&2
      exit 1
    fi
    if [ "$ARTIFACT_HOST" = 1 ]; then
      if ah_run "$WORK" "$TAG" config -q >/dev/null 2>&1; then
        echo "[dry-run] artifact host docker compose config: valid"
      else
        echo "[dry-run] artifact host docker compose config FAILED (details withheld; they could contain values)" >&2
        exit 1
      fi
    fi
  else
    echo "[dry-run] docker compose not available: skipped offline compose validation"
  fi
  if [ "$MEMORY" = 1 ]; then
    if docker compose version >/dev/null 2>&1; then
      if HERDR_CP_FLEET_DIR="$FLEET_DIR" HERDR_CP_GITHUB_APP_DIR="$GITHUB_APP_DIR" \
          HERDR_CP_APPS_DIR="$APPS_DIR" HERDR_CP_PIPELINES_DIR="$PIPELINES_DIR" HERDR_CP_IMAGE_TAG="$TAG" HERDR_CP_GIT_COMMIT="$SHA" \
          docker compose --env-file "$ENV_FILE" -p "$PROJECT" --profile memory "${COMPOSE_FILES[@]}" config -q >/dev/null 2>&1; then
        echo "[dry-run] docker compose --profile memory config: valid"
      else
        echo "[dry-run] docker compose --profile memory config FAILED (details withheld; they could contain values)" >&2
        exit 1
      fi
    fi
    echo "[dry-run] memory: amd64 host, control_plane.memory complete, pin $MEM_PIN found in $MEM_CK"
    echo "[dry-run] memory: after the control plane is verified, would build herdr-memory-hindsight:$MEM_TAG from the pinned checkout and recreate ONLY hindsight-db and hindsight-api (loopback port $MEM_PORT); a failure exits 4 and never touches the control plane"
    echo "[dry-run] memory: would install the backup timer (herdr-hindsight-backup.timer) and record ~/.local/state/herdr-engineering/memory/deployed.json"
  fi
  echo "[dry-run] would run: herdr-eng fleet config render-compose --path <central> --out <file>"
  echo "[dry-run] control-plane-web and worker mount $FLEET_DIR read-only; the file is $MOUNT_TARGET"
  echo "[dry-run] control-plane-web mounts $GITHUB_APP_DIR read-only at /etc/herdr/github-app$([ -d "$GITHUB_APP_DIR" ] || echo ' (would create it empty, 0700)')"
  echo "[dry-run] control-plane-web mounts $APPS_DIR read-only at /etc/herdr/apps$([ -d "$APPS_DIR" ] || echo ' (missing: a real deploy makes it empty, 0700; the operator adds apps.key)')"
  echo "[dry-run] control-plane-web mounts $PIPELINES_DIR read-only at /etc/herdr/pipelines$([ -d "$PIPELINES_DIR" ] || echo ' (missing: a real deploy creates it empty, 0755)'); after the build would install the herdr-pipelines-sync timer"
  echo "[dry-run] llm-proxy mounts only the llm slice (rendered to a temp dir; would install it at ~/.config/herdr-engineering/llm-proxy/)"
  if [ "$COLLAB" = 1 ]; then
    echo "[dry-run] rendered control_plane.collab to a temp slice; after the build would install it at ~/.config/herdr-engineering/collab/, the edge nginx.conf, and the collab-sync timer"
  fi
  if [ "$SHARE" = 1 ]; then
    echo "[dry-run] share links enabled: rendered the share slice to a temp dir (would install it at ~/.config/herdr-engineering/share/), the edge conf gains the share host's server block, the edge certificate must name that host, share-public would be force-recreated before the edge"
  fi
  if [ "$ARTIFACT_HOST" = 1 ]; then
    echo "[dry-run] artifact host enabled: rendered its configuration to a temp dir (would install it in a 0700 $AH_DIR), the edge conf gains its server block and /mcp/artifacts, would build herdr-artifact-host:$TAG, create the database roles, back the database up, run migrate, and bring up project $AH_PROJECT (db, public, internal, relay) healthy BEFORE the control plane's compose up"
  fi
  echo "[dry-run] would prewarm the target image checkpoint on an existing deployment when its CLI supports it, before host or service changes (waiting up to ${CHECKPOINT_LOCK_WAIT}s for the worker's materializer lock$([ "$PAUSE_WORKER" = 1 ] && echo '; --pause-worker: herdr-cp-worker is stopped for the prewarm and started again after'))"
  echo "[dry-run] would build herdr-engineering:$TAG and recreate ${APP_SERVICES[*]} ${EDGE_SERVICE[*]}${SHARE_SERVICE[*]:+ ${SHARE_SERVICE[*]}}"
  exit 0
fi

PREVIOUS_IMAGE="$(docker inspect -f '{{.Config.Image}}' herdr-cp-web 2>/dev/null || true)"
echo "currently running: ${PREVIOUS_IMAGE:-nothing}"
# What to roll back to: the commit recorded in the running image's receipt, deployed
# again with THAT commit's compose files. Only a commit whose compose.yaml carries
# `x-herdr-deploy-layout: 2` can run on this script's variables; anything older (or no
# receipt at all) gets no automatic rollback and a manual recipe instead.
PREV_COMMIT=""; PREV_SHA=""; PREV_TAG=""; PREV_COLLAB_HASH=""; ROLLBACK_NOTE=""
PREV_RECEIPT_TAG=""; VERIFIED_PREV_COMMIT=""
if [ -n "$PREVIOUS_IMAGE" ]; then
  # Protected rollback uses image IDs as Config.Image. Recover its logical tag
  # from the actual image buildstamp, cross-checked against the existing receipt.
  PREVIOUS_IDENTITY="$("$PY" "$STATE_HELPER" verified-previous "$STATE_DIR" "$PREVIOUS_IMAGE")" || exit 1
  read -r PREV_RECEIPT_TAG VERIFIED_PREV_COMMIT <<< "$PREVIOUS_IDENTITY"
fi
if [ -z "$PREVIOUS_IMAGE" ]; then
  ROLLBACK_NOTE="nothing was running (first deploy)"
elif ! [[ "$PREV_RECEIPT_TAG" =~ ^[0-9a-f]{12}$ ]] || [ ! -r "$STATE_DIR/$PREV_RECEIPT_TAG.json" ]; then
  ROLLBACK_NOTE="no deploy receipt for the running image $PREVIOUS_IMAGE"
else
  receipt_field() {
    "$PY" -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2]); print(v if isinstance(v,str) else "")' \
      "$STATE_DIR/$PREV_RECEIPT_TAG.json" "$1" 2>/dev/null || true
  }
  CANDIDATE="$(receipt_field commit)"
  if [ "$CANDIDATE" != "$VERIFIED_PREV_COMMIT" ]; then
    echo "previous deployment receipt changed after image verification; refusing deployment" >&2
    exit 1
  fi
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

# Apply the ancestry guard to every verified previous commit, even old layouts
# lacking automatic rollback. Missing history cannot prove a safe forward roll.
# This guards only forward entry; rollback() always uses its protected snapshot.
if [ -n "$VERIFIED_PREV_COMMIT" ] && [ "$ALLOW_DIVERGENT" = 0 ] \
    && ! git -C "$REPO" merge-base --is-ancestor "$VERIFIED_PREV_COMMIT" "$FULL"; then
  echo "candidate $SHA does not descend from verified deployed commit $VERIFIED_PREV_COMMIT; refusing stale/divergent rollout before build or host changes. Use --allow-divergent only for an intentional manual rollback." >&2
  exit 1
fi

if [ -z "$HEALTH_URL" ]; then
  # The UI is published on HERDR_CP_WEB_BIND:HERDR_CP_WEB_PUBLIC_PORT.
  BIND="$(grep -E '^HERDR_CP_WEB_BIND=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  PORT="$(grep -E '^HERDR_CP_WEB_PUBLIC_PORT=' "$ENV_FILE" | cut -d= -f2- | tr -d "'\"")"
  case "$BIND" in *:*) BIND="[$BIND]" ;; esac   # IPv6 literal
  HEALTH_URL="http://${BIND:-127.0.0.1}:${PORT:-80}/api/v1/health?probe=1"
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
  # collab-public runs as the same uid: it writes the outbox and reads the mirror (both created below as the deploying uid)
  if [ "$(id -u)" != "$WANT_UID" ]; then
    echo "WARNING: collab-public runs as uid $WANT_UID (control_plane.compose.HERDR_COLLAB_CERT_UID) but this deploy" \
         "creates $HERDR_COLLAB_MIRROR_DIR and $HERDR_COLLAB_OUTBOX_DIR as uid $(id -u); it will not be able to write the outbox." >&2
  fi
fi

deploy_lock_phase build

# A failed build exits here: no running service or installed profile changed.
memory_client_build || exit 1
docker build -q -f "$WORK/deploy/fabric-stack/Dockerfile" --build-arg "GIT_COMMIT=$SHA" \
  "${MEMORY_CLIENT_BUILD_ARGS[@]}" -t "herdr-engineering:$TAG" "$WORK" >/dev/null
# The host's image comes from the same commit (only the standalone herdr_artifact_host package goes into it).
if [ "$ARTIFACT_HOST" = 1 ]; then
  docker build -q -f "$WORK/deploy/artifact-host/Dockerfile" --build-arg "GIT_COMMIT=$SHA" \
    -t "herdr-artifact-host:$TAG" "$WORK" >/dev/null
fi

# Use this checkout's helper, not the target commit's possibly older deploy tooling.
# The selected canonical source stays the forward mount. Snapshots live outside
# ENV_DIR so cleanup, logout and a failed rollout cannot destroy recovery inputs.
ATTEMPT="$("$PY" "$STATE_HELPER" prepare "$STATE_DIR" "$CENTRAL_REAL" "$FULL" \
  "$PREV_COMMIT" "$PREVIOUS_IMAGE" "$COLLAB")" || exit 1
if [ "$CENTRAL_HASH" != "$(sha256sum "$ATTEMPT/candidate/fleet-config.yaml" | cut -d' ' -f1)" ]; then
  "$PY" "$STATE_HELPER" outcome "$ATTEMPT" config_changed
  echo "canonical config changed during preparation; refusing to recreate services" >&2
  exit 1
fi
ROLLBACK_IMAGES=""
ABSENT="$("$PY" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["previous"].get("absent", [])))' \
  "$ATTEMPT/metadata.json" 2>/dev/null || true)"
if [ -n "$ABSENT" ]; then
  echo "missing service containers (will be created by this deploy): $ABSENT"
fi

# Prewarm with the TARGET image while the previous release still serves. Only the
# protected config snapshot is mounted: compose run would create the web service's
# missing host bind directories before prewarm succeeded. Share the live web's
# network namespace so its database names/routes work without starting any service.
# A first deploy has no live database/network; legacy targets lack this CLI.
if [ -n "$PREVIOUS_IMAGE" ] && grep -q '^def _cmd_cp_checkpoint(' "$WORK/herdr_engineering/cli.py"; then
  CANDIDATE_IMAGE_ID="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["candidate"]["image_id"])' "$ATTEMPT/metadata.json")"
  PREWARM_LOG="$ATTEMPT/checkpoint-prewarm.log"
  (umask 077; : > "$PREWARM_LOG")
  chmod 600 "$PREWARM_LOG"
  deploy_lock_phase checkpoint-prewarm
  echo "prewarming target projection checkpoint before host or service changes"
  if [ "$PAUSE_WORKER" = 1 ]; then
    if [ "$(docker inspect -f '{{.State.Running}}' herdr-cp-worker 2>/dev/null || true)" = true ]; then
      echo "pausing herdr-cp-worker for the prewarm (restarted right after, even on failure)"
      WORKER_PAUSED=1
      if ! docker stop -t 60 herdr-cp-worker >/dev/null 2>&1; then
        echo "could not stop herdr-cp-worker; no host files or services changed" >&2
        resume_worker
        exit 1
      fi
    else
      echo "herdr-cp-worker is not running; nothing to pause"
    fi
  fi
  if ! docker run --rm --network container:herdr-cp-web \
      --mount "type=bind,src=$ATTEMPT/candidate,dst=/etc/herdr/fleet,readonly" \
      --entrypoint herdr-eng "$CANDIDATE_IMAGE_ID" \
      control-plane checkpoint --fleet-config "$MOUNT_TARGET" \
      --migrations /app/control-plane/migrations \
      --lock-wait "$CHECKPOINT_LOCK_WAIT" >"$PREWARM_LOG" 2>&1; then
    resume_worker
    "$PY" "$STATE_HELPER" outcome "$ATTEMPT" prewarm_failed
    echo "target projection checkpoint prewarm failed; no host files or services changed. Reason and output saved in private log: $PREWARM_LOG" >&2
    exit 1
  fi
  resume_worker
  echo "projection checkpoint prewarm succeeded; metrics saved in private log: $PREWARM_LOG"
else
  echo "projection checkpoint prewarm skipped (first deploy or target predates checkpoint CLI)"
fi

# Keep the timer-driven collab-sync out of this deploy: take the SAME lock its once()
# takes and hold it (released by the kernel on any exit) across every host and
# container change below, rollback included.
if [ "$COLLAB" = 1 ]; then
  COLLAB_LOCK="${HERDR_COLLAB_SYNC_DIR}.lock"
  mkdir -p "$(dirname "$COLLAB_LOCK")"
  exec {COLLAB_LOCK_FD}>>"$COLLAB_LOCK"
  if ! flock -w "$COLLAB_LOCK_WAIT" "$COLLAB_LOCK_FD"; then
    "$PY" "$STATE_HELPER" outcome "$ATTEMPT" collab_sync_busy
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
if [ ! -e "$APPS_DIR" ]; then
  mkdir -p "$(dirname "$APPS_DIR")" && mkdir -m 700 "$APPS_DIR"
fi
check_apps_dir || exit 1
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
if [ "$ARTIFACT_HOST" = 1 ]; then for f in "${AH_FILES[@]}"; do backup_file "$AH_DIR/$f"; done; fi

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

memory_client_snapshot || exit 1
web_egress_snapshot || exit 1

# compose_run <worktree> <env file> <image tag> <commit> <compose args...>: that
# worktree's compose files with the given env file and this deploy's paths. Rollback
# passes the previous commit's worktree and an env file rendered by ITS code.
compose_run() {
  local tree="$1" envf="$2" tag="$3" commit="$4"; shift 4
  local files=(-f "$tree/deploy/control-plane/compose.yaml")
  if [ "$COLLAB" = 1 ]; then files+=(-f "$tree/deploy/control-plane/compose.collab.yaml"); fi
  if [ "$FABRIC" = 1 ] && [ -f "$tree/deploy/control-plane/compose.collab-fabric.yaml" ]; then
    files+=(-f "$tree/deploy/control-plane/compose.collab-fabric.yaml")
  fi
  if [ "$ARTIFACT_HOST" = 1 ] && [ -f "$tree/deploy/control-plane/compose.artifact-host.yaml" ]; then
    files+=(-f "$tree/deploy/control-plane/compose.artifact-host.yaml")
  fi
  if [ "$WEB_EGRESS_COMPOSE_ENABLED" = 1 ]; then files+=(-f "$WEB_EGRESS_OVERLAY"); fi
  if [ -n "$ROLLBACK_IMAGES" ]; then files+=(-f "$ROLLBACK_IMAGES"); fi
  # TRANSITION: a rollback to the release BEFORE the apps mount builds that release's compose.yaml, which still
  # requires HERDR_CP_GOOGLE_DIR (mounted at /etc/herdr/google:ro). Docker would create a missing bind source as
  # root, so point it at $APPS_DIR (exists, read-only, harmless). Remove after the next release.
  HERDR_CP_WEB_EGRESS_CIDR="$MEMORY_PRIVATE_EGRESS_CIDR" HERDR_CP_WEB_EGRESS_GENERATION="$WEB_EGRESS_GENERATION" \
  HERDR_CP_FLEET_DIR="$FLEET_DIR" HERDR_LLM_SLICE_DIR="$LLM_DIR" \
    HERDR_CP_GITHUB_APP_DIR="$GITHUB_APP_DIR" HERDR_CP_APPS_DIR="$APPS_DIR" HERDR_CP_GOOGLE_DIR="$APPS_DIR" HERDR_CP_PIPELINES_DIR="$PIPELINES_DIR" HERDR_CP_IMAGE_TAG="$tag" HERDR_CP_GIT_COMMIT="$commit" \
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
    if printf '%s' "$body" | "$PY" -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('status')=='ok' and str(d.get('commit','')).startswith(sys.argv[1]) else 1)" "$want" 2>/dev/null; then
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
    collab-edge) echo herdr-collab-edge ;; fabric-whois) echo herdr-collab-fabric-whois ;; share-public) echo herdr-share-public ;; *) echo "$1" ;;
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

# install_artifact_host_files: the rendered directory into place. Compose ignores secret uid/gid/mode, so the container
# users read the host files with their own mode: the secrets are 0644 inside a 0700 directory (that is what keeps other
# host users out), and roles.sql, which only this script reads, is 0600.
install_artifact_host_files() {
  mkdir -p "$AH_DIR" && chmod 700 "$AH_DIR" || return 1
  local f mode
  for f in "${AH_FILES[@]}"; do
    mode=0644; [ "$f" != roles.sql ] || mode=0600
    install -m "$mode" "$NEW_AH/$f" "$AH_DIR/$f.install.tmp" && mv -f "$AH_DIR/$f.install.tmp" "$AH_DIR/$f" || return 1
  done
}

# apply_ah_roles <worktree> <tag>: roles.sql as the bootstrap admin over the database container's local socket (idempotent;
# creates the three non-superuser roles and their grants, and moves the passwords with the fleet db_password).
apply_ah_roles() {
  ah_run "$1" "$2" exec -T artifact-host-db psql -U artifact_admin -d artifact_host -X -q -v ON_ERROR_STOP=1 -f - \
    < "$AH_DIR/roles.sql" >/dev/null
}

# backup_ah_database: a custom-format pg_dump kept next to the config (0700 dir, 0600 file), the newest five. Taken AFTER the
# roles exist and BEFORE migrate, so a failed or regretted migration has something to go back to.
backup_ah_database() {
  local dir="$AH_DIR/backups" f
  mkdir -p "$dir" && chmod 700 "$dir" || return 1
  AH_BACKUP_FILE="$dir/pre-$TAG-$(date -u +%Y%m%dT%H%M%SZ).dump"
  ( umask 077; ah_compose exec -T artifact-host-db pg_dump -U artifact_admin -d artifact_host -Fc > "$AH_BACKUP_FILE" ) || {
    rm -f "$AH_BACKUP_FILE"; echo "the database backup before the migration failed; nothing was migrated" >&2; return 1; }
  # shellcheck disable=SC2012
  ls -1t "$dir"/pre-*.dump 2>/dev/null | tail -n +6 | while read -r old; do rm -f "$old"; done
  return 0
}

# ah_schema_count: how many migrations the host database has applied ("" before the first migration has created the table).
ah_schema_count() {
  ah_compose exec -T artifact-host-db psql -U artifact_admin -d artifact_host -X -A -t \
    -c "SELECT count(*) FROM schema_migrations" 2>/dev/null | tr -d '[:space:]' || true
}

# artifact_host_up: database first (never force-recreated: its data directory is not touched by a routine deploy), then the
# roles, a backup, the schema (as the owner role), and last the three services recreated (they read their slice at startup)
# and waited on until healthy. Runs BEFORE the control plane's compose up, which attaches control-plane-web and the edge to
# the networks created here.
artifact_host_up() {
  local before after
  ah_compose up -d --no-build --wait --wait-timeout 120 artifact-host-db >/dev/null || return 1
  apply_ah_roles "$WORK" "$TAG" || return 1
  before="$(ah_schema_count)"
  backup_ah_database || return 1
  # AH_MIGRATED means the SCHEMA may differ from what the previous release's code expects. Roles alone are not that
  # (a rollback re-applies the previous roles.sql), so it is set when the migration failed (it may have applied some of
  # its steps) or when the applied-migrations count changed, and not on a deploy that migrated nothing.
  ah_compose run --rm artifact-host-migrate >/dev/null || { AH_MIGRATED=1; return 1; }
  after="$(ah_schema_count)"
  # an unreadable count after a successful migrate is "moved", never "unchanged": empty == empty must not read as safe
  case "$after" in ''|*[!0-9]*) AH_MIGRATED=1 ;; esac
  [ "$before" = "$after" ] || AH_MIGRATED=1
  ah_compose up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 \
    artifact-host-public artifact-host-internal artifact-host-relay >/dev/null || return 1
}

stop_artifact_host() {   # never fatal: the host is disabled, so a project a previous deploy started has no job (data stays)
  local ids
  ids="$(docker ps -q --filter "label=com.docker.compose.project=$AH_PROJECT" 2>/dev/null || true)"
  [ -z "$ids" ] || docker stop $ids >/dev/null 2>&1 \
    || echo "WARNING: could not stop the artifact host project $AH_PROJECT (control_plane.artifact_host.enabled is false)" >&2
}

stop_fabric_whois() {   # never fatal: the listener is not configured, so the sidecar a previous deploy started has no job
  if docker inspect herdr-collab-fabric-whois >/dev/null 2>&1; then
    docker stop herdr-collab-fabric-whois >/dev/null 2>&1 \
      || echo "WARNING: could not stop herdr-collab-fabric-whois (fabric_trust.listen_* is not configured)" >&2
  fi
}

forward() {
  deploy_lock_phase forward || return 1
  HOST_MUTATED=1
  web_egress_before forward || return 1
  install_private "$NEW_LLM" "$LLM_DIR/llm-config.json" || return 1
  # mount sources of web/worker (and share-public): created here, after the build, never before
  mkdir -p "$HERDR_SHARES_DIR" "$HERDR_SHARE_EVENTS_DIR" && chmod 755 "$HERDR_SHARES_DIR" "$HERDR_SHARE_EVENTS_DIR" \
    || return 1
  # collaborator bridge directories: ALWAYS (the worker mounts them whether or not the bridge is enabled). Mirror and
  # outbox are the deploying uid's, 0755 (the root worker writes them regardless); the spool is worker-private, 0700.
  mkdir -p "$HERDR_COLLAB_MIRROR_DIR" "$HERDR_COLLAB_OUTBOX_DIR" "$HERDR_COLLAB_SPOOL_DIR" \
    && chmod 755 "$HERDR_COLLAB_MIRROR_DIR" "$HERDR_COLLAB_OUTBOX_DIR" && chmod 700 "$HERDR_COLLAB_SPOOL_DIR" \
    || return 1
  if [ "$SHARE" = 1 ]; then install_private "$NEW_SHARE" "$SHARE_CONFIG" || return 1; fi
  if [ "$ARTIFACT_HOST" = 1 ]; then install_artifact_host_files || return 1; fi
  # the deploy/pipelines refresh timer; its first run is the timer's (a fetch never fails a deploy)
  if [ -f "$PIPELINES_SYNC" ]; then
    "$PIPELINES_SYNC" install --dir "$PIPELINES_DIR" || return 1
  fi
  if [ "$COLLAB" = 1 ]; then
    install_private "$NEW_COLLAB" "$COLLAB_CONFIG" || return 1
    mkdir -p "$HERDR_COLLAB_DATA_DIR" && chmod 700 "$HERDR_COLLAB_DATA_DIR" || return 1
    if [ "$FABRIC" = 1 ]; then   # the sidecar's socket directory: created before either container mounts it
      mkdir -p "$HERDR_COLLAB_FABRIC_WHOIS_DIR" && chmod 700 "$HERDR_COLLAB_FABRIC_WHOIS_DIR" || return 1
    fi
    # keys + membership files, the edge certificate, the 1-minute timer and the cert renew
    # timer (pool, group and the invite provisioner's operator come from the slice; the
    # provisioner reads collab-public's log in the data dir it mounts). Runs BEFORE the new nginx.conf lands, so
    # a reload it triggers (changed certificate) still uses the running conf.
    # This deploy holds the sync lock, so the refresh it runs must be told so (an older
    # candidate's collab-sync.sh lacks the flag: its refresh skips, the timer catches up).
    local held=()
    if grep -q -- '--lock-held' "$WORK/deploy/control-plane/collab-sync.sh"; then held=(--lock-held)
    else echo "WARNING: this commit's collab-sync.sh cannot run under the deploy's lock; its refresh is left to the timer" >&2; fi
    # Shares enabled: the edge serves the share host with this same certificate, so refuse one that does not
    # name it (checked by collab-sync before it copies, and once more on the staged copy).
    local share_host=()
    if [ "$ARTIFACT_HOST" = 1 ]; then   # the artifact host's name is served with this same certificate: the same check
      share_host=(--share-host "$AH_PUBLIC_HOST")
    elif [ "$SHARE" = 1 ]; then
      SHARE_HOST_NAME="$("$PY" -c 'import json,sys; from urllib.parse import urlsplit; print(urlsplit(json.load(open(sys.argv[1]))["sections"]["control_plane"]["values"]["share"]["public_url"]).hostname or "")' "$SHARE_CONFIG")" \
        && [ -n "$SHARE_HOST_NAME" ] || { echo "cannot read the share host from $SHARE_CONFIG" >&2; return 1; }
      share_host=(--share-host "$SHARE_HOST_NAME")
    fi
    "$WORK/deploy/control-plane/collab-sync.sh" install --config "$COLLAB_CONFIG" \
      --dir "$HERDR_COLLAB_SYNC_DIR" --data-dir "$HERDR_COLLAB_DATA_DIR" --cert-dir "$CERT_DIR_VALUE" \
      "${share_host[@]}" "${held[@]}" || return 1
    if [ "$SHARE" = 1 ] || [ "$ARTIFACT_HOST" = 1 ]; then
      "$WORK/deploy/control-plane/collab-sync.sh" cert-host --config "$COLLAB_CONFIG" --cert-dir "$CERT_DIR_VALUE" \
        "${share_host[@]}" || return 1
    fi
    mkdir -p "$(dirname "$HERDR_COLLAB_EDGE_CONF")" || return 1
    write_edge_conf "$NEW_EDGE" || return 1
  fi
  if [ "$ARTIFACT_HOST" = 1 ]; then artifact_host_up || return 1; fi
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
    compose_up_retry "$WORK" "$ENV_FILE" "$TAG" "$SHA" up -d --no-deps --no-build "${APP_SERVICES[@]}" collab-edge "${FABRIC_SERVICE[@]}" >/dev/null || return 1
  else
    compose_up "$WORK" "$ENV_FILE" "$TAG" "$SHA" >/dev/null || return 1
  fi
  memory_client_apply forward "$WORK" || return 1
  web_egress_after forward || return 1
  memory_client_forward_connectivity "$WORK" || return 1
  # The edge resolves collab-public once, at (re)load: a recreated collab-public has a
  # new address, so reload the edge after every collab up, not only on a new conf.
  if [ "$COLLAB" = 1 ]; then EDGE_CONF_CHANGED=1; reload_edge_if_changed || return 1; fi
  # Shares disabled: the edge no longer serves the share host; stop a share-public a previous deploy started (its
  # bundles stay on disk). Never fatal.
  if [ "$SHARE" = 0 ] && [ -n "$NEW_SHARE" ]; then stop_share_public; fi
  if [ "$ARTIFACT_HOST" = 0 ] && [ -n "$NEW_AH" ]; then stop_artifact_host; fi
  if [ "$COLLAB" = 1 ] && [ "$FABRIC" = 0 ]; then stop_fabric_whois; fi
  deploy_lock_phase verify || return 1
  "$PY" "$STATE_HELPER" verify-forward "$ATTEMPT" || return 1
  verify "$SHA" || return 1
  memory_client_promote
}

# rollback_services: the service list for the checkout being rolled back to. A checkout without the fabric overlay
# cannot name fabric-whois (compose fails "no such service"), so drop it for the whole rollback and stop the sidecar
# the failed forward deploy started (it is an orphan there: nothing of that checkout talks to it).
rollback_services() {
  local svc kept=()
  FABRIC_SERVICE_ORPHAN=0
  [ "$FABRIC" = 1 ] || return 0
  [ ! -f "$PREV_WORK/deploy/control-plane/compose.collab-fabric.yaml" ] || return 0
  for svc in "${EDGE_SERVICE[@]}"; do
    [ "$svc" != fabric-whois ] || continue
    kept+=("$svc")
  done
  EDGE_SERVICE=("${kept[@]}")
  FABRIC_SERVICE=()
  FABRIC_SERVICE_ORPHAN=1
}

rollback_share() {
  local conf="${HERDR_COLLAB_EDGE_CONF:-}"
  SHARE_SERVICE=()
  if [ "$COLLAB" = 1 ] && [ -f "$conf" ] && grep -Eq 'location (\^~ )?/s/ ' "$conf" \
      && grep -q '^  share-public:' "$PREV_WORK/deploy/control-plane/compose.collab.yaml" 2>/dev/null; then
    SHARE_SERVICE=(share-public)
    compose_up_retry "$PREV_WORK" "$PREV_ENV" "$PREV_TAG" "$PREV_SHA" \
      up -d --no-deps --no-build --force-recreate share-public >/dev/null
  else
    stop_share_public
  fi
}

# rollback_artifact_host: the host project as the PREVIOUS deploy left it (PREV_AH), before the control plane goes back (its
# overlay joins the host's networks, so they must exist, and must not be required when the previous release had no host).
#  - the previous release had no host: the project this deploy started is stopped (data stays) and the overlay is dropped;
#  - it had one and this deploy never touched its database: roles re-applied from the restored roles.sql and the previous
#    release's own compose file and image bring it back (a failed DISABLE lands here too: nothing was migrated);
#  - it had one and this deploy ran roles or migrations: the previous host code may not run against the changed database,
#    so nothing is guessed. The host stays as it is, the control plane still goes back, and the exact steps to finish are
#    printed (AH_MANUAL makes the whole rollback report failure).
rollback_artifact_host() {
  ARTIFACT_HOST="$PREV_AH"
  if [ "$PREV_AH" = 0 ]; then
    [ -z "$NEW_AH" ] || stop_artifact_host
    return 0
  fi
  if [ ! -f "$PREV_WORK/deploy/artifact-host/compose.yaml" ] || [ ! -f "$PREV_WORK/deploy/control-plane/compose.artifact-host.yaml" ]; then
    ARTIFACT_HOST=0; AH_MANUAL=1
    echo "the artifact host was running before this deploy, but commit $PREV_SHA has no artifact host files to restore it from" >&2
    return 0
  fi
  if [ "$AH_MIGRATED" = 1 ]; then
    AH_MANUAL=1
    {
      echo "ARTIFACT HOST NOT REVERTED: this deploy changed its database (roles and/or migrations), and the previous release's host code"
      echo "may not run against it, so the host project $AH_PROJECT stays on the new release $SHA. To finish the rollback by hand:"
      # $WORK is deleted when this script exits: print the checkout's own compose file, and every variable it requires.
      local AH_ENV="HERDR_ARTIFACT_HOST_TAG=$TAG HERDR_COLLAB_SYNC_DIR=$HERDR_COLLAB_SYNC_DIR HERDR_ARTIFACT_HOST_DIR=$AH_DIR"
      local AH_COMPOSE="docker compose -p $AH_PROJECT -f $REPO/deploy/artifact-host/compose.yaml"
      echo "  (run these from a checkout of $SHA: $REPO/deploy/artifact-host/compose.yaml must be that release's file)"
      echo "  1. stop the roles:   $AH_ENV $AH_COMPOSE stop artifact-host-public artifact-host-internal artifact-host-relay"
      if [ -e "${AH_BACKUP_FILE:-}" ]; then
        echo "  2. restore the pre-migration dump ($AH_BACKUP_FILE):"
        echo "       $AH_ENV $AH_COMPOSE exec -T artifact-host-db pg_restore -U artifact_admin -d artifact_host --clean --if-exists < $AH_BACKUP_FILE"
      else
        echo "  2. no dump was taken by this deploy (it failed before the backup): the database is as the roles step left it"
      fi
      echo "  3. redeploy the previous release: $MANUAL"
    } >&2
    return 0
  fi
  ah_run "$PREV_WORK" "$PREV_TAG" up -d --no-build --wait --wait-timeout 120 artifact-host-db >/dev/null \
    && [ -f "$AH_DIR/roles.sql" ] && apply_ah_roles "$PREV_WORK" "$PREV_TAG" \
    && ah_run "$PREV_WORK" "$PREV_TAG" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 \
      artifact-host-public artifact-host-internal artifact-host-relay >/dev/null || {
    AH_MANUAL=1; ARTIFACT_HOST=0
    echo "could not bring the artifact host back on commit $PREV_SHA (project $AH_PROJECT)" >&2
    return 0
  }
}

rollback() {
  trap '' INT TERM HUP
  DEPLOY_FINISHED=1
  deploy_lock_phase rollback || true
  if declare -F web_egress_before >/dev/null; then
    web_egress_before rollback || { echo "private egress revocation failed; preserving network and recovery state" >&2; return 3; }
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
    "$PY" "$STATE_HELPER" outcome "$ATTEMPT" failed_no_rollback || return 3
    echo "NOT rolled back: $ROLLBACK_NOTE." >&2
    if [ -n "${PREV_COMMIT_OLD:-}" ]; then
      echo "manual rollback (that commit's own deploy.sh reads ~/.config/herdr-engineering/control-plane.env):" >&2
      echo "  git -C $REPO worktree add /tmp/herdr-rollback $PREV_COMMIT_OLD && /tmp/herdr-rollback/deploy/control-plane/deploy.sh --ref $PREV_COMMIT_OLD --no-fetch" >&2
    elif [ -n "$PREVIOUS_IMAGE" ]; then
      echo "manual rollback: redeploy the commit $PREVIOUS_IMAGE was built from with that commit's deploy/control-plane/deploy.sh" >&2
    fi
    [ "$RESTORED" = 1 ] || return 3
    return 1
  fi

  echo "rolling back to $PREVIOUS_IMAGE with the compose files and code of commit $PREV_SHA" >&2
  PREV_CONFIG="$ATTEMPT/previous/fleet-config.yaml"
  FLEET_DIR="$(dirname "$PREV_CONFIG")"
  ROLLBACK_IMAGES="$ATTEMPT/rollback-images.json"
  printf -v MANUAL '%q --ref %q --no-fetch --allow-divergent --fleet-config %q' \
    "$REPO/deploy/control-plane/deploy.sh" "$PREV_COMMIT" "$PREV_CONFIG"
  PREV_WORK="$ENV_DIR/rollback-worktree"
  PREV_ENV="$ENV_DIR/rollback-compose.env"
  # the compose env as the PREVIOUS commit's own code renders it
  prev_render() {
    (cd "$PREV_WORK" && PYTHONPATH="$PREV_WORK" "$PY" -m herdr_engineering.cli \
      fleet config render-compose --path "$PREV_CONFIG" --out "$PREV_ENV") >/dev/null 2>&1
  }
  if [ "$RESTORED" = 1 ] \
      && git -C "$REPO" worktree add -q --detach "$PREV_WORK" "$PREV_COMMIT" >/dev/null 2>&1 \
      && prev_render \
      && rollback_artifact_host \
      && rollback_services \
      && compose_up "$PREV_WORK" "$PREV_ENV" "$PREV_TAG" "$PREV_SHA" >/dev/null \
      && { [ "${FABRIC_SERVICE_ORPHAN:-0}" = 0 ] || { stop_fabric_whois; true; }; } \
      && memory_client_apply rollback "$PREV_WORK" \
      && { ! declare -F web_egress_after >/dev/null || web_egress_after rollback; } \
      && rollback_share \
      && { [ "$COLLAB" = 0 ] || { EDGE_CONF_CHANGED=1; reload_edge_if_changed || true; }; } \
      && "$PY" "$STATE_HELPER" verify-rollback "$ATTEMPT" \
      && verify "$PREV_SHA" \
      && [ "$AH_MANUAL" = 0 ]; then
    if declare -F web_egress_commit >/dev/null; then web_egress_commit rollback || return 3; fi
    if declare -F web_egress_cleanup >/dev/null; then web_egress_cleanup rollback || echo "owned egress network retained; inspect saved receipt" >&2; fi
    "$PY" "$STATE_HELPER" outcome "$ATTEMPT" rolled_back || return 3
    echo "rolled back to $PREVIOUS_IMAGE (commit $PREV_SHA) and healthy (all of: ${APP_SERVICES[*]}${EDGE_SERVICE[*]:+ ${EDGE_SERVICE[*]}}); the deploy of $SHA FAILED" >&2
    return 1
  fi
  "$PY" "$STATE_HELPER" outcome "$ATTEMPT" rollback_failed || return 3
  if [ "$AH_MANUAL" = 1 ]; then
    echo "NOT FULLY ROLLED BACK to $PREVIOUS_IMAGE (commit $PREV_SHA): the artifact host needs the manual steps above" >&2
  fi
  echo "FAILED TO ROLL BACK to $PREVIOUS_IMAGE (commit $PREV_SHA): the control plane may be down or mixed." >&2
  echo "manual rollback: $MANUAL" >&2
  echo "pinned rollback images and recovery metadata: $ATTEMPT" >&2
  return 3
}

if forward; then
  # The receipt names the ref, commits and images (and only a hash of the collab
  # slice) - never configuration. json.dumps: a ref may contain quotes.
  "$PY" "$STATE_HELPER" receipt "$ATTEMPT" "$STATE_DIR/$SHA.json" "$REF" \
    "herdr-engineering:$TAG" "$PREVIOUS_IMAGE" "$COLLAB_HASH" "$(date -u +%FT%TZ)"
  web_egress_commit forward || exit 1
  "$PY" "$STATE_HELPER" outcome "$ATTEMPT" succeeded
  if [ -n "$NEW_AH" ] && [ "$DRY" != 1 ]; then   # what a later rollback must restore (see PREV_AH)
    mkdir -p "$(dirname "$AH_STATE")" && { if [ "$ARTIFACT_HOST" = 1 ]; then echo enabled; else echo disabled; fi; } > "$AH_STATE" \
      || echo "WARNING: could not record the artifact host state in $AH_STATE" >&2
  fi
  DEPLOY_FINISHED=1
  web_egress_cleanup forward || echo "owned egress network retained; inspect saved receipt" >&2
  echo "ok: ${APP_SERVICES[*]}${EDGE_SERVICE[*]:+ ${EDGE_SERVICE[*]}}${SHARE_SERVICE[*]:+ ${SHARE_SERVICE[*]}} running $SHA (receipt $STATE_DIR/$SHA.json)"
  PROBE_HOST=""
  if [ "$SHARE" = 1 ] && [ -n "${SHARE_HOST_NAME:-}" ]; then PROBE_HOST="$SHARE_HOST_NAME"
  elif [ "$ARTIFACT_HOST" = 1 ]; then PROBE_HOST="$AH_PUBLIC_HOST"; fi
  if [ -n "$PROBE_HOST" ]; then
    # Smoke line, never fatal: an unknown token on the share (or artifact) host's own TLS name answers a bare 404 when the
    # DNS record, the SNI route, the certificate and the server behind it all line up.
    probe="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "https://$PROBE_HOST/s/probe/" 2>/dev/null || true)"
    [ "$probe" = 404 ] || echo "WARNING: probe of https://$PROBE_HOST/s/probe/ answered '${probe:-no response}', expected 404:" \
      "check DNS, the SNI route to the edge and the certificate for the host" >&2
  fi
  if [ "$MEMORY" = 1 ]; then memory_phase || exit 4; fi
  exit 0
fi

rollback_status=0
rollback || rollback_status=$?
exit "$rollback_status"
