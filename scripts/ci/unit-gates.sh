#!/usr/bin/env bash
# The Python and client unit gates as CI runs them (.woodpecker/unit-gates.yml) and as anyone can run them from a clean checkout:
#   bash scripts/ci/unit-gates.sh
# Installs nothing system-wide (CI hosts have no apt and no sudo); needs Python 3.12+ with venv, Node 22 and git already on the host.
# ruff is not a gate yet: main has 122 existing findings (scripts/validate.sh runs it).
# It runs the whole suites, not a scoped subset: a scoped gate hides the breakage a change causes elsewhere.
#
# It raises the file-descriptor soft limit before running: the suite leaks roughly 3,000 descriptors over a full run, and a host
# with the common 1,024 soft limit dies about a third of the way through with no summary at all. See scripts/ci/ensure-fd-limit.sh.
#
# pytest runs in parallel under pytest-xdist (serial it took ~90 minutes), `--dist=loadfile` so a file's tests share a worker.
# Overrides: HERDR_PYTEST_WORKERS=<n>|auto|0 (0 = serial), HERDR_PYTEST_MAXPROCESSES=<cap on auto, default 8>. See
# scripts/ci/pytest-parallel.sh. The descriptor limit is per process, so each worker leaks only its share of the ~3,000.
#
# Hang protection (user rule: timeouts only kill hangs). Each pytest pass runs under scripts/ci/run-watched.py (a copy of the shared
# run-watched helper, extended: it tracks the command's session, parent-pid descendants and any process carrying its per-run token in its environment, which covers a Postgres started by pg_ctl; a hang or SIGTERM/SIGINT/SIGHUP stops that whole tree, and survivors left after the command exits get a bounded grace and are then stopped): after HERDR_PYTEST_SOFT_SECONDS (default 3600) of wall time it prints one notice and keeps going; it kills the
# pass (exit 124) only after HERDR_PYTEST_HANG_SECONDS (default 1800) with no output AND no CPU use anywhere in the pass. A slow but
# live run is never killed; a wedged one (pipeline 1362 sat ~8 h with no result) fails with a clear message. There is no flat cap.
#
# Tests marked `slow` are deselected from the parallel pass and run serially in a tail pass (see pytest-parallel.sh).
set -euo pipefail
cd "$(dirname "$0")/../.."
. scripts/ci/journal.sh; ci_journal unit-gates

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3; need node; need git

. scripts/ci/ensure-fd-limit.sh; herdr_ensure_fd_limit || exit 1
. scripts/ci/host-facts.sh; ci_host_facts || true
. scripts/ci/pytest-parallel.sh
herdr_pytest_parallel_load || exit 1     # a bad override fails loudly instead of silently running serial

venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-unit-gates-venv.XXXXXX")"
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install -q -e '.[dev,collab,review-gate,federation]'
export PATH="$venv/bin:$PATH"

echo "== pytest =="
echo "pytest arguments: ${PYTEST_PARALLEL[*]:-(serial)}"
watched() { python3 scripts/ci/run-watched.py "${HERDR_PYTEST_SOFT_SECONDS:-3600}" "${HERDR_PYTEST_HANG_SECONDS:-1800}" -- "$@"; }
watched python -m pytest -q -m "$HERDR_PYTEST_FAST_MARKEXPR" ${PYTEST_PARALLEL[@]+"${PYTEST_PARALLEL[@]}"}
echo "== pytest (slow tests, serial tail) =="
# Tail pass: pytest-timeout (3600 s on the slow test) must be able to fire and NAME a hung test before the watchdog kills the pass,
# so the no-progress limit here is above it (4200 s). Exit 5 (no slow tests collected) fails the gate on purpose: the slow tests
# disappearing silently would mean the coverage moved out of the gate; remove the marker and this pass together.
HERDR_PYTEST_HANG_SECONDS="${HERDR_PYTEST_SLOW_HANG_SECONDS:-4200}" watched python -m pytest -q -m "$HERDR_PYTEST_SLOW_MARKEXPR"
echo "== client tests =="
npm ci --no-audit --no-fund       # the component tests import playwright
npx playwright install chromium     # one component test drives a real browser; a no-op when cached
node --test --test-timeout=300000 packages/herdr-web-client/*.test.js packages/herdr-web-components/*.test.js packages/*.test.js
