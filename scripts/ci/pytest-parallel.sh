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
herdr_pytest_parallel_args() {
  local workers="${HERDR_PYTEST_WORKERS:-auto}" cap="${HERDR_PYTEST_MAXPROCESSES:-8}"
  case "$workers" in
    0) return 0 ;;
    auto) ;;
    ''|*[!0-9]*) echo "HERDR_PYTEST_WORKERS must be a number or 'auto', got '$workers'" >&2; return 1 ;;
  esac
  case "$cap" in ''|*[!0-9]*|0) echo "HERDR_PYTEST_MAXPROCESSES must be a positive number, got '$cap'" >&2; return 1 ;; esac
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
