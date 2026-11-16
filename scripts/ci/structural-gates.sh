#!/usr/bin/env bash
# The structural gates as CI runs them (.woodpecker/structural-gates.yml) and as anyone can run them from a clean checkout:
#   bash scripts/ci/structural-gates.sh
# Seconds, not minutes: it parses every tracked data file and checks the memory pin against the lock. It is not a cheaper
# substitute for scripts/ci/unit-gates.sh, which runs whole suites on purpose; it answers the few questions whose failure has
# actually reached `main` (see the header of scripts/ci/structural_gates.py), and it runs on a push to `main`, which unit-gates
# deliberately does not.
# Needs python3 with venv; pyyaml is the only dependency, installed into a throwaway venv so nothing system-wide is touched.
set -euo pipefail
cd "$(dirname "$0")/../.."
. scripts/ci/journal.sh; ci_journal structural-gates

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3; need git

venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-structural-gates-venv.XXXXXX")"
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install -q pyyaml

echo "== structural gates =="
"$venv/bin/python" scripts/ci/structural_gates.py
