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
python3 -m build --wheel
wheel="$(ls -1 "$PWD"/dist/herdr_engineering-*.whl | tail -n 1)"
export HERDR_SCRATCH_TEST_WHEEL="$wheel" HERDR_COMMUNICATION_TEST_WHEEL="$wheel"

# agent-communications: the generated client bundle must match its TypeScript source (needs node_modules from `npm ci`).
if [ -x node_modules/.bin/tsc ]; then
  bash scripts/build-communications.sh
  git diff --exit-code -- packages/communications-view.js
else
  echo "module gate: communications: node_modules missing, skipping the bundle drift check" >&2
fi
# The native-CLI tests need the real pinned HerdR binary (sha256-checked, as the retired workflow did) and a running server. The
# server is isolated: its own HERDR_HOME and a socket private to this run, so no test can reach a real agent.
herdr_sha=2a02fed16beb651ef006e1d43f048f652ca4dc58ad053cd2d44450563d5c54b7
if [ "$(uname -m)" = x86_64 ]; then
  herdr_dir="$PWD/.superpowers/herdr-native"
  mkdir -p "$herdr_dir"
  if ! echo "$herdr_sha  $herdr_dir/herdr" | sha256sum -c --status - 2>/dev/null; then
    curl -fsSL --retry 3 https://github.com/herdrdev/herdr/releases/download/v0.9.1/herdr-linux-x86_64 -o "$herdr_dir/herdr"
    echo "$herdr_sha  $herdr_dir/herdr" | sha256sum -c -
  fi
  chmod +x "$herdr_dir/herdr"
  runtime_base="${XDG_RUNTIME_DIR:-}"
  [ -n "$runtime_base" ] && [ -d "$runtime_base" ] && [ -w "$runtime_base" ] || runtime_base="$PWD/.superpowers"
  herdr_run="$(mktemp -d "$runtime_base/hmg.XXXXXX")"
  # HERDR_HOME alone does not isolate: the server reads and rewrites $XDG_CONFIG_HOME/herdr/session.json (restoring the real
  # user's agents), so the server and the tests get a private HOME and XDG dirs; only HERDR_SOCKET_PATH is shared with them.
  herdr_home="$herdr_run/h"
  mkdir -p "$herdr_home"
  herdr_env=(HOME="$herdr_home" XDG_CONFIG_HOME="$herdr_home/.config" XDG_STATE_HOME="$herdr_home/.local/state"
    XDG_DATA_HOME="$herdr_home/.local/share" XDG_CACHE_HOME="$herdr_home/.cache" HERDR_SOCKET_PATH="$herdr_run/s.sock")
  export PATH="$herdr_dir:$PATH"
  env "${herdr_env[@]}" "$herdr_dir/herdr" server > "$herdr_run/server.log" 2>&1 &
  herdr_pid=$!
  trap 'kill "$herdr_pid" 2>/dev/null || true; wait "$herdr_pid" 2>/dev/null || true; rm -rf -- "$herdr_run"' EXIT
  for _ in $(seq 1 20); do env "${herdr_env[@]}" herdr agent list >/dev/null 2>&1 && break; sleep 0.5; done
  env "${herdr_env[@]}" herdr agent list >/dev/null || { cat "$herdr_run/server.log" >&2; exit 1; }
  # The isolated server must start empty; a non-empty list means it reached a real session.
  [ "$(env "${herdr_env[@]}" herdr agent list | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["agents"]))')" = 0 ] \
    || { echo "module gate: isolated HerdR server is not empty; refusing to run native-CLI tests" >&2; exit 1; }
else
  echo "module gate: NOTICE $(uname -m) has no pinned HerdR binary: native-CLI communications tests will skip" >&2
  herdr_env=()
fi
# env needs at least one assignment when herdr_env is empty; PATH is a harmless one.
env PATH="$PATH" "${herdr_env[@]}" bash -c "$(declare -f cov); cov communications .coveragerc-communications -m 'not e2e' \
  tests/test_communications.py tests/control_plane/test_communications.py tests/control_plane/test_communications_postgres.py"

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
  # cargo-host needs a real sccache for the run_rustc cache path; CI has none, so fetch a pinned official release.
  cargo_host_path="$PATH"
  if ! command -v sccache >/dev/null 2>&1; then
    sccache_version=v0.19.0
    sccache_sha256=aa73c7270c997894038c0d8f0d297865f57569e1687883322bc431713d4bb9dd   # x86_64-unknown-linux-musl tarball
    sccache_dir="sccache-$sccache_version-x86_64-unknown-linux-musl"
    if [ "$(uname -sm)" = "Linux x86_64" ]; then
      mkdir -p .superpowers/bin .superpowers/sccache-dl
      curl -fsSL -o ".superpowers/sccache-dl/$sccache_dir.tar.gz" \
        "https://github.com/mozilla/sccache/releases/download/$sccache_version/$sccache_dir.tar.gz"
      echo "$sccache_sha256  .superpowers/sccache-dl/$sccache_dir.tar.gz" | sha256sum -c -
      tar -xzf ".superpowers/sccache-dl/$sccache_dir.tar.gz" -C .superpowers/sccache-dl "$sccache_dir/sccache"
      install -m 0755 ".superpowers/sccache-dl/$sccache_dir/sccache" .superpowers/bin/sccache
      cargo_host_path="$PWD/.superpowers/bin:$PATH"
    else
      echo "module gate: NOTICE no sccache and no pinned build for $(uname -sm): sccache-dependent tests skip" >&2
    fi
  fi
  # cargo-host: real cargo tests, the e2e retention test last. Tests that need the APFS volume skip themselves on Linux.
  PATH="$cargo_host_path" cov cargo-host .coveragerc-cargo-host -m 'not e2e' \
    tests/test_cargo_host.py tests/test_cargo_host_real.py tests/test_cargo_target_storage.py \
    tests/test_cargo_managed_targets_real.py tests/test_cargo_target_retention.py
  PATH="$cargo_host_path" python3 -m pytest -m e2e -q tests/test_cargo_target_retention.py
else
  echo "module gate: NOTICE no cargo/rustc on this host: scratch-retention and cargo-host gates SKIPPED" >&2
fi
