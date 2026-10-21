#!/usr/bin/env bash
# Roll the host-side agents to every machine in the fleet, from a git ref:
#   * the snapshot/inventory plugin  (deploy/herdr-cp-feed-plugin)  -> herdr-cp-sessions
#   * the service-ownership feed     (python package)               -> herdr-service-metadata
#
#   deploy/deploy-fleet-agents.sh [--ref origin/main] [--hosts bender,beast] [--dry-run] [--no-fetch]
#
# Each host gets a new release directory named for the commit; the `-current`
# symlink is switched and the service restarted. If the service is not active a few
# seconds later the previous release is put back. Hosts come from
# ~/.config/herdr-engineering/fleet-hosts.txt (see fleet-hosts.example.txt).
set -euo pipefail

REF="origin/main"; ONLY=""; DRY=0; FETCH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?}"; shift 2 ;;
    --hosts) ONLY="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
HOSTS_FILE="${HERDR_FLEET_HOSTS:-$HOME/.config/herdr-engineering/fleet-hosts.txt}"
[ -r "$HOSTS_FILE" ] || { echo "no hosts file: $HOSTS_FILE (copy deploy/fleet-hosts.example.txt)" >&2; exit 1; }
[ "$FETCH" = 1 ] && git -C "$REPO" fetch -q origin
FULL="$(git -C "$REPO" rev-parse --verify "$REF^{commit}")"; SHA="${FULL:0:12}"
REL="fleet-$SHA"
PLUGIN_FILES="herdr-plugin.toml package.json emit.js enrich.js git-metadata.js host-config.js inventory.js session-model.js snapshot.js"

WORK="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
git -C "$REPO" worktree add -q --detach "$WORK/src" "$FULL"
for f in $PLUGIN_FILES; do
  [ -f "$WORK/src/deploy/herdr-cp-feed-plugin/$f" ] || { echo "missing plugin file at $SHA: $f" >&2; exit 1; }
done
mkdir -p "$WORK/wheel"
python3 -m pip wheel -q --no-deps -w "$WORK/wheel" "$WORK/src" >/dev/null
WHEEL="$(ls "$WORK/wheel"/*.whl)"
echo "rolling $REF = $SHA to hosts in $HOSTS_FILE"

run() { # run <target> <command...>   ("local" runs here)
  local target="$1"; shift
  # -n / </dev/null: nothing here may read the hosts file the outer loop is iterating.
  if [ "$target" = local ]; then bash -c "$*" </dev/null; else ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$target" "$*"; fi
}
put() { # put <target> <src> <dest-dir>
  if [ "$1" = local ]; then cp "$2" "$3/"; else scp -q -o BatchMode=yes "$2" "$1:$3/"; fi
}

restart_cmd() { # restart_cmd <manager> <unit-or-label>
  case "$1" in
    systemd) echo "systemctl --user restart $2.service" ;;
    launchd) echo "launchctl kickstart -k gui/\$(id -u)/$2" ;;
  esac
}
active_cmd() {
  case "$1" in
    systemd) echo "sleep 8; systemctl --user is-active --quiet $2.service" ;;
    launchd) echo "sleep 8; launchctl list | grep -q $2" ;;
  esac
}

failed=0
while IFS='|' read -r name target manager; do
  name="$(echo "$name" | xargs)"; target="$(echo "$target" | xargs)"; manager="$(echo "$manager" | xargs)"
  case "$name" in ''|\#*) continue ;; esac
  if [ -n "$ONLY" ] && ! printf ',%s,' "$ONLY" | grep -q ",$name,"; then continue; fi
  echo "== $name ($manager)"
  if [ "$DRY" = 1 ]; then echo "   [dry-run] would install plugin + feed release $REL"; continue; fi

  # ---- snapshot plugin ----
  base='$HOME/.local/share/herdr-cp-feed-plugin-releases'
  prev="$(run "$target" "readlink \$HOME/.local/share/herdr-cp-feed-plugin-current || true")"
  run "$target" "mkdir -p -m 700 $base/$REL"
  for f in $PLUGIN_FILES; do
    put "$target" "$WORK/src/deploy/herdr-cp-feed-plugin/$f" "$(run "$target" "echo $base/$REL")"
  done
  run "$target" "ln -sfn $base/$REL \$HOME/.local/share/herdr-cp-feed-plugin-current"
  if [ "$manager" = launchd ]; then unit=com.herdr.cp-sessions; else unit=herdr-cp-sessions; fi
  run "$target" "$(restart_cmd "$manager" "$unit")"
  if run "$target" "$(active_cmd "$manager" "$unit")"; then
    echo "   plugin: ok"
  else
    echo "   plugin: NOT active, restoring ${prev:-previous release}" >&2; failed=1
    [ -n "$prev" ] && run "$target" "ln -sfn $prev \$HOME/.local/share/herdr-cp-feed-plugin-current; $(restart_cmd "$manager" "$unit")" || true
  fi

  # ---- service-ownership feed (only where it is installed) ----
  if [ "$manager" = systemd ] && run "$target" "test -d \$HOME/.local/share/herdr-service-metadata/releases"; then
    fb='$HOME/.local/share/herdr-service-metadata'
    fprev="$(run "$target" "readlink $fb/current || true")"
    run "$target" "mkdir -p -m 700 $fb/releases/$REL && python3 -m venv $fb/releases/$REL/venv"
    put "$target" "$WHEEL" "$(run "$target" "echo $fb/releases/$REL")"
    run "$target" "$fb/releases/$REL/venv/bin/pip install -q $fb/releases/$REL/*.whl"
    run "$target" "ln -sfn $fb/releases/$REL $fb/current; $(restart_cmd systemd herdr-service-metadata)"
    if run "$target" "$(active_cmd systemd herdr-service-metadata)"; then
      echo "   ownership feed: ok"
    else
      echo "   ownership feed: NOT active, restoring ${fprev:-previous release}" >&2; failed=1
      [ -n "$fprev" ] && run "$target" "ln -sfn $fprev $fb/current; $(restart_cmd systemd herdr-service-metadata)" || true
    fi
  else
    echo "   ownership feed: not installed here, skipped"
  fi
done < "$HOSTS_FILE"
exit "$failed"
