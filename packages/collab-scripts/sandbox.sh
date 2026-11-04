#!/usr/bin/env bash
# collab-sandbox.sh WORKDIR COMMAND — run COMMAND (a shell string, task text on stdin) with only WORKDIR writable.
#
# A collaboration agent runs text a room member wrote. This is the box it runs in:
#   * the system (/usr, /etc bits) is read-only; there is no /home, no other agent's credential file, no ssh keys,
#     no cloud credentials, no collab log: the home directory is an empty temp dir
#   * WORKDIR is the one place it can create, change or delete files; everything else fails with
#     "Read-only file system"
#   * the harness itself (claude/codex/pi, node) and its login (~/.claude, ~/.codex, ~/.pi) are visible
#     READ-ONLY, so it can sign in but cannot rewrite its own settings or hooks
#   * own process namespace (it can't see or signal other processes), no extra privileges, no capabilities,
#     the environment is cleared, and limits on processes, open files and file size apply
# The network is left open: the harness has to reach its model. That, plus the harness login being readable
# to the harness, is what this does NOT protect: only start tool-enabled agents for people you trust.
# No bubblewrap, no run: this never falls back to running unboxed.
set -euo pipefail

WORK="${1:?usage: collab-sandbox.sh WORKDIR COMMAND}"
CMD="${2:?usage: collab-sandbox.sh WORKDIR COMMAND}"
BWRAP="$(command -v bwrap)" || { echo "collab-sandbox: bubblewrap (bwrap) is not installed; refusing to run unboxed" >&2; exit 126; }
AREA="$HOME/herdr-agents/work"
case "/$WORK/" in */../*) echo "collab-sandbox: workdir must not contain .." >&2; exit 126 ;; esac
case "$WORK" in "$AREA"/*) ;; *) echo "collab-sandbox: workdir must be under ~/herdr-agents/work/" >&2; exit 126 ;; esac
mkdir -p -m 0700 "$WORK"
WORK="$(cd "$WORK" && pwd -P)"
# a symlink inside the work area must not lead out of it
case "$WORK" in "$(cd "$AREA" && pwd -P)"/*) ;; *) echo "collab-sandbox: workdir resolves outside ~/herdr-agents/work/" >&2; exit 126 ;; esac

args=(--die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts --cap-drop ALL
      --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/sbin /sbin
      --ro-bind-try /etc/resolv.conf /etc/resolv.conf --ro-bind-try /run/systemd/resolve /run/systemd/resolve
      --ro-bind-try /etc/hosts /etc/hosts --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf
      --ro-bind-try /etc/ssl /etc/ssl --ro-bind-try /etc/ca-certificates /etc/ca-certificates
      --ro-bind-try /etc/passwd /etc/passwd --ro-bind-try /etc/group /etc/group --ro-bind-try /etc/localtime /etc/localtime
      --ro-bind-try /etc/alternatives /etc/alternatives
      --proc /proc --dev /dev --size 268435456 --tmpfs /tmp --size 67108864 --tmpfs /var/tmp
      --tmpfs "$HOME")

# The harness and its login, read-only. Resolve where the executables really live (symlinks into ~/.local/share,
# node under a version manager...) and expose just those directories.
seen=":"
bindro() { case "$seen" in *":$1:"*) return 0 ;; esac; seen="$seen$1:"; if [ -e "$1" ]; then args+=(--ro-bind "$1" "$1"); fi; }
for n in claude codex pi node; do
  p="$(command -v "$n" 2>/dev/null)" || continue
  r="$(readlink -f "$p")"
  case "$p" in "$HOME"/*) bindro "$(dirname "$p")" ;; esac
  case "$r" in "$HOME"/*) bindro "$(dirname "$r")" ;; esac
done
for d in .claude .codex .pi; do bindro "$HOME/$d"; done
if [ -e "$HOME/.claude.json" ]; then args+=(--ro-bind "$HOME/.claude.json" "$HOME/.claude.json"); fi
args+=(--bind "$WORK" "$WORK" --chdir "$WORK"
       --clearenv --setenv PATH "$(dirname "$(command -v claude 2>/dev/null || echo /usr/bin/x)"):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
       --setenv HOME "$HOME" --setenv USER "${USER:-agent}" --setenv LANG "${LANG:-C.UTF-8}" --setenv TERM dumb
       --setenv TMPDIR /tmp --setenv HERDR_SANDBOX 1)

# file-size and fd limits apply to everything in the box. A process cap (ulimit -u) would count every process
# of this user, so the process and memory caps are a systemd scope instead (a fork bomb stops at the cap).
ulimit -n 1024 -f "${HERDR_SANDBOX_FILE_KB:-1048576}" -c 0 2>/dev/null || true
run=("$BWRAP" "${args[@]}" -- /usr/bin/env bash -c "$CMD")
if [ "${HERDR_SANDBOX_EXTERNAL_SCOPE:-0}" = 1 ]; then
  exec "${run[@]}"
fi
if systemd-run --user --scope -q -p TasksMax=1000 true 2>/dev/null; then
  exec systemd-run --user --scope -q -p TasksMax="${HERDR_SANDBOX_TASKS:-512}" -p MemoryMax="${HERDR_SANDBOX_MEM:-8G}" -- "${run[@]}"
fi
echo "collab-sandbox: no systemd user scope; running without a process/memory cap" >&2
exec "${run[@]}"
