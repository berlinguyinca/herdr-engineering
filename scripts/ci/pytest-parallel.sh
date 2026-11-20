# Source from a gate script:  . "$(dirname "$0")/pytest-parallel.sh"; python -m pytest -q $(herdr_pytest_parallel_args)
#
# Prints the pytest-xdist arguments the unit gate runs with, one word per line (empty = run serially). The serial suite takes ~90
# minutes; running it across workers is what makes the gate affordable. Everything here is overridable because the CPU count of the
# CI exec host is not known in advance:
#   HERDR_PYTEST_WORKERS       worker count; `auto` (default) = one per CPU, `0` = plain serial run, as before xdist.
#   HERDR_PYTEST_MAXPROCESSES  ceiling on the worker count (default: the usable cores, at least 8 and at most 12; xdist applies it to `auto` and to an
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

# The default ceiling on the worker count: the cores this job can really use (visible cores, or a smaller cgroup quota), at least 8
# and at most 12. HERDR_PYTEST_MAXPROCESSES overrides it.
herdr_default_maxprocesses() {
  local cpus quota avail
  cpus="$(herdr_visible_cpus)"; quota="$(herdr_cpu_quota 2>/dev/null || true)"; avail="$cpus"
  [ -n "$quota" ] && [ "$quota" -lt "$avail" ] && avail="$quota"
  awk -v a="$avail" 'BEGIN { print (a < 8 ? 8 : (a > 12 ? 12 : a)) }'
}

herdr_pytest_parallel_args() {
  local workers="${HERDR_PYTEST_WORKERS:-auto}" cap="${HERDR_PYTEST_MAXPROCESSES:-}"
  [ -n "${HERDR_PYTEST_MAXPROCESSES+x}" ] || cap="$(herdr_default_maxprocesses)"
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
PYTEST_REPORT=()
herdr_pytest_report_load() {
  PYTEST_REPORT=(--durations=40 -p pytest_file_times)
  export PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pytest-plugins${PYTHONPATH:+:$PYTHONPATH}"
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
