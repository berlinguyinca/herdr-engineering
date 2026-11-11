# Source from a gate script:  . "$(dirname "$0")/pytest-parallel.sh"; python -m pytest -q $(herdr_pytest_parallel_args)
#
# Prints the pytest-xdist arguments the unit gate runs with, one word per line (empty = run serially). The serial suite takes ~90
# minutes; running it across workers is what makes the gate affordable. Everything here is overridable because the CPU count of the
# CI exec host is not known in advance:
#   HERDR_PYTEST_WORKERS       worker count; `auto` (default) = one per CPU, `0` = plain serial run, as before xdist.
#   HERDR_PYTEST_MAXPROCESSES  ceiling on `auto` (default 8): a 100+ core host must not start 100 workers that each import the
#                              package, bind loopback servers and hold ~3,000 leaked descriptors apiece (see ensure-fd-limit.sh).
# --dist=loadfile keeps every test of a file on one worker, because the suite's fixtures and module state are written per file. A
# test that cannot share a machine with others (fixed port, shared path) is marked `xdist_group`, never deselected: loadfile already
# honours it by keeping the whole file together.
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
