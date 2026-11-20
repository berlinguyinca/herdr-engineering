# Source from a gate script (after pytest-parallel.sh):  . scripts/ci/pytest-rerun.sh; herdr_pytest_run_fast watched "$HERDR_PYTEST_FAST_MARKEXPR"
#
# Pipeline main-cron #1710: two pytest-xdist workers died ("node down: Not properly terminated") on a host with load 155 on 4 CPUs, xdist
# 3.8.0's scheduler then raised an INTERNALERROR KeyError and pytest exited 3; the same commit passed in four other runs. Whether the
# workers were OOM-killed or killed from outside is not known. The parallel pass is therefore rerun ONCE when the xdist machinery broke:
# pytest exit 3, or a worker-crash signature in the output. Never rerun: a pass, a watchdog hang (124), or a genuine test failure (no
# signature), and a serial pass has no xdist to blame. The rerun uses HERDR_PYTEST_RERUN_WORKERS workers (default 2; 0 = serial, ~90 min
# against ~15 parallel and ~40 on two workers). A rerun's result is final, there is no second rerun.

#
# Second mechanism (flaky tests, not a broken runner): when the fast pass FAILED (exit 1) and was not an xdist crash, the failed and
# errored tests - and only those - are run once more, serially, by explicit node id. Tests failing in both passes are real failures and
# the pass still fails. Tests that pass on the retry are logged loudly as "RETRIED: <id> ... first failure: <summary>" and listed in
# HERDR_PYTEST_RETRIED_FILE (default pytest-retried.jsonl; JSON lines, rewritten each run, empty when nothing was retried) so flakes can
# be tracked. HERDR_PYTEST_RETRY=0 disables the retry; more than HERDR_PYTEST_RETRY_MAX (default 25) failures are a broken change, not
# flakes, and are not retried. Known flaky tests can be quarantined (they still run, a failure does not fail the gate): see
# scripts/ci/herdr_flaky_plugin.py and tests/flaky-quarantine.toml. A crash rerun's result is final: it is not retried again.
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

# herdr_pytest_run_fast <runner> <markexpr>: the parallel pass (arguments from the global PYTEST_PARALLEL) through <runner>, with the
# single rerun described above. Returns the final pytest status. Safe under `set -e`.
herdr_pytest_run_fast() {
  local runner="$1" markexpr="$2" log status words word restore="" crash_rerun="" failed_file ids_file
  local retried_file="${HERDR_PYTEST_RETRIED_FILE-pytest-retried.jsonl}" plugin="$HERDR_PYTEST_PLUGIN_DIR/herdr_flaky_plugin.py"
  local -a args=() ids=()
  case "$-" in *e*) restore=1 ;; esac
  log="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-pass.XXXXXX")"
  failed_file="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-failed.XXXXXX")"
  set +e
  "$runner" env "PYTHONPATH=$HERDR_PYTEST_PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" "HERDR_PYTEST_FAILED_FILE=$failed_file" \
    python -m pytest -q -p herdr_flaky_plugin -m "$markexpr" ${PYTEST_PARALLEL[@]+"${PYTEST_PARALLEL[@]}"} 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  if [ "${#PYTEST_PARALLEL[@]}" -gt 0 ] && herdr_pytest_rerun_wanted "$status" "$log"; then
    words="$(herdr_pytest_rerun_args)" || { rm -f "$log"; [ -n "$restore" ] && set -e; return 1; }
    while IFS= read -r word; do [ -z "$word" ] || args+=("$word"); done <<<"$words"
    echo "== pytest: the parallel pass exited $status and its output shows the xdist machinery broke (worker crash or internal error, not a test failure); rerunning the parallel pytest pass once ${args[*]:+with ${args[*]}}${args[*]:-serially} =="
    "$runner" python -m pytest -q -m "$markexpr" ${args[@]+"${args[@]}"}
    status=$?
    crash_rerun=1
  fi
  [ -n "$retried_file" ] && : >"$retried_file"
  if [ "$status" -eq 1 ] && [ -z "$crash_rerun" ] && [ "${HERDR_PYTEST_RETRY-1}" != 0 ]; then
    while IFS= read -r -d '' word; do ids+=("$word"); done < <(python "$plugin" ids "$failed_file")
    if [ "${#ids[@]}" -eq 0 ]; then
      echo "== pytest: the pass failed but no failed test ids were recorded; not retrying (the failure stands) =="
    elif [ "${#ids[@]}" -gt "${HERDR_PYTEST_RETRY_MAX:-25}" ]; then
      echo "== pytest: ${#ids[@]} tests failed, more than HERDR_PYTEST_RETRY_MAX=${HERDR_PYTEST_RETRY_MAX:-25}; that is a broken change, not flakes, so nothing is retried =="
    else
      echo "== pytest: ${#ids[@]} test(s) failed in the fast pass; retrying ONLY those once, serially: ${ids[*]} =="
      ids_file="$(mktemp "${TMPDIR:-/tmp}/herdr-pytest-failed.XXXXXX")"
      "$runner" env "PYTHONPATH=$HERDR_PYTEST_PLUGIN_DIR${PYTHONPATH:+:$PYTHONPATH}" "HERDR_PYTEST_FAILED_FILE=$ids_file" \
        python -m pytest -q -p herdr_flaky_plugin -m "$markexpr" "${ids[@]}"
      status=$?
      python "$plugin" settle "$failed_file" "$ids_file" "${retried_file:-/dev/null}"
      # The retry's own exit status decides: 0 only when every retried test passed; a failure the retry could not even
      # attribute (a hang, an internal error) can never come out as a pass.
      rm -f "$ids_file"
    fi
  fi
  rm -f "$log" "$failed_file"
  [ -n "$restore" ] && set -e
  return "$status"
}
