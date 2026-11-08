#!/usr/bin/env bash
# The Python and client unit gates as CI runs them (.woodpecker/unit-gates.yml) and as anyone can run them from a clean checkout:
#   bash scripts/ci/unit-gates.sh
# Installs nothing system-wide (CI hosts have no apt and no sudo); needs Python 3.12+ with venv, Node 22 and git already on the host.
# ruff is not a gate yet: main has 122 existing findings (scripts/validate.sh runs it).
# It runs the whole suites, not a scoped subset: a scoped gate hides the breakage a change causes elsewhere.
set -euo pipefail
cd "$(dirname "$0")/../.."
. scripts/ci/journal.sh; ci_journal unit-gates

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3; need node; need git

# --- shared dependency cache ---------------------------------------------------------
# The exec agents get a private per-job /tmp, so the venv wheels, the npm tree and Chromium used to be paid on every
# single run (~5-10 min of the ~45 min gate). The one shared path the agent mounts rw into every job is the CI store
# mount /home/wohlgemuth/woodpecker/ci (host: /quobyte/metabolomicsgrp/it/ci/woodpecker); /quobyte inside the image
# is NOT mounted and must not be used. The caches here are content-addressed (pip http cache, npm cacache, playwright
# browser dir) and safe to share between concurrent pipelines. When the path is not writable (docker agents, a dev
# host) the script falls back to a private per-job dir and keeps working, just cold. Override with HERDR_CI_CACHE.
cache_root="${HERDR_CI_CACHE:-/home/wohlgemuth/woodpecker/ci/cache/herdr}"
cache_root_fallback=""
if ! mkdir -p "$cache_root" 2>/dev/null || [ ! -w "$cache_root" ]; then
  cache_root_fallback="$(mktemp -d "${TMPDIR:-/tmp}/herdr-ci-cache.XXXXXX")"
  cache_root="$cache_root_fallback"
  echo "unit-gates: shared cache not writable; using private $cache_root (cold)" >&2
fi
export PIP_CACHE_DIR="$cache_root/pip"
export npm_config_cache="$cache_root/npm"
export PLAYWRIGHT_BROWSERS_PATH="$cache_root/playwright"
mkdir -p "$PIP_CACHE_DIR" "$npm_config_cache" "$PLAYWRIGHT_BROWSERS_PATH"

# --- private venv per run (two jobs on one agent must not share one) -----------------
venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-unit-gates-venv.XXXXXX")"
cleanup() { rm -rf "$venv" "${cache_root_fallback:-/nonexistent-$$}"; }
trap cleanup EXIT

t0=$(date +%s); t1=$t0
python3 -m venv "$venv"
"$venv/bin/pip" install -q -e '.[dev,collab,review-gate,federation]'
export PATH="$venv/bin:$PATH"
t2=$(date +%s)

echo "== pytest =="
python -m pytest -q
t3=$(date +%s)

echo "== client tests =="
npm ci --no-audit --no-fund       # the component tests import playwright
# Chromium is content-addressed under PLAYWRIGHT_BROWSERS_PATH; flock serializes concurrent first runs on the shared
# cache so two pipelines never race the same download. A no-op once the build is present.
flock "$cache_root/playwright-install.lock" npx playwright install chromium
node --test --test-timeout=300000 packages/herdr-web-client/*.test.js packages/herdr-web-components/*.test.js packages/*.test.js
t4=$(date +%s)

# Per-phase totals at the end of the step log: this is what the fast/full gate split (docs/operations/ui-gates.md
# budgets, wpci-timing elsewhere) is designed from.
echo "unit-gates timing: venv+deps $((t2-t1))s pytest $((t3-t2))s client $((t4-t3))s total $((t4-t0))s"
