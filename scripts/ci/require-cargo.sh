#!/usr/bin/env bash
# Decide whether module-gates.sh may run the Rust-toolchain gates (scratch-retention, cargo-host).
#   exit 0 -> cargo and rustc are on PATH: run them
#   exit 2 -> no toolchain on a local run, or CI with an explicit opt-out: skip, loudly
#   exit 1 -> no toolchain in CI: fail. A required gate must not report green because the runner lacked cargo.
# Opt-out for a runner known to have no Rust: HERDR_ALLOW_NO_CARGO=1 (logged every run).
set -euo pipefail
if command -v cargo >/dev/null 2>&1 && command -v rustc >/dev/null 2>&1; then exit 0; fi
if [ -n "${CI:-}${CI_PIPELINE_EVENT:-}" ] && [ "${HERDR_ALLOW_NO_CARGO:-}" != 1 ]; then
  echo "module gate: ERROR no cargo/rustc in CI: the scratch-retention and cargo-host gates cannot run. Install a Rust toolchain on the runner, or set HERDR_ALLOW_NO_CARGO=1 to skip them explicitly." >&2
  exit 1
fi
echo "module gate: NOTICE no cargo/rustc on this host (HERDR_ALLOW_NO_CARGO=${HERDR_ALLOW_NO_CARGO:-unset}, CI=${CI:-unset}): scratch-retention and cargo-host gates SKIPPED" >&2
exit 2
