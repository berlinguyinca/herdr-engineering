# Source from a gate script:  . "$(dirname "$0")/ensure-fd-limit.sh"
#
# A full `pytest` run of this repository holds about 3,000 more file descriptors at the end than at the start - measured on Linux
# at `df39b736` across 20,033 tests: 15 open at the first test, 3,032 at the last. The control-plane tests bind loopback servers
# and spawn subprocesses, and the descriptors are not all returned. That is a leak worth fixing at the source, but until it is,
# the limit decides how the run ends: on a host with the common 1,024 soft limit the suite runs out roughly a third of the way
# through and pytest dies with `INTERNALERROR ... OSError: [Errno 24] Too many open files` - a truncated log with **no summary
# line and no verdict**, which is worse than a failure because it reads as a hang and invites a wrong diagnosis (issue #651).
#
# So: raise the soft limit to the hard limit before running, and if that still leaves less than the floor below, say so and refuse
# rather than produce a truncated non-verdict. A run that stops with an explanation is a result; a run that stops at 38% silently
# is not.
#
# Set HERDR_FD_FLOOR to override the floor (16,384 is roughly 5x the measured peak, which leaves room for a suite that grows).
herdr_ensure_fd_limit() {
  local floor="${HERDR_FD_FLOOR:-16384}" hard now target
  now="$(ulimit -Sn 2>/dev/null)" || return 0
  hard="$(ulimit -Hn 2>/dev/null)" || hard=""
  # `unlimited` (or anything not numeric) is already past any floor we would set.
  case "$now" in ''|*[!0-9]*) echo "file descriptor soft limit: ${now:-unknown} (hard: ${hard:-unknown}); floor ${floor} not applied"; return 0 ;; esac
  [ "$now" -ge "$floor" ] && { echo "file descriptor soft limit: $now (hard: ${hard:-unknown}); floor $floor met"; return 0; }
  case "$hard" in
    ''|*[!0-9]*|unlimited) target="$floor" ;;
    *) target="$hard" ;;
  esac
  # Never lower the limit, and tolerate a host that will not let us raise it at all.
  if [ "$now" -lt "$target" ] 2>/dev/null; then ulimit -n "$target" 2>/dev/null || true; fi
  now="$(ulimit -Sn 2>/dev/null)"
  echo "file descriptor soft limit: raised ${hard:+to }${now:-unknown} (hard: ${hard:-unknown}; floor $floor)"
  case "$now" in ''|*[!0-9]*) return 0 ;; esac
  if [ "$now" -lt "$floor" ]; then
    echo "REFUSING to run the suite: the file descriptor soft limit is $now and the floor is $floor (the hard limit is ${hard:-unknown}, so it cannot be raised far enough)." >&2
    echo "  A full run needs roughly 3,000 descriptors; below the floor pytest dies mid-run with 'Too many open files' and prints" >&2
    echo "  no summary, which looks like a hang and is not one. Raise the limit for this shell (ulimit -n $floor) or the session's" >&2
    echo "  rlimit (systemd: LimitNOFILE=; ulimit -Hn currently ${hard:-unknown}), then re-run." >&2
    return 1
  fi
  return 0
}

# Run it directly (not sourced) to just report and validate the current shell.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -uo pipefail
  herdr_ensure_fd_limit
fi
