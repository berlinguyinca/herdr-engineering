#!/usr/bin/env bash
# Per-module coverage gates, ported from the retired GitHub Actions workflows (agent-communications, build-metrics, cargo-host,
# scratch-retention). Run by scripts/ci/unit-gates.sh in shard 1 (the required ci/woodpecker/pr/unit-gates context), inside its venv:
#   bash scripts/ci/module-gates.sh
# Each gate runs its module's tests under that module's own .coveragerc-* and then holds every file to 80% statements, branches and
# functions with scripts/ci/check-cargo-coverage.py. Thresholds are unchanged from the workflows. Linux only: the macOS legs (APFS
# volume, tests/test_scratch_swift_macos.py) are dropped, there is no macOS runner.
# Gates that need a real Rust toolchain are skipped LOUDLY when the host has none; the rest always run.
set -euo pipefail
cd "$(dirname "$0")/../.."

mkdir -p .superpowers
python3 -m pip install -q 'pytest-cov==7.0.0' 'coverage==7.13.1' 'build==1.3.0'
cov() {   # cov NAME RCFILE [pytest args...]: run pytest under RCFILE, then enforce the per-file thresholds
  local name="$1" rc="$2"; shift 2
  echo "== module gate: $name =="
  python3 -m pytest -q "$@" --cov --cov-config="$rc" --cov-report=term-missing --cov-report="json:.superpowers/$name-coverage.json"
  python3 scripts/ci/check-cargo-coverage.py ".superpowers/$name-coverage.json"
}

# Installed-wheel acceptance artifacts the scratch and communications tests look for.
python3 -m build --wheel -q
wheel="$(ls -1 "$PWD"/dist/herdr_engineering-*.whl | tail -n 1)"
export HERDR_SCRATCH_TEST_WHEEL="$wheel" HERDR_COMMUNICATION_TEST_WHEEL="$wheel"

# agent-communications: the generated client bundle must match its TypeScript source (needs node_modules from `npm ci`).
if [ -x node_modules/.bin/tsc ]; then
  bash scripts/build-communications.sh
  git diff --exit-code -- packages/communications-view.js
else
  echo "module gate: communications: node_modules missing, skipping the bundle drift check" >&2
fi
cov communications .coveragerc-communications -m 'not e2e' \
  tests/test_communications.py tests/control_plane/test_communications.py tests/control_plane/test_communications_postgres.py

# build-metrics, Linux leg: coverage is measured the way the workflow did, with `coverage run`, so subprocess patching applies.
echo "== module gate: build-metrics =="
export COVERAGE_RCFILE=.coveragerc-build-metrics
rm -f .coverage .coverage.*
python3 -m coverage run -m pytest -q -m 'not integration' tests/test_build_metrics.py tests/test_build_metrics_wait.py tests/test_build_coord.py tests/test_build_trace.py
python3 -m coverage run -m pytest -q tests/test_build_registry.py tests/test_build_contract_events.py
python3 -m coverage combine
python3 -m coverage report --fail-under=80
python3 -m coverage json -o .superpowers/build-metrics-coverage.json
python3 scripts/ci/check-cargo-coverage.py .superpowers/build-metrics-coverage.json
unset COVERAGE_RCFILE

if command -v cargo >/dev/null 2>&1 && command -v rustc >/dev/null 2>&1; then
  # scratch-retention: `-m ''` clears the default marker deselection, so the real-cargo integration test and the e2e run.
  cov scratch .coveragerc-scratch-reaper -m '' tests/test_scratch_reaper.py
  # cargo-host: real cargo tests, the e2e retention test last. Tests that need the APFS volume skip themselves on Linux.
  cov cargo-host .coveragerc-cargo-host -m 'not e2e' \
    tests/test_cargo_host.py tests/test_cargo_host_real.py tests/test_cargo_target_storage.py \
    tests/test_cargo_managed_targets_real.py tests/test_cargo_target_retention.py
  python3 -m pytest -m e2e -q tests/test_cargo_target_retention.py
else
  echo "module gate: NOTICE no cargo/rustc on this host: scratch-retention and cargo-host gates SKIPPED" >&2
fi
