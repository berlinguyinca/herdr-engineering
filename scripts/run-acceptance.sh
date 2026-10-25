#!/usr/bin/env bash
# T16: the one command for the end-to-end acceptance suite. Configuration lives in tests/e2e/acceptance.yaml
# (no environment variables). Fails on any failure, zero-test suite, short suite or unexplained skip.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/run_acceptance.py "$@"
