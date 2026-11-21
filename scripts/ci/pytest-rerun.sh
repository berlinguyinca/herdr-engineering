# Source from a gate script (after pytest-parallel.sh):  . scripts/ci/pytest-rerun.sh; herdr_pytest_run_fast watched "$HERDR_PYTEST_FAST_MARKEXPR"
#
# Pipeline main-cron #1710: two pytest-xdist workers died ("node down: Not properly terminated") on a host with load 155 on 4 CPUs, xdist
# 3.8.0's scheduler then raised an INTERNALERROR KeyError and pytest exited 3; the same commit passed in four other runs. Whether the
# workers were OOM-killed or killed from outside is not known. The parallel pass is therefore rerun ONCE when the xdist machinery broke:
# pytest exit 3, or a worker-crash signature in the output. Never rerun: a pass, a watchdog hang (124), or a genuine test failure (no
# signature), and a serial pass has no xdist to blame. The rerun uses HERDR_PYTEST_RERUN_WORKERS workers (default 2; 0 = serial, ~90 min
# against ~15 parallel and ~40 on two workers). A rerun's result is final, there is no second rerun.

#
# Second mechanism (flaky tests, not a broken runner): when the fast pass FAILED (exit 1) and was not an xdist crash, the TEST FILES of the
# failed and errored tests are run once more, serially, whole (so a test polluted by an earlier test of its own module fails again and is
# not mistaken for a flake). Tests that failed in the fast pass and PASSED on the retry are logged loudly as "RETRIED: <id> ... first
# failure: <summary>", repeated as "RETRIED-JSONL {...}" lines and written to HERDR_PYTEST_RETRIED_FILE (default pytest-retried.jsonl;
# JSON lines, rewritten each run, empty when nothing was retried). Everything else fails the gate: a test failing again, a retried test
# that was skipped or did not run (only a recorded PASS counts), a test that fails on the retry that passed in the fast pass. A test needing
# a retry in HERDR_PYTEST_CHRONIC_RUNS (3) distinct runs within HERDR_PYTEST_CHRONIC_DAYS (14) days is a CHRONIC flake: a loud CHRONIC FLAKE warning and a
# chronic_runs field in its JSONL row; it fails the gate only with HERDR_PYTEST_CHRONIC_FAIL=1 (fix it, or quarantine it with an issue); the counter is only on for push/cron events (a pull_request, manual or local run reads and writes nothing:
# 'RETRY HISTORY: off (untrusted event)'), lives in HERDR_PYTEST_RETRY_HISTORY or HERDR_CI_STATE/pytest-retried-history.jsonl (explicit paths
# may be created; the default main-cron-gates.sh state dir is used only if it already exists), is updated under flock, and is simply off,
# with a note in the log, when it cannot be written.
# HERDR_PYTEST_RETRY=0 disables the retry; more than HERDR_PYTEST_RETRY_MAX (default 25) failures are a broken change, not flakes, and
# are not retried. Not retried either: a pass whose failure is session-level (a coverage floor, an INTERNALERROR): rerunning some files
# cannot fix it, and a green retry must not paper over it. Only the fast pass has a retry: a slow tail run separately gets none. Known
# flaky tests can be quarantined (they still run, a failure does not fail the gate): see scripts/ci/herdr_flaky_plugin.py and
# tests/flaky-quarantine.toml. A crash rerun's result is final: it is not retried again.
HERDR_PYTEST_PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

herdr_pytest_session_failure() {  # the failure is the session's, not a test's (--cov-fail-under, a broken plugin): retrying files cannot fix it
  grep -qE "^(FAIL )?Required test coverage of|INTERNALERROR>|^ERROR: Coverage failure" "$1"
}

# herdr_pytest_run_fast <runner> <markexpr>: the parallel pass (arguments from the global PYTEST_PARALLEL) through <runner>, with the
# single rerun described above. Returns the final pytest status. Safe under `set -e`.
herdr_pytest_run_fast() {
  local runner="$1" markexpr="$2" log status words word restore="" crash_rerun="" failed_file ids_file
  local retried_file="${HERDR_PYTEST_RETRIED_FILE-pytest-retried.jsonl}" plugin="$HERDR_PYTEST_PLUGIN_DIR/herdr_flaky_plugin.py"
  local retry_max="${HERDR_PYTEST_RETRY_MAX-25}"
  local -a args=() ids=() files=()
  case "$retry_max" in ''|*[!0-9]*) echo "HERDR_PYTEST_RETRY_MAX must be a number, got '$retry_max'" >&2; return 1 ;; esac
  case "$-" in *e*) restore=1 ;; esac
  log="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-pass.XXXXXX")"
  failed_file="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-failed.XXXXXX")"
  set +e
  "$runner" env "PYTHONPATH=$HERDR_PYTEST_PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" "HERDR_PYTEST_FAILED_FILE=$failed_file" \
    python -m pytest -q -p herdr_flaky_plugin -m "$markexpr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"} ${PYTEST_PARALLEL[@]+"${PYTEST_PARALLEL[@]}"} 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "${#PYTEST_PARALLEL[@]}" -gt 0 ] && herdr_pytest_rerun_wanted "$status" "$log"; then
    words="$(herdr_pytest_rerun_args)" || { rm -f "$log"; [ -n "$restore" ] && set -e; return 1; }
    while IFS= read -r word; do [ -z "$word" ] || args+=("$word"); done <<<"$words"
    echo "== pytest: the parallel pass exited $status and its output shows the xdist machinery broke (worker crash or internal error, not a test failure); rerunning the parallel pytest pass once ${args[*]:+with ${args[*]}}${args[*]:-serially} =="
    "$runner" python -m pytest -q -m "$markexpr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"} ${args[@]+"${args[@]}"}
    status=$?
    crash_rerun=1
  fi
  [ -n "$retried_file" ] && : >"$retried_file"
  if [ "$status" -eq 1 ] && [ -z "$crash_rerun" ] && [ "${HERDR_PYTEST_RETRY-1}" != 0 ]; then
    while IFS= read -r -d '' word; do ids+=("$word"); done < <(python "$plugin" ids "$failed_file")
    while IFS= read -r -d '' word; do files+=("$word"); done < <(python "$plugin" files "$failed_file")
    if herdr_pytest_session_failure "$log"; then
      echo "== pytest: the pass failed at session level (coverage floor or internal error), not only in tests; nothing is retried (the failure stands) =="
    elif [ "${#ids[@]}" -gt 0 ] && [ "${#files[@]}" -eq 0 ]; then
      echo "== pytest: the failed ids (${ids[*]}) name no test file (an xdist worker crash?); nothing safe to retry, the failure stands =="
    elif [ "${#ids[@]}" -eq 0 ]; then
      echo "== pytest: the pass failed but no failed test ids were recorded; not retrying (the failure stands) =="
    elif [ "${#ids[@]}" -gt "$retry_max" ]; then
      echo "== pytest: ${#ids[@]} tests failed, more than HERDR_PYTEST_RETRY_MAX=$retry_max; that is a broken change, not flakes, so nothing is retried =="
    else
      echo "== pytest: ${#ids[@]} test(s) failed in the fast pass (${ids[*]}); retrying their ${#files[@]} test file(s) once, serially and whole, so same-module pollution reproduces: ${files[*]} =="
      ids_file="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-failed.XXXXXX")"
      "$runner" env "PYTHONPATH=$HERDR_PYTEST_PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" "HERDR_PYTEST_FAILED_FILE=$ids_file" \
        python -m pytest -q -p herdr_flaky_plugin -m "$markexpr" "${files[@]}"
      status=$?
      # 0 only when the retry passed AND settle is satisfied: a failure the retry could not attribute (a hang, an internal error) or
      # a retried test that did not actually pass (skipped) can never come out as a pass.
      python "$plugin" settle "$failed_file" "$ids_file" "${retried_file:-/dev/null}" || [ "$status" -ne 0 ] || status=1
      rm -f "$ids_file"
    fi
  fi
  rm -f "$log" "$failed_file"
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
  # nice, and ionice -c3 (idle I/O class) where the host has it: the tail must never slow the workers' CPU or disk; no ionice = silently just nice.
  local slow_nice="nice -n 5"
  command -v ionice >/dev/null 2>&1 && ionice -c3 true >/dev/null 2>&1 && slow_nice="ionice -c3 nice -n 5"
  statusf="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-slow.XXXXXX")"
  if [ "${HERDR_PYTEST_SLOW_CONCURRENT:-1}" != 0 ]; then
    echo "== pytest (slow tests: serial tail, running alongside the fast pass; its lines start with [slow]) =="
    ( "$slow_runner" $slow_nice python -m pytest -q -p no:cacheprovider -m "$slow_expr" ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"}
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
