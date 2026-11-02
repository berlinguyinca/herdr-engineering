#!/usr/bin/env bash
# Roll the host-side agents to every machine in the fleet, from a git ref:
#   * the snapshot/inventory plugin  (deploy/herdr-cp-feed-plugin)  -> herdr-cp-sessions
#   * the service-ownership feed     (python package)               -> herdr-service-metadata
#
#   deploy/deploy-fleet-agents.sh [--ref origin/main] [--hosts bender,beast] [--dry-run] [--no-fetch]
#                                 [--with-terminal-relay | --only-terminal-relay]
#
# Each host gets a new release directory named for the commit; the `-current`
# symlink is switched and the service restarted. If the service is not active a few
# seconds later the previous release is put back. Hosts come from
# ~/.config/herdr-engineering/fleet-hosts.txt (see fleet-hosts.example.txt).
#
# --with-terminal-relay (off by default, so a normal roll never installs it) also rolls the Shepherd terminal relay
# (deploy/herdr-terminal-relay) per host: a release venv on Python >= 3.12 (uv, python3.12 or
# /opt/homebrew/bin/python3.12; plain python3 over ssh may be 3.9), the package from this checkout, a `current` symlink,
# and the systemd user unit or launchd plist pointing at <current>/venv/bin/herdr-eng. The roll only counts when the
# relay writes its status file (first successful wait, pid alive, newer than the restart) within 20 s; otherwise `current` goes back to the previous
# release, which is restarted, and the run exits non-zero. --only-terminal-relay rolls just the relay (no hooks,
# plugin or feed) and implies the flag. Linger off, herdr not running as that user or a missing
# herdr socket only warn.
#
# Copies no env and no secrets. A host's configuration is its own
# ~/.config/herdr-engineering/host.json (installed by `herdr-eng fleet join <host>`);
# a host without it is skipped with a hint, and the run exits non-zero.
set -euo pipefail

REF="origin/main"; ONLY=""; DRY=0; FETCH=1; RELAY=0; ONLY_RELAY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?}"; shift 2 ;;
    --hosts) ONLY="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    --with-terminal-relay) RELAY=1; shift ;;
    --only-terminal-relay) RELAY=1; ONLY_RELAY=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
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
PLUGIN_FILES="herdr-plugin.toml package.json emit.js enrich.js git-metadata.js host-config.js inventory.js session-model.js session-transcript.js session-helpers.js report-worker.js snapshot.js"

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

# This function runs on each owning host; native installers merge their own config.
# Keep installer output private because config parse errors may contain credentials.
install_native_hooks() {
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  export PATH
  python3 - <<'HERDR_NATIVE_HOOKS'
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

home = Path.home()
herdr = shutil.which("herdr")
configs = (
    ("codex", Path(os.environ.get("CODEX_HOME") or home / ".codex"),
     ("config.toml", "hooks.json", "herdr-agent-state.sh")),
    ("claude", Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
     ("settings.json", "hooks/herdr-agent-state.sh")),
    ("pi", Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".pi/agent"),
     ("extensions/herdr-agent-state.ts",)),
)
failed = False
for agent, config, files in configs:
    if not shutil.which(agent):
        print(f"   {agent} hooks: runtime not installed, skipped", flush=True)
        continue
    required = config / "extensions" if agent == "pi" else config
    if not herdr or not required.is_dir():
        print(f"   {agent} hooks: FAILED (herdr or runtime configuration missing)", flush=True)
        failed = True
        continue
    backups = home / ".local/state/herdr-engineering/hook-backups"
    backups.mkdir(parents=True, mode=0o700, exist_ok=True)
    backups.chmod(0o700)
    backup = Path(tempfile.mkdtemp(prefix=f"{agent}-", dir=backups))
    originals = {}
    attempted = False
    try:
        for relative in files:
            source = config / relative
            if source.exists():
                dest = backup / relative
                dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                shutil.copyfile(source, dest)
                dest.chmod(0o600)
                originals[relative] = stat.S_IMODE(source.stat().st_mode)
        # Record all originals before invoking the native installer. Its success
        # means hooks are installed for the next start/resume, not live-reloaded.
        log = backup / "install.log"
        with log.open("w") as output:
            log.chmod(0o600)
            attempted = True
            result = subprocess.run([herdr, "integration", "install", agent],
                                    stdout=output, stderr=subprocess.STDOUT, timeout=60)
        if result.returncode or not all((config / f).is_file() for f in files):
            raise RuntimeError("native installer failed")
    except Exception:
        # Restore managed files only after an attempted installation. If copying
        # originals failed, no installer has run and the runtime stays untouched.
        if attempted:
            for relative in files:
                destination = config / relative
                if relative in originals:
                    shutil.copyfile(backup / relative, destination)
                    destination.chmod(originals[relative])
                elif destination.exists():
                    destination.unlink()
        print(f"   {agent} hooks: FAILED; private backup/log: {backup}", flush=True)
        failed = True
    else:
        print(f"   {agent} hooks: installed; private backup/log: {backup}", flush=True)
raise SystemExit(1 if failed else 0)
HERDR_NATIVE_HOOKS
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

# --- terminal relay host functions (run on each host over ssh, like install_native_hooks) ---
relay_path() { # the tools a non-login ssh shell does not have on PATH
  local extra="${HERDR_RELAY_PATH_EXTRA-$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin}"
  if [ -n "$extra" ]; then PATH="$extra:$PATH"; export PATH; fi
}
relay_pyok() { # relay_pyok <python>: it REALLY is Python 3.12 or newer (the name proves nothing; macOS python3 is 3.9)
  local v
  v="$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || return 1
  case "$v" in 3.[0-9]|3.[0-9][0-9]) [ "${v#3.}" -ge 12 ] ;; *) return 1 ;; esac
}
relay_system_python() { # the first system interpreter that is >= 3.12: python3.12, 3.13, ..., homebrew, plain python3
  local c p
  relay_path
  for c in python3.12 python3.13 python3.14 python3.15 python3; do
    p="$(command -v "$c" 2>/dev/null || true)"
    if [ -n "$p" ] && relay_pyok "$p"; then echo "$p"; return 0; fi
  done
  p="${HERDR_RELAY_BREW_PYTHON-/opt/homebrew/bin/python3.12}"
  if [ -x "$p" ] && relay_pyok "$p"; then echo "$p"; return 0; fi
  return 1
}
relay_nopython() {
  echo "no Python >= 3.12 on this host: install uv, python3.12 or /opt/homebrew/bin/python3.12 (plain python3 over ssh may be 3.9), then roll again" >&2
}
relay_python() { # prints "uv" or the path of a Python >= 3.12; refuses (status 3) when there is none
  relay_path
  if command -v uv >/dev/null 2>&1; then echo uv; return 0; fi
  relay_system_python && return 0
  relay_nopython
  return 3
}
relay_make_venv() { # relay_make_venv <release-dir>: <dir>/venv with the wheel in <dir> installed
  local dir="$1" how wheel
  relay_path
  how="$(relay_python)" || return $?
  rm -rf "$dir/venv"   # not live: a release already behind `current` never gets here
  if [ "$how" = uv ] && ! uv venv -q --python '>=3.12' "$dir/venv"; then
    echo "uv could not create the venv; trying the system pythons" >&2
    rm -rf "$dir/venv"
    how="$(relay_system_python)" || { relay_nopython; return 3; }
  fi
  if [ "$how" != uv ]; then "$how" -m venv "$dir/venv" || return 1; fi
  relay_pyok "$dir/venv/bin/python" || { echo "the release venv is older than Python 3.12 (Python >= 3.12 is required)" >&2; return 3; }
  wheel="$(ls "$dir"/*.whl 2>/dev/null | head -n 1)"
  [ -n "$wheel" ] || { echo "no wheel in $dir" >&2; return 1; }
  if [ "$how" = uv ]; then uv pip install -q --python "$dir/venv/bin/python" "$wheel" || return 1
  else "$dir/venv/bin/pip" install -q "$wheel" || return 1; fi
  [ -x "$dir/venv/bin/herdr-eng" ] || { echo "herdr-eng is missing from the release venv" >&2; return 1; }
}
relay_render() { # relay_render <template> <out> <current>: fill @CURRENT@ (the `current` symlink) and @HOME@ (plist log path)
  local current="$3" home="$HOME"
  case "$1" in
    *.plist) current="$(printf '%s' "$current" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
             home="$(printf '%s' "$home" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')" ;;
    *) current="$(printf '%s' "$current" | sed -e 's/%/%%/g' -e 's/"/\\"/g')" ;;   # systemd specifiers and quoting
  esac
  sedq() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
  sed -e "s|@CURRENT@|$(sedq "$current")|g" -e "s|@HOME@|$(sedq "$home")|g" "$1" > "$2"
}
relay_state_dir() { echo "${HERDR_ENGINEERING_STATE:-$HOME/.local/share/herdr-engineering/state}"; }
relay_status_ok() { # relay_status_ok <marker-seconds>: the relay wrote its status file AFTER the marker and its pid is alive
  local f pid at
  f="$(relay_state_dir)/terminal-relay-status.json"
  [ -r "$f" ] || return 1
  grep -q '"state": *"connected"' "$f" || return 1
  pid="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$f" | head -n 1)"
  at="$(sed -n 's/.*"connected_at": *\([0-9][0-9.]*\).*/\1/p' "$f" | head -n 1)"
  [ -n "$pid" ] && [ -n "$at" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  awk -v a="$at" -v b="$1" 'BEGIN { exit !(a >= b) }'
}
relay_start() { # relay_start <manager>: load (launchd) or restart (systemd) the unit
  case "$1" in
    systemd) systemctl --user restart herdr-terminal-relay.service ;;
    launchd) launchctl bootout "gui/$(id -u)/com.herdr.terminal-relay" 2>/dev/null || true
             launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.herdr.terminal-relay.plist" ;;
    *) echo "unknown service manager '$1' (systemd or launchd)" >&2; return 2 ;;
  esac
}
relay_restart() { # relay_restart <manager>
  case "$1" in
    systemd) systemctl --user restart herdr-terminal-relay.service ;;
    launchd) launchctl kickstart -k "gui/$(id -u)/com.herdr.terminal-relay" ;;
    *) echo "unknown service manager '$1' (systemd or launchd)" >&2; return 2 ;;
  esac
}
relay_host_install() { # relay_host_install <systemd|launchd> <release>
  local manager="$1" rel="$2" base="$HOME/.local/share/herdr-terminal-relay" dir prev marker waited=0 sock
  case "$manager" in systemd|launchd) ;; *) echo "   terminal relay: unknown service manager '$manager' (systemd or launchd)" >&2; return 2 ;; esac
  dir="$base/releases/$rel"
  relay_path
  prev="$(readlink "$base/current" 2>/dev/null || true)"
  if [ "$prev" = "$dir" ] && [ -x "$dir/venv/bin/herdr-eng" ]; then
    echo "   terminal relay: release $rel is already current; keeping its venv, re-rendering and verifying"
  else
    relay_make_venv "$dir" || { echo "   terminal relay: could not build the release venv" >&2; return 1; }
  fi
  # Warnings only: the other services on this host are not this relay's business.
  sock="${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}"
  pgrep -u "$(id -u)" -x herdr >/dev/null 2>&1 \
    || echo "   warning: herdr does not run as $(id -un) here; the relay serves only the herdr of its own user" >&2
  [ -S "$sock" ] || echo "   warning: herdr socket $sock does not exist (herdr not started?); the relay cannot serve panes until it does" >&2
  ln -sfn "$dir" "$base/current"
  case "$manager" in
    systemd)
      if [ "$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null)" != "Linger=yes" ]; then
        echo "   warning: linger is off for $(id -un): the relay stops at logout; run  loginctl enable-linger $(id -un)" >&2
      fi
      mkdir -p "$HOME/.config/systemd/user"
      relay_render "$dir/herdr-terminal-relay.service" "$HOME/.config/systemd/user/herdr-terminal-relay.service" "$base/current"
      systemctl --user daemon-reload
      systemctl --user enable herdr-terminal-relay.service ;;
    launchd)
      mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
      relay_render "$dir/com.herdr.terminal-relay.plist" "$HOME/Library/LaunchAgents/com.herdr.terminal-relay.plist" "$base/current" ;;
  esac
  # Health evidence is the relay's status file (written on its first successful wait): only one newer than this
  # marker, with a live pid, counts. The old file goes first so it can never satisfy the check.
  rm -f "$(relay_state_dir)/terminal-relay-status.json"
  marker="$(date +%s)"
  relay_start "$manager" || true
  while [ "$waited" -lt "${HERDR_RELAY_HEALTH_SECONDS:-20}" ]; do
    if relay_status_ok "$marker"; then echo "   terminal relay: connected (release $rel)"; return 0; fi
    sleep 1; waited=$((waited + 1))
  done
  echo "   terminal relay: no fresh status file from a live relay within ${HERDR_RELAY_HEALTH_SECONDS:-20}s, rolling back" >&2
  if [ -n "$prev" ] && [ "$prev" != "$dir" ]; then
    ln -sfn "$prev" "$base/current"; relay_restart "$manager" || true
    echo "   terminal relay: restored $prev" >&2
  elif [ -n "$prev" ]; then
    echo "   terminal relay: release $rel was already current: nothing to roll back to" >&2
  else
    ln -sfn "$dir" "$base/failed"; rm -f "$base/current"
    case "$manager" in
      systemd) systemctl --user disable --now herdr-terminal-relay.service || true ;;
      launchd) launchctl bootout "gui/$(id -u)/com.herdr.terminal-relay" 2>/dev/null || true ;;
    esac
    echo "   terminal relay: first install failed, the relay is stopped" >&2
  fi
  return 1
}
# --- end terminal relay host functions ---

# Terminal relay roll for the current host (opt-in). Every failure marks the host failed and returns 0, so the multi-host
# run goes on under `set -e`.
roll_relay() {
  local rb='$HOME/.local/share/herdr-terminal-relay' rdir tmpl relay_script
  if [ "$manager" = launchd ]; then tmpl=com.herdr.terminal-relay.plist; else tmpl=herdr-terminal-relay.service; fi
  if ! { run "$target" "mkdir -p -m 700 $rb/releases/$REL" \
         && rdir="$(run "$target" "echo $rb/releases/$REL")" \
         && put "$target" "$WHEEL" "$rdir" \
         && put "$target" "$WORK/src/deploy/herdr-terminal-relay/$tmpl" "$rdir"; }; then
    echo "   terminal relay: FAILED on $name (could not stage the release)" >&2; failed=1; return 0
  fi
  relay_script="$(declare -f relay_path relay_python relay_make_venv relay_render relay_pyok relay_system_python relay_nopython relay_state_dir relay_status_ok relay_start relay_restart relay_host_install); relay_host_install $manager $REL"
  relay_script="${relay_script//\'/\'\\\'\'}"
  if run "$target" "bash -c '$relay_script'"; then echo "   terminal relay: ok"
  else echo "   terminal relay: FAILED on $name" >&2; failed=1; fi
  return 0
}

failed=0
while IFS='|' read -r name target manager; do
  name="$(echo "$name" | xargs)"; target="$(echo "$target" | xargs)"; manager="$(echo "$manager" | xargs)"
  case "$name" in ''|\#*) continue ;; esac
  if [ -n "$ONLY" ] && ! printf ',%s,' "$ONLY" | grep -q ",$name,"; then continue; fi
  case "$manager" in
    systemd|launchd) ;;
    *) echo "== $name: unknown service manager '$manager' in the hosts file (systemd or launchd); skipping" >&2
       failed=1; continue ;;
  esac
  echo "== $name ($manager)"
  if [ "$DRY" = 1 ]; then
    if [ "$ONLY_RELAY" = 0 ]; then
      echo "   [dry-run] would check host.json, then install plugin + feed release $REL and native hooks for installed runtimes"
    fi
    if [ "$RELAY" = 1 ]; then
      echo "   [dry-run] terminal relay release $REL: venv on Python >= 3.12, package from $SHA, current symlink, $manager unit/plist" \
           "-> <current>/venv/bin/herdr-eng; needs a fresh status file from a live relay within 20 s, else roll back and exit non-zero"
    else
      echo "   [dry-run] terminal relay: not requested (--with-terminal-relay)"
    fi
    continue
  fi

  # ---- host credentials: nothing is rolled to a host that cannot fetch its config ----
  if ! run "$target" 'test -s "$HOME/.config/herdr-engineering/host.json"'; then
    echo "   host.json missing on $name: run  herdr-eng fleet join $name  first; skipping" >&2
    failed=1; continue
  fi

  if [ "$ONLY_RELAY" = 1 ]; then roll_relay; continue; fi

  # Run through bash even if the remote login shell differs. Quote the function
  # as data so paths and the embedded Python cannot be interpreted by that shell.
  hook_script="$(declare -f install_native_hooks); install_native_hooks"
  hook_script="${hook_script//\'/\'\\\'\'}"
  if ! run "$target" "bash -c '$hook_script'"; then failed=1; fi

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

  if [ "$RELAY" = 1 ]; then roll_relay; fi
done < "$HOSTS_FILE"
exit "$failed"
