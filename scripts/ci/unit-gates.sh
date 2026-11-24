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
# Overrides: HERDR_PYTEST_WORKERS=<n>|auto|0 (0 = serial), HERDR_PYTEST_MAXPROCESSES=<cap, default 8>, HERDR_PYTEST_SLOW_CONCURRENT=0 (slow tail after, not beside, the fast pass). See
# scripts/ci/pytest-parallel.sh. The descriptor limit is per process, so each worker leaks only its share of the ~3,000.
#
# Hang protection (user rule: timeouts only kill hangs). Each pytest pass runs under scripts/ci/run-watched.py (a copy of the shared
# run-watched helper, extended: it tracks the command's session, parent-pid descendants and any process carrying its per-run token in its environment, which covers a Postgres started by pg_ctl; a hang or SIGTERM/SIGINT/SIGHUP stops that whole tree, and survivors left after the command exits get a bounded grace and are then stopped): after HERDR_PYTEST_SOFT_SECONDS (default 3600) of wall time it prints one notice and keeps going; it kills the
# pass (exit 124) only after HERDR_PYTEST_HANG_SECONDS (default 1800) with no output AND no CPU use anywhere in the pass. A slow but
# live run is never killed; a wedged one (pipeline 1362 sat ~8 h with no result) fails with a clear message. There is no flat cap.
#
# If the xdist machinery itself breaks (workers die, pytest exit 3) the parallel pass is rerun once on HERDR_PYTEST_RERUN_WORKERS workers
# (default 2, 0 = serial); a real test failure or a hang is never rerun. `auto` also shrinks on a loaded host (HERDR_LOADAVG_FILE).
#
# Progress (issue #937): pytest_ci_progress prints, every HERDR_PYTEST_PROGRESS_EVERY_SECONDS (60) / HERDR_PYTEST_PROGRESS_EVERY_TESTS
# (200), one line naming what each worker is inside - to the step log and to $HERDR_PYTEST_PROGRESS_DIR/progress.jsonl - and once past
# HERDR_PYTEST_PROGRESS_BUDGET_SECONDS (900 for a shard, 1800 unsharded; see scripts/ci/pytest-parallel.sh) repeats a `[ci-budget]` dump of
# the same view. It stops nothing: the budgets exist so a step the platform stops still says what it was inside. Pipeline 1978's shard 3
# was killed at 1950 s with no such line, and pipeline 1754's single-pass gate ran 6829 s; both logs are silent about the cause.
# Sharding: HERDR_UNIT_SHARD=<i>/<n> runs only shard i's test files (scripts/ci/unit-shard.py; one workflow per shard, see
# .woodpecker/unit-gates-shard-N.yml); the client tests run in shard 1.
# Tests marked `slow` are deselected from the parallel pass and run serially in a tail pass, concurrently with it (see pytest-parallel.sh).
set -euo pipefail
cd "$(dirname "$0")/../.."
. scripts/ci/journal.sh; ci_journal unit-gates

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3; need node; need git; need flock

. scripts/ci/ensure-fd-limit.sh; herdr_ensure_fd_limit || exit 1
. scripts/ci/host-facts.sh; ci_host_facts || true
. scripts/ci/pytest-parallel.sh
. scripts/ci/pytest-rerun.sh
herdr_pytest_report_load                 # --durations=40 and the per-file/per-worker time summary on both passes
herdr_pytest_progress_defaults || exit 1 # live per-worker progress + the pass's wall budget (#937); a bad override fails loudly
herdr_pytest_parallel_load || exit 1     # a bad override fails loudly instead of silently running serial
herdr_pytest_shard_load || exit 1        # HERDR_UNIT_SHARD=<i>/<n>: only this shard's test files (empty = the whole suite)

# --- dependency cache ----------------------------------------------------------------
# The exec agents get a private per-job /tmp; the shared path is the rw CI store mount /home/wohlgemuth/woodpecker/ci (host:
# /quobyte/metabolomicsgrp/it/ci/woodpecker; /quobyte inside the image is NOT mounted). Only trusted events (push/cron/manual
# on main) use it; pull_request pipelines get a private cold cache. See herdr_ci_cache_setup in scripts/ci/cache-trust.sh.
. scripts/ci/cache-trust.sh
herdr_ci_cache_setup unit-gates
cache_root="$HERDR_CI_CACHE_ROOT"; private_cache="$HERDR_CI_PRIVATE_CACHE"

# --- private venv per run (two jobs on one agent must not share one) -----------------
venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-unit-gates-venv.XXXXXX")"
cleanup() { rm -rf "$venv" "${private_cache:-/nonexistent-$$}"; }
trap cleanup EXIT

t0=$(date +%s); t1=$t0
python3 -m venv "$venv"
"$venv/bin/pip" install -q -e '.[dev,collab,review-gate,federation]'
export PATH="$venv/bin:$PATH"
t2=$(date +%s)

echo "== pytest =="
# CI agents are Slurm exec hosts with no docker daemon: the real `docker compose up` tests of the artifact host skip here
# (they fail, not skip, under CI otherwise) and run wherever docker exists; no pipeline has docker today.
export HERDR_SKIP_DOCKER=1
echo "pytest arguments: ${PYTEST_PARALLEL[*]:-(serial)}"
watched() { python3 scripts/ci/run-watched.py "${HERDR_PYTEST_SOFT_SECONDS:-3600}" "${HERDR_PYTEST_HANG_SECONDS:-1800}" -- "$@"; }
# The slow tail runs alongside the fast pass (herdr_pytest_run_gate in scripts/ci/pytest-rerun.sh), each under its own watchdog.
# Tail pass: pytest-timeout (3600 s on the slow test) must be able to fire and NAME a hung test before the watchdog kills the pass,
# so the no-progress limit here is above it (4200 s). Exit 5 (no slow tests collected) fails the gate on purpose: the slow tests
# disappearing silently would mean the coverage moved out of the gate; remove the marker and this pass together.
watched_slow() { HERDR_PYTEST_HANG_SECONDS="${HERDR_PYTEST_SLOW_HANG_SECONDS:-4200}" watched "$@"; }
herdr_pytest_run_gate watched watched_slow "$HERDR_PYTEST_FAST_MARKEXPR" "$HERDR_PYTEST_SLOW_MARKEXPR"
t3=$(date +%s)

# The client (node) tests are not python files, so no shard split covers them: they run in shard 1, or in an unsharded run.
if [ -z "${HERDR_UNIT_SHARD:-}" ] || [ "${HERDR_UNIT_SHARD%%/*}" = 1 ]; then
  echo "== client tests =="
  npm ci --no-audit --no-fund       # the component tests import playwright
  # Chromium is content-addressed under PLAYWRIGHT_BROWSERS_PATH; flock serializes concurrent first runs on the shared
  # cache so two pipelines never race the same download (a private cache has no contention). A no-op once the build is
  # present. Quobyte flock is advisory and its scope across clients is not guaranteed, so this narrows the race rather than
  # proving exclusion; the install is idempotent (content-addressed, GC disabled), so a lost race costs time, not correctness.
  flock "$cache_root/playwright-install.lock" npx playwright install chromium
  node --test --test-timeout=300000 packages/herdr-web-client/*.test.js packages/herdr-web-components/*.test.js packages/*.test.js
  # Per-module coverage gates (ported from the retired GitHub Actions workflows); also shard 1 only. scripts/ci/module-gates.sh.
  echo "== module gates =="
  bash scripts/ci/module-gates.sh
else
  echo "== client tests: run in shard 1 (this is shard $HERDR_UNIT_SHARD) =="
fi
t4=$(date +%s)

# Per-phase totals at the end of the step log: this is what the fast/full gate split (docs/operations/ui-gates.md
# budgets, wpci-timing elsewhere) is designed from.
echo "unit-gates timing: venv+deps $((t2-t1))s pytest $((t3-t2))s client $((t4-t3))s total $((t4-t0))s"
