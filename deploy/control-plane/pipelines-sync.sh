#!/usr/bin/env bash
# Keep the deployment definitions control-plane-web reads (control_plane.deployment_definitions
# = /etc/herdr/pipelines) equal to InferWeave's deploy/pipelines/*.yaml on origin/main.
#
#   pipelines-sync.sh once|install [--remote URL] [--clone DIR] [--dir DIR] [--branch NAME]
#                                  [--validator FILE]
#
# once     fetch BRANCH into a dedicated bare, blob-less clone (--clone; never a working
#          checkout), extract ONLY the regular *.yaml files directly under deploy/pipelines/
#          (symlinks, submodules, subdirectories and other names are skipped), validate them
#          with herdr's own definition validator (deployment_definitions.validate_dir), and only
#          then refresh --dir: changed files are renamed in one by one, then stale *.yaml
#          removed. Any failure (fetch, size or count cap, an empty set, a validation error)
#          leaves the previous good set untouched. Runs never overlap (flock on DIR.lock).
# install  copy this script and the validator to stable paths (deploy.sh runs from a temporary
#          worktree) and install the user units herdr-pipelines-sync.{service,timer}: first run
#          ~20 s after the timer starts, then every 5 minutes. It does not fetch itself.
#
# --dir is the bind source of the web container's read-only /etc/herdr/pipelines mount, so it
# is never replaced as a whole (a bind mount pins the directory inode); its files are.
# Authentication: ssh as the running user (the default remote is git@github.com:...; no token
# is used or stored). The user's ~/.gitconfig may rewrite https GitHub URLs to ssh anyway.
# Paths are flags (no environment): install writes every flag into the unit's ExecStart.
set -euo pipefail
USAGE="usage: $0 once|install [--remote URL] [--clone DIR] [--dir DIR] [--branch NAME] [--validator FILE]"
ACTION="${1:-once}"; [ $# -gt 0 ] && shift
SHARE="$HOME/.local/share/herdr-engineering"
REMOTE="git@github.com:InferWeave/inferweave.git"
CLONE="$SHARE/inferweave-pipelines.git"
DIR="$SHARE/pipelines"
BRANCH="main"
VALIDATOR="$SHARE/pipelines-validator/deployment_definitions.py"
while [ $# -gt 0 ]; do
  case "$1" in
    --remote) REMOTE="${2:?--remote needs a value}"; shift 2 ;;
    --clone) CLONE="${2:?--clone needs a value}"; shift 2 ;;
    --dir) DIR="${2:?--dir needs a value}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    --validator) VALIDATOR="${2:?--validator needs a value}"; shift 2 ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
done
DIR="${DIR%/}"
# The loader's own bounds (deployment_definitions.MAX_FILE_BYTES / MAX_FILES).
MAX_BYTES=65536
MAX_FILES=256
SRC="deploy/pipelines"
NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]*\.yaml$'
# Never prompt (a timer has no terminal), fail fast on a dead network.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"

STAGE=""
trap '[ -z "$STAGE" ] || rm -rf "$STAGE" "$STAGE.ls"' EXIT
g() { git -C "$CLONE" "$@"; }

ensure_clone() {
  if [ ! -f "$CLONE/HEAD" ]; then
    mkdir -p "$(dirname "$CLONE")"
    git init -q --bare "$CLONE"
    # partial clone: blobs come only for the files read below (deploy/pipelines)
    g config core.repositoryformatversion 1
    g config extensions.partialClone origin
    g config remote.origin.promisor true
    g config remote.origin.partialclonefilter blob:none
    g config gc.auto 0
  fi
  g config remote.origin.url "$REMOTE"   # every run: a changed --remote applies
  g config --unset-all remote.origin.fetch >/dev/null 2>&1 || true   # fetch names its refspec
}

check_dest() {   # --dir: a real directory of ours holding only regular *.yaml files
  if [ -L "$DIR" ] || { [ -e "$DIR" ] && [ ! -d "$DIR" ]; }; then
    echo "$DIR must be a real directory (not a symlink or a file)" >&2; return 1
  fi
  if [ ! -e "$DIR" ]; then mkdir -p "$(dirname "$DIR")"; mkdir -m 755 "$DIR"; fi
  if [ "$(stat -c %u "$DIR")" != "$(id -u)" ]; then
    echo "$DIR must be owned by uid $(id -u)" >&2; return 1
  fi
  chmod 755 "$DIR"
  local bad
  bad="$(find "$DIR" -mindepth 1 -maxdepth 1 \( ! -type f -o ! -name '*.yaml' \) -printf '%f ' 2>/dev/null || true)"
  if [ -n "$bad" ]; then
    echo "$DIR holds entries that are not regular *.yaml files ($bad); not touching it, remove them by hand" >&2
    return 1
  fi
}

once() {
  mkdir -p "$(dirname "$DIR")"
  exec 9>"$DIR.lock"; flock -n 9 || { echo "another pipelines-sync is running" >&2; return 0; }   # no overlapping runs
  check_dest
  [ -r "$VALIDATOR" ] || { echo "validator not found: $VALIDATOR (run deploy.sh, or pass --validator)" >&2; return 1; }
  ensure_clone
  timeout 180 git -C "$CLONE" fetch -q --no-tags --depth 1 --filter=blob:none origin \
    "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
    || { echo "fetch of $BRANCH from $REMOTE failed; keeping the previous definitions" >&2; return 1; }
  local commit; commit="$(g rev-parse --verify "refs/remotes/origin/$BRANCH^{commit}")"

  # Staging is a sibling on the same filesystem (renames into DIR are atomic per file), never
  # inside DIR, where the loader and deploy.sh's checks would see it.
  STAGE="$(mktemp -d "$(dirname "$DIR")/.pipelines-staging.XXXXXX")"   # removed on exit
  local stage="$STAGE" listing="$STAGE.ls"
  chmod 755 "$stage"
  local entry meta path mode type oid name size n=0
  timeout 60 git -C "$CLONE" ls-tree -z "$commit" -- "$SRC/" > "$listing" \
    || { echo "cannot list $SRC at ${commit:0:12}; keeping the previous definitions" >&2; return 1; }
  while IFS= read -r -d '' entry; do
    meta="${entry%%$'\t'*}"; path="${entry#*$'\t'}"
    read -r mode type oid <<< "$meta"
    name="${path#"$SRC"/}"
    # regular files only (100644/100755): a symlink (120000), submodule or subtree is skipped
    [ "$type" = blob ] || continue
    [ "$mode" = 100644 ] || [ "$mode" = 100755 ] || continue
    [[ "$name" =~ $NAME_RE ]] || continue
    n=$((n + 1))
    if [ "$n" -gt "$MAX_FILES" ]; then
      echo "more than $MAX_FILES definition files at ${commit:0:12}; keeping the previous definitions" >&2; return 1
    fi
    size="$(timeout 60 git -C "$CLONE" cat-file -s "$oid")"
    if [ "$size" -gt "$MAX_BYTES" ]; then
      echo "$name exceeds $MAX_BYTES bytes at ${commit:0:12}; keeping the previous definitions" >&2; return 1
    fi
    timeout 60 git -C "$CLONE" cat-file blob "$oid" > "$stage/$name"
    chmod 644 "$stage/$name"
  done < "$listing"
  if [ "$n" = 0 ]; then
    echo "no *.yaml under $SRC at ${commit:0:12}; keeping the previous definitions" >&2; return 1
  fi
  # herdr's own validator, the same code the web container loads with (all-or-nothing)
  if ! timeout 60 python3 -I - "$VALIDATOR" "$stage" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("herdr_pipelines_validator", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod          # dataclasses need the module registered
spec.loader.exec_module(mod)
errors = mod.validate_dir(sys.argv[2])
prefix = sys.argv[2].rstrip("/") + "/"
for e in errors:
    print("  " + e.replace(prefix, ""), file=sys.stderr)
sys.exit(1 if errors else 0)
PY
  then
    echo "deploy/pipelines at ${commit:0:12} is invalid (above); keeping the previous definitions" >&2
    return 1
  fi

  # New and changed files first (unchanged ones keep their inode, so the loader's signature
  # does not move every run), then stale ones. The loader re-reads on every change and
  # loads all-or-nothing, so the short mixed state in between converges on the next read.
  local f base changed=0 removed=0 keep
  keep=" $(cd "$stage" && printf '%s ' *.yaml)"
  for f in "$stage"/*.yaml; do
    base="${f##*/}"
    if ! cmp -s "$f" "$DIR/$base"; then mv -f "$f" "$DIR/$base"; changed=$((changed + 1)); fi
  done
  for f in "$DIR"/*.yaml; do
    [ -e "$f" ] || continue
    base="${f##*/}"
    case "$keep" in *" $base "*) ;; *) rm -f "$f"; removed=$((removed + 1)) ;; esac
  done
  printf '%s\n' "$commit" > "$DIR.commit.tmp" && mv -f "$DIR.commit.tmp" "$DIR.commit"
  echo "pipelines: ${commit:0:12} $n definitions, $changed changed, $removed removed"
}

install_timer() {
  local u="$HOME/.config/systemd/user" bin="$SHARE/pipelines-sync.sh"
  local here; here="$(cd "$(dirname "$0")" && pwd)"
  local src="$here/../../herdr_engineering/control_plane/deployment_definitions.py"
  [ -r "$src" ] || { echo "validator source not found: $src" >&2; return 1; }
  mkdir -p "$u" "$SHARE" "$(dirname "$VALIDATOR")"
  install -m 0755 "$0" "$bin"                  # stable copies: deploy.sh runs from a temporary worktree
  install -m 0644 "$src" "$VALIDATOR"
  cat > "$u/herdr-pipelines-sync.service" <<UNIT
[Unit]
Description=Refresh InferWeave deploy/pipelines definitions for the control plane
After=network-online.target
[Service]
Type=oneshot
TimeoutStartSec=10min
ExecStart="$bin" once --remote "$REMOTE" --clone "$CLONE" --dir "$DIR" --branch "$BRANCH" --validator "$VALIDATOR"
UNIT
  cat > "$u/herdr-pipelines-sync.timer" <<UNIT
[Unit]
Description=Refresh InferWeave deploy/pipelines definitions every 5 minutes
[Timer]
OnActiveSec=20s
OnUnitActiveSec=5min
AccuracySec=30s
[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now herdr-pipelines-sync.timer
}

case "$ACTION" in once) once ;; install) install_timer ;; *) echo "$USAGE" >&2; exit 2 ;; esac
