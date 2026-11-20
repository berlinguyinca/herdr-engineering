# Source from a gate script (after pytest-parallel.sh):  . scripts/ci/pytest-rerun.sh; herdr_pytest_run_fast watched "$HERDR_PYTEST_FAST_MARKEXPR"
#
# Pipeline main-cron #1710: two pytest-xdist workers died ("node down: Not properly terminated") on a host with load 155 on 4 CPUs, xdist
# 3.8.0's scheduler then raised an INTERNALERROR KeyError and pytest exited 3; the same commit passed in four other runs. Whether the
# workers were OOM-killed or killed from outside is not known. The parallel pass is therefore rerun ONCE when the xdist machinery broke:
# pytest exit 3, or a worker-crash signature in the output. Never rerun: a pass, a watchdog hang (124), or a genuine test failure (no
# signature), and a serial pass has no xdist to blame. The rerun uses HERDR_PYTEST_RERUN_WORKERS workers (default 2; 0 = serial, ~90 min
# against ~15 parallel and ~40 on two workers). A rerun's result is final, there is no second rerun.

herdr_pytest_crash_signature() {
  grep -qE "node down: Not properly terminated|worker 'gw[0-9]+' crashed|INTERNALERROR>.*xdist/|INTERNALERROR> KeyError: <WorkerController" "$1"
}

# herdr_pytest_rerun_wanted <pytest exit status> <captured output file>: 0 = rerun.
herdr_pytest_rerun_wanted() {
  case "$1" in 0|124) return 1 ;; 3) return 0 ;; esac
  herdr_pytest_crash_signature "$2"
}

herdr_pytest_rerun_args() {
  local workers="${HERDR_PYTEST_RERUN_WORKERS-2}"
  case "$workers" in
    0) return 0 ;;
    ''|*[!0-9]*) echo "HERDR_PYTEST_RERUN_WORKERS must be a number (0 = serial), got '$workers'" >&2; return 1 ;;
  esac
  printf '%s\n' -n "$workers" "--maxprocesses=$workers" --dist=loadfile
}

# herdr_pytest_run_fast <runner> <markexpr>: the parallel pass (arguments from the global PYTEST_PARALLEL) through <runner>, with the
# single rerun described above. Returns the final pytest status. Safe under `set -e`.
herdr_pytest_run_fast() {
  local runner="$1" markexpr="$2" log status words word restore=""
  local -a args=()
  case "$-" in *e*) restore=1 ;; esac
  log="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-pass.XXXXXX")"
  set +e
  "$runner" python -m pytest -q -m "$markexpr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"} ${PYTEST_PARALLEL[@]+"${PYTEST_PARALLEL[@]}"} 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "${#PYTEST_PARALLEL[@]}" -gt 0 ] && herdr_pytest_rerun_wanted "$status" "$log"; then
    words="$(herdr_pytest_rerun_args)" || { rm -f "$log"; [ -n "$restore" ] && set -e; return 1; }
    while IFS= read -r word; do [ -z "$word" ] || args+=("$word"); done <<<"$words"
    echo "== pytest: the parallel pass exited $status and its output shows the xdist machinery broke (worker crash or internal error, not a test failure); rerunning the parallel pytest pass once ${args[*]:+with ${args[*]}}${args[*]:-serially} =="
    "$runner" python -m pytest -q -m "$markexpr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"} ${args[@]+"${args[@]}"}
    status=$?
  fi
  rm -f "$log"
  [ -n "$restore" ] && set -e
  return "$status"
}

# herdr_pytest_run_gate <fast runner> <slow runner> <fast markexpr> <slow markexpr>: the fast parallel pass (herdr_pytest_run_fast, with its
# one rerun) and the serial `slow` tail, run AT THE SAME TIME. The tail is one process (about one core, niced so it never takes a CPU from
# the workers) whose output is streamed with a `[slow] ` prefix; it ran after the fast pass before (~5.7 min on top of ~14.7).
# Safe alongside xdist: its one test (tests/control_plane/test_postgres_recovery.py) starts its own PostgreSQL on a unix socket in a
# pid-named mkdtemp dir, so it shares no port, socket or path with a worker; it runs with `-p no:cacheprovider` so two pytest processes
# never write .pytest_cache together. Each pass keeps its own watchdog (the runners). Returns the fast pass's status when it failed,
# else the tail's: a gate fails if either fails, and the tail is always waited for (exit 5, nothing collected, fails on purpose).
# HERDR_PYTEST_SLOW_CONCURRENT=0 runs the tail after the fast pass, as before.
herdr_pytest_run_gate() {
  local fast_runner="$1" slow_runner="$2" fast_expr="$3" slow_expr="$4" restore="" fast=0 slow=0 pid="" statusf
  case "$-" in *e*) restore=1 ;; esac
  set +e
  statusf="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-slow.XXXXXX")"
  if [ "${HERDR_PYTEST_SLOW_CONCURRENT:-1}" != 0 ]; then
    echo "== pytest (slow tests: serial tail, running alongside the fast pass; its lines start with [slow]) =="
    ( "$slow_runner" nice -n 5 python -m pytest -q -p no:cacheprovider -m "$slow_expr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"}
      echo $? >"$statusf" ) 2>&1 | sed -u 's/^/[slow] /' &
    pid=$!
  fi
  herdr_pytest_run_fast "$fast_runner" "$fast_expr"; fast=$?
  if [ -n "$pid" ]; then
    wait "$pid"
    slow="$(cat "$statusf" 2>/dev/null)"; slow="${slow:-1}"
  else
    echo "== pytest (slow tests, serial tail) =="
    "$slow_runner" python -m pytest -q -m "$slow_expr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"}; slow=$?
  fi
  rm -f "$statusf"
  [ "$fast" -eq 0 ] || echo "== pytest: the fast parallel pass failed (exit $fast) =="
  [ "$slow" -eq 0 ] || echo "== pytest: the slow tail failed (exit $slow) =="
  [ -n "$restore" ] && set -e
  [ "$fast" -ne 0 ] && return "$fast"
  return "$slow"
}
