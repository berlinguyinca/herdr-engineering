# Source from a gate script:  . "$(dirname "$0")/pytest-parallel.sh"; python -m pytest -q $(herdr_pytest_parallel_args)
#
# Prints the pytest-xdist arguments the unit gate runs with, one word per line (empty = run serially). The serial suite takes ~90
# minutes; running it across workers is what makes the gate affordable. Everything here is overridable because the CPU count of the
# CI exec host is not known in advance:
#   HERDR_PYTEST_WORKERS       worker count; `auto` (default) = one per CPU, `0` = plain serial run, as before xdist.
#   HERDR_PYTEST_MAXPROCESSES  ceiling on the worker count (default 8; xdist applies it to `auto` and to an
#                              explicit `-n N` alike): a 100+ core host must not start 100 workers that each
#                              import the package, bind loopback servers and hold ~3,000 leaked descriptors
#                              apiece (see ensure-fd-limit.sh).
# --dist=loadfile keeps every test of a file on one worker, because the suite's fixtures and module state are written per file. It
# does NOT honour `xdist_group` marks (only --dist=loadgroup does), so files still run concurrently with each other: a test that
# cannot share a machine with other files (fixed port, shared path) must use an OS-assigned port or a per-test tmp path instead.
# The CPUs this process may really use under a cgroup CPU quota (Slurm's task/cgroup, a container's --cpus), as a whole number >= 1, or
# nothing when there is no quota. xdist's `auto` counts the cores in the CPU affinity mask, which a quota does not shrink: a job granted
# 2 CPUs on a 24-core node starts 8 workers that starve each other, and then 3 s socket reads and `pg_ctl stop` waits time out in tests
# that did nothing wrong. HERDR_CGROUP_ROOT overrides /sys/fs/cgroup (tests/test_ci_pytest_parallel.py uses a real directory).
herdr_cpu_quota() {
  python3 - "${HERDR_CGROUP_ROOT:-/sys/fs/cgroup}" <<'PY'
import math, sys
from pathlib import Path
root = Path(sys.argv[1])
own = ""
try:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            own = line[3:].strip("/")
except OSError:
    pass
def v2(path):
    try:
        quota, period = path.read_text().split()[:2]
        return None if quota == "max" else float(quota) / float(period)
    except (OSError, ValueError, ZeroDivisionError):
        return None
def v1(directory):
    try:
        quota = float((directory / "cpu.cfs_quota_us").read_text())
        period = float((directory / "cpu.cfs_period_us").read_text())
        return None if quota <= 0 else quota / period
    except (OSError, ValueError, ZeroDivisionError):
        return None
limits = [v2(root / "cpu.max"), v1(root / "cpu"), v1(root)]
if own:
    limits.append(v2(root / own / "cpu.max"))
limits = [x for x in limits if x is not None]
if limits:
    print(max(1, math.floor(min(limits))))
PY
}

herdr_pytest_parallel_args() {
  local workers="${HERDR_PYTEST_WORKERS:-auto}" cap="${HERDR_PYTEST_MAXPROCESSES:-8}"
  case "$workers" in
    0) return 0 ;;
    auto) ;;
    ''|*[!0-9]*) echo "HERDR_PYTEST_WORKERS must be a number or 'auto', got '$workers'" >&2; return 1 ;;
  esac
  case "$cap" in ''|*[!0-9]*|0) echo "HERDR_PYTEST_MAXPROCESSES must be a positive number, got '$cap'" >&2; return 1 ;; esac
  if [ "$workers" = auto ]; then
    local quota; quota="$(herdr_cpu_quota 2>/dev/null || true)"
    # A quota below both the cap and the visible cores is the real budget; otherwise `auto` is right as before.
    if [ -n "$quota" ] && [ "$quota" -lt "$cap" ] && [ "$quota" -lt "$(python3 -c 'import os; print(len(os.sched_getaffinity(0)))')" ]; then
      workers="$quota"
    fi
  fi
  printf '%s\n' -n "$workers" "--maxprocesses=$cap" --dist=loadfile
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
# expressions repeat its `not e2e and not integration` and must be kept in step with it (tests/test_ci_pytest_parallel.py checks).
HERDR_PYTEST_FAST_MARKEXPR='not e2e and not integration and not slow'
HERDR_PYTEST_SLOW_MARKEXPR='slow and not e2e and not integration'
