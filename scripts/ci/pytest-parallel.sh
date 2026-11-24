# Source from a gate script:  . "$(dirname "$0")/pytest-parallel.sh"; python -m pytest -q $(herdr_pytest_parallel_args)
#
# Prints the pytest-xdist arguments the unit gate runs with, one word per line (empty = run serially). The serial suite takes ~90
# minutes; running it across workers is what makes the gate affordable. Everything here is overridable because the CPU count of the
# CI exec host is not known in advance:
#   HERDR_PYTEST_WORKERS       worker count; `auto` (default) = one per CPU, `0` = plain serial run, as before xdist.
#   HERDR_PYTEST_MAXPROCESSES  ceiling on the worker count (default 8, empty = default; xdist applies it to `auto` and to an
#                              explicit `-n N` alike): a 100+ core host must not start 100 workers that each
#                              import the package, bind loopback servers and hold ~3,000 leaked descriptors
#                              apiece (see ensure-fd-limit.sh).
# --dist=loadfile keeps every test of a file on one worker, because the suite's fixtures and module state are written per file. It
# does NOT honour `xdist_group` marks (only --dist=loadgroup does), so files still run concurrently with each other: a test that
# cannot share a machine with other files (fixed port, shared path) must use an OS-assigned port or a per-test tmp path instead.
# The CPUs this process may really use under a cgroup CPU quota (smallest quota from its cgroup up to the root, v1 and v2), as a whole
# number >= 1, or nothing when there is no quota: scripts/ci/cpu-quota.py. xdist's `auto` counts the cores in the affinity mask, which a
# quota does not shrink: a job granted 2 CPUs on a 24-core node starts 8 workers that starve each other, and then 3 s socket reads and
# `pg_ctl stop` waits time out in tests that did nothing wrong. HERDR_CGROUP_ROOT and HERDR_PROC_CGROUP override /sys/fs/cgroup and
# /proc/self/cgroup (tests/test_ci_pytest_parallel.py builds real directory trees).
herdr_cpu_quota() {
  python3 "$(dirname "${BASH_SOURCE[0]}")/cpu-quota.py" "${HERDR_CGROUP_ROOT:-/sys/fs/cgroup}" "${HERDR_PROC_CGROUP:-/proc/self/cgroup}"
}

# The load average file (HERDR_LOADAVG_FILE overrides /proc/loadavg for tests; scripts/ci/host-facts.sh prints the same file).
herdr_loadavg_file() { echo "${HERDR_LOADAVG_FILE:-/proc/loadavg}"; }

# The CPUs this process may use (the affinity mask; `taskset` in tests narrows it for real).
herdr_visible_cpus() { python3 -c 'import os; print(len(os.sched_getaffinity(0)))'; }

# The CPUs the host's 1-minute load average leaves free, as a whole number, or nothing when the file is unreadable. Pipeline 1710
# started 8 workers on a host with load 155 on 4 CPUs; two of them died (#888). But the load of a shared node is mostly OTHER tenants'
# and says little about what this job can get: load 28 on 16 CPUs gave `-n 1` and 104-120 minute gates against 15-22 minutes with 8
# workers. So load only shaves workers down to a FLOOR of half the CPUs and at least 4 (never above the CPUs themselves); a genuinely
# small budget is the cgroup quota's job (herdr_cpu_quota), not the load average's. It only ever lowers `auto`; an explicit
# HERDR_PYTEST_WORKERS wins.
herdr_free_cpus() {
  local load cpus
  load="$(cut -d' ' -f1 "$(herdr_loadavg_file)" 2>/dev/null)" || return 0
  case "$load" in ''|*[!0-9.]*|*.*.*) return 0 ;; esac
  cpus="$(herdr_visible_cpus)"
  awk -v l="$load" -v c="$cpus" 'BEGIN {
    f = c - int(l); floor = int(c / 2); if (floor < 4) floor = 4; if (floor > c) floor = c
    print (f < floor ? floor : f) }'
}

herdr_pytest_parallel_args() {
  local workers="${HERDR_PYTEST_WORKERS:-auto}" cap="${HERDR_PYTEST_MAXPROCESSES:-8}"   # empty means the default; 12 is not the default until a 12-worker run is measured
  case "$workers" in
    0) return 0 ;;
    auto) ;;
    ''|*[!0-9]*) echo "HERDR_PYTEST_WORKERS must be a number or 'auto', got '$workers'" >&2; return 1 ;;
  esac
  case "$cap" in ''|*[!0-9]*|0) echo "HERDR_PYTEST_MAXPROCESSES must be a positive number, got '$cap'" >&2; return 1 ;; esac
  if [ "$workers" = auto ]; then
    local cpus; cpus="$(herdr_visible_cpus)"
    local quota; quota="$(herdr_cpu_quota 2>/dev/null || true)"
    # A quota below the visible cores is the real budget; otherwise `auto` is right as before.
    if [ -n "$quota" ] && [ "$quota" -lt "$cpus" ]; then
      workers="$quota"; [ "$workers" -le "$cap" ] || workers="$cap"
    fi
    # Then the CPUs the host's current load leaves free, never below the floor above: only ever lower.
    local free; free="$(herdr_free_cpus)"
    if [ -n "$free" ] && [ "$free" -lt "$cap" ] && [ "$free" -lt "$cpus" ] \
       && { [ "$workers" = auto ] || [ "$free" -lt "$workers" ]; }; then
      workers="$free"
    fi
  fi
  printf '%s\n' -n "$workers" "--maxprocesses=$cap" --dist=loadfile
}

# Fills the global array PYTEST_REPORT (read as ${PYTEST_REPORT[@]+"${PYTEST_REPORT[@]}"}) with the arguments that make a pass report where
# its time went: the 40 slowest tests (`--durations=40`) and, from the scripts/ci/pytest-plugins/pytest_file_times.py plugin, a per-test-file
# and per-xdist-worker total, which shows `--dist=loadfile` imbalance (one slow file keeping one worker busy while the rest idle).
# Plus pytest_ci_progress (scripts/ci/pytest-plugins/pytest_ci_progress.py), which reports the same view WHILE the pass runs, because a
# teardown summary is worth nothing to a step the platform stopped (issue #937).
PYTEST_REPORT=()
herdr_pytest_report_load() {
  PYTEST_REPORT=(--durations=40 -p pytest_file_times -p pytest_ci_progress)
  export PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pytest-plugins${PYTHONPATH:+:$PYTHONPATH}"
}

# Progress cadence and the per-pass wall budget (#937), exported for pytest_ci_progress. The budgets are measured, not guessed: since the
# shards landed (#919, pipelines 1958-2012) a shard's median wall is 346-611 s and its maximum 741 s, so 900 s marks a shard overrunning
# long before the platform stops it (pipeline 1978's shard 3 was `killed` at 1950 s having said nothing about what it was inside). An
# unsharded pass - `unit-gates-main`, or anyone's local run - measured 999-1405 s, and the single-pass gate ran 6829 s in pipeline 1754,
# so its budget is 1800 s. Exceeding a budget prints a dump of what each worker is inside and keeps running: scripts/ci/unit-gates.sh has
# deliberately no flat cap ("a slow but live run is never killed") and scripts/ci/run-watched.py owns stopping a wedged one.
herdr_pytest_progress_defaults() {
  local every="${HERDR_PYTEST_PROGRESS_EVERY_SECONDS:-60}"
  local tests="${HERDR_PYTEST_PROGRESS_EVERY_TESTS:-200}"
  local budget="${HERDR_PYTEST_PROGRESS_BUDGET_SECONDS-}"
  case "$every" in ''|*[!0-9]*) echo "HERDR_PYTEST_PROGRESS_EVERY_SECONDS must be a whole number of seconds, got '$every'" >&2; return 1 ;; esac
  case "$tests" in ''|*[!0-9]*) echo "HERDR_PYTEST_PROGRESS_EVERY_TESTS must be a whole number of tests, got '$tests'" >&2; return 1 ;; esac
  if [ -z "$budget" ]; then
    if [ -n "${HERDR_UNIT_SHARD:-}" ]; then budget=900; else budget=1800; fi
  fi
  case "$budget" in ''|*[!0-9]*) echo "HERDR_PYTEST_PROGRESS_BUDGET_SECONDS must be a whole number of seconds (0 = no budget), got '$budget'" >&2; return 1 ;; esac
  export HERDR_PYTEST_PROGRESS_EVERY_SECONDS="$every"
  export HERDR_PYTEST_PROGRESS_EVERY_TESTS="$tests"
  export HERDR_PYTEST_PROGRESS_BUDGET_SECONDS="$budget"
  export HERDR_PYTEST_PROGRESS_DIR="${HERDR_PYTEST_PROGRESS_DIR:-$PWD/.tmp/ci-progress}"
  mkdir -p "$HERDR_PYTEST_PROGRESS_DIR" || return 1
  echo "ci progress: dir=$HERDR_PYTEST_PROGRESS_DIR every=${every}s/${tests}-tests budget=${budget}s shard=${HERDR_UNIT_SHARD:-none}"
}

# Fills the global array PYTEST_PARALLEL with those arguments (empty for a serial run) and fails if an override is invalid. Read it
# as ${PYTEST_PARALLEL[@]+"${PYTEST_PARALLEL[@]}"} so an empty array is safe under `set -u` on older bash. Words are read one per
# line on purpose: `echo $words` would swallow the leading `-n` as echo's own option.
herdr_pytest_parallel_load() {
  local words word
  PYTEST_PARALLEL=()
  words="$(herdr_pytest_parallel_args)" || return 1
  while IFS= read -r word; do
    [ -z "$word" ] || PYTEST_PARALLEL+=("$word")
  done <<<"$words"
  return 0
}

# The one test that builds ~1.77M Postgres rows (tests/control_plane/test_postgres_recovery.py) is marked `slow`: ~5.5 minutes alone and
# over 23 under disk contention, it must not compete with the parallel workers. The gate runs everything else in parallel and the slow
# tests serially afterwards, so coverage is identical. `-m` on the command line REPLACES pyproject's addopts expression, so both
# expressions repeat its `not e2e and not integration and not herdr_live` and must be kept in step with it (tests/test_ci_pytest_parallel.py checks).
HERDR_PYTEST_FAST_MARKEXPR='not e2e and not integration and not herdr_live and not slow'
HERDR_PYTEST_SLOW_MARKEXPR='slow and not e2e and not integration and not herdr_live'

# Sharding (.woodpecker/unit-gates-shard-N.yml). HERDR_UNIT_SHARD=<i>/<n> restricts the gate to the test files of shard i of n
# (scripts/ci/unit-shard.py: a deterministic split by path hash, checked to be a partition by tests/test_ci_unit_shards.py). Unset = the
# whole suite, as before. Fills PYTEST_SHARD_FILES (the fast pass's file arguments; empty when unsharded) and PYTEST_SLOW_FILES (the
# slow-marked files of this shard: the serial tail runs only on those, and not at all in a shard that owns none, because pytest exits 5
# when nothing is collected). HERDR_UNIT_SHARDED is 1 when a shard is active. A malformed value fails loudly: an empty shard is a
# green that proved nothing.
PYTEST_SHARD_FILES=(); PYTEST_SLOW_FILES=(); HERDR_UNIT_SHARDED=0
herdr_pytest_shard_load() {
  PYTEST_SHARD_FILES=(); PYTEST_SLOW_FILES=(); HERDR_UNIT_SHARDED=0
  [ -n "${HERDR_UNIT_SHARD:-}" ] || return 0
  local tool="$(dirname "${BASH_SOURCE[0]}")/unit-shard.py" f out
  out="$(python3 "$tool" files "$HERDR_UNIT_SHARD")" || return 1
  while IFS= read -r f; do [ -z "$f" ] || PYTEST_SHARD_FILES+=("$f"); done <<<"$out"
  [ "${#PYTEST_SHARD_FILES[@]}" -gt 0 ] || { echo "shard $HERDR_UNIT_SHARD has no test files" >&2; return 1; }
  out="$(python3 "$tool" slow-files "$HERDR_UNIT_SHARD")" || return 1
  while IFS= read -r f; do [ -z "$f" ] || PYTEST_SLOW_FILES+=("$f"); done <<<"$out"
  HERDR_UNIT_SHARDED=1
  echo "unit shard $HERDR_UNIT_SHARD: ${#PYTEST_SHARD_FILES[@]} test files, ${#PYTEST_SLOW_FILES[@]} with a slow tail"
}
