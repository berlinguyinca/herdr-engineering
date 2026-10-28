#!/usr/bin/env bash
# The Python and client unit gates as CI runs them (.woodpecker/unit-gates.yml) and as anyone can run them from a clean checkout:
#   bash scripts/ci/unit-gates.sh
# Installs nothing system-wide (CI hosts have no apt and no sudo); needs Python 3.12+ with venv, Node 22 and git already on the host.
# ruff is not a gate yet: main has 122 existing findings (scripts/validate.sh runs it).
# It runs the whole suites, not a scoped subset: a scoped gate hides the breakage a change causes elsewhere.
set -euo pipefail
cd "$(dirname "$0")/../.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3; need node; need git

venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-unit-gates-venv.XXXXXX")"
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install -q -e '.[dev]'
export PATH="$venv/bin:$PATH"

echo "== pytest =="
python -m pytest -q
echo "== client tests =="
node --test packages/herdr-web-client/*.test.js packages/herdr-web-components/*.test.js
