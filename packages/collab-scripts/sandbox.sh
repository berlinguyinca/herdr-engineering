#!/usr/bin/env bash
# collab-sandbox.sh WORKDIR COMMAND — run COMMAND (a shell string, task text on stdin) with only WORKDIR writable.
#
# A collaboration agent runs text a room member wrote. This is the box it runs in:
#   * the system (/usr, /etc bits) is read-only; there is no /home, no other agent's credential file, no ssh keys,
#     no cloud credentials, no collab log: the home directory is an empty temp dir
#   * WORKDIR is the one place it can create, change or delete files; everything else fails with
#     "Read-only file system"
#   * the harness itself (claude/codex/pi, node) and its login (~/.claude, ~/.codex, ~/.pi) are visible
#     READ-ONLY, so it can sign in but cannot rewrite its own settings or hooks. Helpers (HERDR_SANDBOX_HARNESS_DIR)
#     get none of those: only a private copy of the login file the harness needs, made by the runner
#   * own process namespace (it can't see or signal other processes), no extra privileges, no capabilities,
#     the environment is cleared, and limits on processes, open files and file size apply
# The network is left open: the harness has to reach its model. That, plus the harness login being readable
# to the harness, is what this does NOT protect: only start tool-enabled agents for people you trust.
# With HERDR_EGRESS_SOCK set (helpers) the box has NO network of its own: a private network namespace with only
# loopback, where a relay (collab-egress.py relay) on 127.0.0.1:HERDR_EGRESS_PORT (3128) hands every connection
# to the runner's egress proxy on that unix socket, which tunnels only approved host:ports and adds bound API keys
# itself. HTTPS_PROXY/HTTP_PROXY point at the relay; SVC_<NAME>_URL for each name in HERDR_EGRESS_SERVICES (comma
# list). If the relay does not come up, the command does not run.
# No bubblewrap, no run: this never falls back to running unboxed.
set -euo pipefail

verify_strict_limits() {
  local tasks="$1" memory="$2" cpu="$3" rel base actual_tasks actual_memory quota period
  rel="$(awk -F: '$1 == "0" { print $3 }' /proc/self/cgroup)"
  case "$rel" in /*) ;; *) exit 1 ;; esac
  case "/$rel/" in */../*|*/./*) exit 1 ;; esac
  base="/sys/fs/cgroup$rel"
  [ -n "$rel" ] && [ -r "$base/pids.max" ] && [ -r "$base/memory.max" ] && [ -r "$base/cpu.max" ] || exit 1
  actual_tasks="$(cat "$base/pids.max")"; actual_memory="$(cat "$base/memory.max")"
  case "$actual_tasks:$actual_memory" in *max*|*[!0-9:]*) exit 1 ;; esac
  [ "$actual_tasks" -le "$tasks" ] && [ "$actual_memory" -le "$memory" ] || exit 1
  read -r quota period < "$base/cpu.max"
  case "$quota:$period" in *max*|*[!0-9:]*) exit 1 ;; esac
  [ "$quota" -le $((cpu * period / 100)) ] || exit 1
}
if [ "${1:-}" = "--run-strict" ]; then
  tasks="${2:?tasks}"; memory="${3:?memory}"; cpu="${4:?cpu}"; wall="${5:?wall}"; timeout_bin="${6:?timeout}"
  shift 6
  verify_strict_limits "$tasks" "$memory" "$cpu"
  exec "$timeout_bin" --signal=TERM --kill-after=10s "$wall"s "$@"
fi

deny() { echo "collab-sandbox: $1; refusing to run" >&2; exit 126; }
owner_mode() { stat -c '%u %a' "$1" 2>/dev/null || stat -f '%u %Lp' "$1"; }
bounded_decimal() {
  local name="$1" value="$2" minimum="$3" maximum="$4" digits="$5"
  case "$value" in ''|0|0*|*[!0-9]*) deny "$name must be a canonical positive integer" ;; esac
  [ "${#value}" -le "$digits" ] && [ "$value" -ge "$minimum" ] && [ "$value" -le "$maximum" ] ||
    deny "$name must be $minimum-$maximum"
}
PROFILE="${HERDR_SANDBOX_PROFILE:-legacy}"
case "$PROFILE" in legacy|task-restricted-v1) ;; *) deny "unknown sandbox profile $PROFILE" ;; esac
STRICT=0
if [ "$PROFILE" = task-restricted-v1 ]; then
  STRICT=1
  [ -n "${HERDR_SANDBOX_HARNESS_DIR:-}" ] || deny "task-restricted-v1 requires a private harness directory"
  [ -n "${HERDR_SANDBOX_RUNTIME_DIR:-}" ] || deny "task-restricted-v1 requires a pinned runtime directory"
  [ -n "${HERDR_EGRESS_SOCK:-}" ] || deny "task-restricted-v1 requires broker-only network egress"
  [[ "${HERDR_SANDBOX_UNIT:-}" =~ ^herdr-[a-z0-9-]{1,48}$ ]] || deny "task-restricted-v1 requires a safe sandbox unit"
  if [ -n "${HERDR_SANDBOX_RO_BINDS+x}" ] || [ -n "${HERDR_SANDBOX_SSH_DIR+x}" ]; then
    deny "task-restricted-v1 does not permit SSH or extra host mounts"
  fi
  bounded_decimal HERDR_SANDBOX_MEM_BYTES "${HERDR_SANDBOX_MEM_BYTES:-}" 268435456 17179869184 11
  bounded_decimal HERDR_SANDBOX_TASKS "${HERDR_SANDBOX_TASKS:-}" 16 1024 4
  bounded_decimal HERDR_SANDBOX_TIMEOUT_SECONDS "${HERDR_SANDBOX_TIMEOUT_SECONDS:-}" 30 86400 5
  bounded_decimal HERDR_SANDBOX_CPU_PERCENT "${HERDR_SANDBOX_CPU_PERCENT:-}" 10 800 3
  [ -d "$HERDR_SANDBOX_HARNESS_DIR" ] && [ ! -L "$HERDR_SANDBOX_HARNESS_DIR" ] || deny "the harness directory must be a directory, not a link"
  [ "$(owner_mode "$HERDR_SANDBOX_HARNESS_DIR")" = "$(id -u) 700" ] || deny "the harness directory must be yours with mode 0700"
  [ -d "$HERDR_SANDBOX_RUNTIME_DIR" ] && [ ! -L "$HERDR_SANDBOX_RUNTIME_DIR" ] || deny "the runtime directory must be a directory, not a link"
  read -r runtime_uid runtime_mode <<< "$(owner_mode "$HERDR_SANDBOX_RUNTIME_DIR")"
  [ "$runtime_uid" = "$(id -u)" ] && (( (8#$runtime_mode & 022) == 0 )) || deny "the runtime directory must be yours and not group/other writable"
  runtime_real="$(cd "$HERDR_SANDBOX_RUNTIME_DIR" && pwd -P)"
  if find "$runtime_real" -xdev \( ! -type d ! -type f -o -type f -links +1 \) -print -quit | grep -q .; then deny "the runtime directory must contain only private regular files and directories"; fi
  [ -S "$HERDR_EGRESS_SOCK" ] && [ ! -L "$HERDR_EGRESS_SOCK" ] || deny "task-restricted-v1 requires a private broker egress socket"
  sock_parent="$(dirname "$HERDR_EGRESS_SOCK")"
  [ ! -L "$sock_parent" ] && [ "$(owner_mode "$sock_parent")" = "$(id -u) 700" ] || deny "the egress socket directory must be yours with mode 0700"
  read -r sock_uid sock_mode <<< "$(owner_mode "$HERDR_EGRESS_SOCK")"
  [ "$sock_uid" = "$(id -u)" ] && (( (8#$sock_mode & 022) == 0 )) || deny "the egress socket must be yours and not group/other writable"
  TIMEOUT_BIN="$(command -v timeout)" || deny "task-restricted-v1 requires timeout enforcement"
fi

WORK="${1:?usage: collab-sandbox.sh WORKDIR COMMAND}"
CMD="${2:?usage: collab-sandbox.sh WORKDIR COMMAND}"
AREA="$HOME/herdr-agents/work"
case "/$WORK/" in */../*) echo "collab-sandbox: workdir must not contain .." >&2; exit 126 ;; esac
case "$WORK" in "$AREA"/*) ;; *) echo "collab-sandbox: workdir must be under ~/herdr-agents/work/" >&2; exit 126 ;; esac
if [ "$STRICT" = 1 ]; then
  [ -d "$AREA" ] && [ ! -L "$AREA" ] || deny "task-restricted-v1 requires a private work root"
  [ "$(owner_mode "$AREA")" = "$(id -u) 700" ] || deny "the work root must be yours with mode 0700"
  home_real="$(cd "$HOME" && pwd -P)"; area_real="$(cd "$AREA" && pwd -P)"
  [ "$area_real" = "$home_real/herdr-agents/work" ] || deny "the work root must not traverse links"
  [ "$(dirname "$WORK")" = "$AREA" ] || deny "task-restricted-v1 workspaces must be direct children of the work root"
  if [ -e "$WORK" ] || [ -L "$WORK" ]; then
    [ -d "$WORK" ] && [ ! -L "$WORK" ] || deny "the workspace must be a directory, not a link"
  else
    mkdir -m 0700 "$WORK"
  fi
  [ "$(owner_mode "$WORK")" = "$(id -u) 700" ] || deny "the workspace must be yours with mode 0700"
else
  mkdir -p -m 0700 "$WORK"
fi
WORK="$(cd "$WORK" && pwd -P)"
# a symlink inside the work area must not lead out of it
case "$WORK" in "$(cd "$AREA" && pwd -P)"/*) ;; *) echo "collab-sandbox: workdir resolves outside ~/herdr-agents/work/" >&2; exit 126 ;; esac
if [ "$STRICT" = 1 ] && { find "$WORK" -xdev \( -type s -o -type p -o -type b -o -type c -o \( -type f -links +1 \) \) -print -quit | grep -q .; }; then
  deny "task-restricted-v1 workspace must not contain host-linked or special files"
fi

# Local name resolution only for a box with the host network: a no-network box must not reach systemd-resolved's
# socket (a DNS side channel); the egress proxy resolves names on the host side.
args=(--die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL
      --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/sbin /sbin)
if [ -z "${HERDR_EGRESS_SOCK+x}" ]; then
  args+=(--ro-bind-try /etc/resolv.conf /etc/resolv.conf --ro-bind-try /run/systemd/resolve /run/systemd/resolve)
fi
args+=(
      --ro-bind-try /etc/hosts /etc/hosts --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf
      --ro-bind-try /etc/ssl /etc/ssl --ro-bind-try /etc/ca-certificates /etc/ca-certificates
      --ro-bind-try /etc/passwd /etc/passwd --ro-bind-try /etc/group /etc/group --ro-bind-try /etc/localtime /etc/localtime
      --ro-bind-try /etc/alternatives /etc/alternatives
      --proc /proc --dev /dev --size 268435456 --tmpfs /tmp --size 67108864 --tmpfs /var/tmp
      --tmpfs "$HOME")
if [ "$STRICT" = 1 ]; then args+=(--ro-bind "$runtime_real" /opt/herdr-pi); fi

# The harness and its login, read-only. Resolve where the executables really live (symlinks into ~/.local/share,
# node under a version manager...) and expose just those directories.
seen=":"
bindro() { case "$seen" in *":$1:"*) return 0 ;; esac; seen="$seen$1:"; if [ -e "$1" ]; then args+=(--ro-bind "$1" "$1"); fi; }
for n in claude codex pi node; do
  p="$(command -v "$n" 2>/dev/null)" || continue
  r="$(readlink -f "$p")"
  if [ "$STRICT" = 1 ]; then case "$p:$r" in *"$HOME"/*) deny "task-restricted-v1 requires harness executables outside the operator home" ;; esac; fi
  case "$p" in "$HOME"/*) bindro "$(dirname "$p")" ;; esac
  # every hop of the link chain must exist in the box, not just both ends: codex's standalone install is
  # ~/.local/bin/codex -> ~/.codex/packages/standalone/current/bin/codex with `current` itself a link to
  # releases/<version>; binding .../current/bin (bwrap follows the source) makes that hop resolve
  h="$p"
  for _ in 1 2 3 4 5 6 7 8; do
    t="$(readlink "$h" 2>/dev/null)" || break
    case "$t" in /*) ;; *) t="$(dirname "$h")/$t" ;; esac
    case "$t" in "$HOME"/*) bindro "$(dirname "$t")" ;; esac
    h="$t"
  done
  case "$r" in "$HOME"/*) bindro "$(dirname "$r")" ;; esac
done
# Helpers only (set by the runner, never by a task), each fails closed when it does not check out:
#   HERDR_SANDBOX_RO_BINDS     newline-separated directories under ~/herdr-agents/work/ (a reviewer's view of its
#                              parent's work), shown READ-ONLY at the same path
#   HERDR_SANDBOX_SSH_DIR      a directory of your own with mode 0700 holding key files, shown READ-ONLY as ~/.ssh
#   HERDR_SANDBOX_HARNESS_DIR  a directory of your own with mode 0700 holding claude/ codex/ pi/ (the login file
#                              each harness needs, nothing else), shown WRITABLE as ~/.herdr-harness, with
#                              CLAUDE_CONFIG_DIR, CODEX_HOME and PI_CODING_AGENT_DIR pointing into it. The operator's
#                              ~/.claude, ~/.codex, ~/.pi and ~/.claude.json (memory, hooks, settings, plugins,
#                              projects) are then NOT in the box at all.
harness_env=()
if [ -n "${HERDR_SANDBOX_HARNESS_DIR+x}" ]; then
  HD="$HERDR_SANDBOX_HARNESS_DIR"
  case "$HD" in /*) ;; *) deny "the harness directory must be an absolute path" ;; esac
  if [ -L "$HD" ] || [ ! -d "$HD" ]; then deny "the harness directory must be a directory, not a link"; fi
  [ "$(owner_mode "$HD")" = "$(id -u) 700" ] || deny "the harness directory must be yours with mode 0700"
  hreal="$(cd "$HD" && pwd -P)"
  credential_roots=(.claude .codex .pi)
  if [ "$STRICT" = 1 ]; then credential_roots+=(.ssh .aws .config); fi
  for d in "${credential_roots[@]}"; do
    if [ -d "$HOME/$d" ]; then
      oreal="$(cd "$HOME/$d" && pwd -P)"
      case "$hreal/" in "$oreal"/*) deny "the harness directory must not be the operator's $d" ;; esac
      if [ "$STRICT" = 1 ]; then case "$oreal/" in "$hreal"/*) deny "the harness directory must not contain the operator's $d" ;; esac; fi
    fi
  done
  if [ "$STRICT" = 1 ]; then
    [ "$hreal" != "$(cd "$HOME" && pwd -P)" ] || deny "the harness directory must not be the operator home"
    if find "$hreal" -xdev \( ! -type d ! -type f -o -type f -links +1 \) -print -quit | grep -q .; then deny "the harness directory must contain only private regular files and directories"; fi
    for entry in "$hreal"/* "$hreal"/.[!.]* "$hreal"/..?*; do
      [ -e "$entry" ] || continue
      name="$(basename "$entry")"
      case "$name" in claude|codex|pi) [ -d "$entry" ] || deny "the harness directory may contain only claude, codex and pi directories" ;; *) deny "the harness directory may contain only claude, codex and pi directories" ;; esac
    done
  fi
  HB="$HOME/.herdr-harness"
  args+=(--bind "$hreal" "$HB")
  harness_env=(--setenv CLAUDE_CONFIG_DIR "$HB/claude" --setenv CODEX_HOME "$HB/codex"
               --setenv PI_CODING_AGENT_DIR "$HB/pi")
else
  for d in .claude .codex .pi; do bindro "$HOME/$d"; done
  if [ -e "$HOME/.claude.json" ]; then args+=(--ro-bind "$HOME/.claude.json" "$HOME/.claude.json"); fi
fi
if [ -n "${HERDR_SANDBOX_RO_BINDS+x}" ]; then
  while IFS= read -r ro; do
    [ -n "$ro" ] || continue
    case "$ro" in /*) ;; *) deny "a read-only bind must be an absolute path" ;; esac
    case "/$ro/" in */../*|*/./*) deny "a read-only bind must not contain . or .." ;; esac
    if [ -L "$ro" ] || [ ! -d "$ro" ]; then deny "a read-only bind must be a directory, not a link"; fi
    real="$(cd "$ro" && pwd -P)"
    case "$real" in "$(cd "$AREA" && pwd -P)"/*) ;; *) deny "a read-only bind must be under ~/herdr-agents/work/" ;; esac
    [ "$real" != "$WORK" ] || deny "a read-only bind must not be the work directory"
    args+=(--ro-bind "$real" "$real")
  done <<< "$HERDR_SANDBOX_RO_BINDS"
fi
if [ -n "${HERDR_SANDBOX_SSH_DIR+x}" ]; then
  SSHD="$HERDR_SANDBOX_SSH_DIR"
  case "$SSHD" in /*) ;; *) deny "the ssh directory must be an absolute path" ;; esac
  if [ -L "$SSHD" ] || [ ! -d "$SSHD" ]; then deny "the ssh directory must be a directory, not a link"; fi
  [ "$(owner_mode "$SSHD")" = "$(id -u) 700" ] || deny "the ssh directory must be yours with mode 0700"
  sreal="$(cd "$SSHD" && pwd -P)"
  if [ -d "$HOME/.ssh" ]; then             # a copy of the room's keys, never the operator's own keys
    oreal="$(cd "$HOME/.ssh" && pwd -P)"
    case "$sreal/" in "$oreal"/*) deny "the ssh directory must not be the operator's ~/.ssh" ;; esac
    case "$oreal/" in "$sreal"/*) deny "the ssh directory must not contain the operator's ~/.ssh" ;; esac
  fi
  args+=(--ro-bind "$sreal" "$HOME/.ssh")
fi
args+=(--bind "$WORK" "$WORK" --chdir "$WORK"
       --clearenv --setenv PATH "$(dirname "$(command -v claude 2>/dev/null || echo /usr/bin/x)"):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
       --setenv HOME "$HOME" --setenv USER "${USER:-agent}" --setenv LANG "${LANG:-C.UTF-8}" --setenv TERM dumb
       --setenv TMPDIR /tmp --setenv HERDR_SANDBOX 1 "${harness_env[@]}")
if [ "$STRICT" = 1 ]; then args+=(--setenv HERDR_SANDBOX_PROFILE task-restricted-v1); fi

# file-size and fd limits apply to everything in the box. A process cap (ulimit -u) would count every process
# of this user, so the process and memory caps are a systemd scope instead (a fork bomb stops at the cap).
ulimit -n 1024 -f "${HERDR_SANDBOX_FILE_KB:-1048576}" -c 0 2>/dev/null || true
inner=(/usr/bin/env bash -c "$CMD")
if [ -n "${HERDR_EGRESS_SOCK+x}" ]; then   # set but empty is a runner bug: fail closed, never open
  no() { echo "collab-sandbox: $1; refusing to run" >&2; exit 126; }
  RELAY="$(dirname "$0")/collab-egress.py"
  PORT="${HERDR_EGRESS_PORT:-3128}"
  [ -S "${HERDR_EGRESS_SOCK:-}" ] || no "egress socket ${HERDR_EGRESS_SOCK:-(empty)} is not a socket"
  [ -f "$RELAY" ] || no "the egress relay $RELAY is not installed"
  case "$PORT" in ''|*[!0-9]*) no "HERDR_EGRESS_PORT must be a number" ;; esac
  [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || no "HERDR_EGRESS_PORT must be 1024-65535"
  url="http://127.0.0.1:$PORT"
  args+=(--unshare-net --ro-bind "$RELAY" /run/herdr-egress/egress.py --ro-bind "$HERDR_EGRESS_SOCK" /run/herdr-egress/sock
         --setenv HTTPS_PROXY "$url" --setenv HTTP_PROXY "$url" --setenv https_proxy "$url" --setenv http_proxy "$url"
         --setenv NO_PROXY 127.0.0.1,localhost --setenv no_proxy 127.0.0.1,localhost)
  if [ -n "${HERDR_EGRESS_SERVICES:-}" ]; then
    IFS=',' read -r -a svcs <<< "$HERDR_EGRESS_SERVICES"
    for s in "${svcs[@]}"; do
      [[ "$s" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || no "bad service name in HERDR_EGRESS_SERVICES"
      n="${s^^}"
      args+=(--setenv "SVC_${n//-/_}_URL" "$url/svc/$s")
    done
  fi
  # inside the box: start the relay (stdin stays the task's, output stays out of the result), wait until it listens,
  # then run the command. The relay exits at once if it cannot reach the proxy; then the command never runs.
  inner=(/usr/bin/env bash -c '
    /usr/bin/python3 /run/herdr-egress/egress.py relay --sock /run/herdr-egress/sock --listen "127.0.0.1:$1" </dev/null >/dev/null 2>&1 &
    relay=$!
    for _ in $(seq 100); do
      if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then exec bash -c "$2"; fi
      kill -0 "$relay" 2>/dev/null || break
      sleep 0.1
    done
    echo "collab-sandbox: the egress relay did not come up; refusing to run" >&2
    exit 126' collab-sandbox "$PORT" "$CMD")
fi
BWRAP="$(command -v bwrap)" || { echo "collab-sandbox: bubblewrap (bwrap) is not installed; refusing to run unboxed" >&2; exit 126; }
run=("$BWRAP" "${args[@]}" -- "${inner[@]}")
if [ "$STRICT" = 1 ]; then
  command -v systemd-run >/dev/null || deny "cannot enforce the strict resource limits on this host"
  exec systemd-run --user --scope -q --collect --unit="$HERDR_SANDBOX_UNIT" -p TasksMax="$HERDR_SANDBOX_TASKS" -p MemoryMax="$HERDR_SANDBOX_MEM_BYTES" -p CPUQuota="$HERDR_SANDBOX_CPU_PERCENT%" -- "$0" --run-strict "$HERDR_SANDBOX_TASKS" "$HERDR_SANDBOX_MEM_BYTES" "$HERDR_SANDBOX_CPU_PERCENT" "$HERDR_SANDBOX_TIMEOUT_SECONDS" "$TIMEOUT_BIN" "${run[@]}"
fi
if systemd-run --user --scope -q -p TasksMax=1000 true 2>/dev/null; then
  exec systemd-run --user --scope -q -p TasksMax="${HERDR_SANDBOX_TASKS:-512}" -p MemoryMax="${HERDR_SANDBOX_MEM:-8G}" -- "${run[@]}"
fi
echo "collab-sandbox: no systemd user scope; running without a process/memory cap" >&2
exec "${run[@]}"
