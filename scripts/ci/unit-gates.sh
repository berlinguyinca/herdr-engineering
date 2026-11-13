#!/usr/bin/env bash
# The Python and client unit gates as CI runs them (.woodpecker/unit-gates.yml) and as anyone can run them from a clean checkout:
#   bash scripts/ci/unit-gates.sh
# Installs nothing system-wide (CI hosts have no apt and no sudo); needs Python 3.12+ with venv, Node 22 and git already on the host.
# ruff is not a gate yet: main has 122 existing findings (scripts/validate.sh runs it).
# It runs the whole suites, not a scoped subset: a scoped gate hides the breakage a change causes elsewhere.
#
# It raises the file-descriptor soft limit before running: the suite leaks roughly 3,000 descriptors over a full run, and a host
# with the common 1,024 soft limit dies about a third of the way through with no summary at all. See scripts/ci/ensure-fd-limit.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
. scripts/ci/journal.sh; ci_journal unit-gates

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need python3; need node; need git

. scripts/ci/ensure-fd-limit.sh; herdr_ensure_fd_limit || exit 1

venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-unit-gates-venv.XXXXXX")"
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install -q -e '.[dev,collab,review-gate,federation]'
export PATH="$venv/bin:$PATH"

echo "== pytest =="
# CI agents are Slurm exec hosts with no docker daemon: the real `docker compose up` tests of the artifact host skip here
# (they fail, not skip, under CI otherwise) and run wherever docker exists; no pipeline has docker today.
HERDR_SKIP_DOCKER=1 python -m pytest -q
echo "== client tests =="
npm ci --no-audit --no-fund       # the component tests import playwright
npx playwright install chromium     # one component test drives a real browser; a no-op when cached
node --test --test-timeout=300000 packages/herdr-web-client/*.test.js packages/herdr-web-components/*.test.js packages/*.test.js
