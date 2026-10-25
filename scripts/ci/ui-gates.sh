#!/usr/bin/env bash
# The UI gates as CI runs them (.woodpecker/ui-gates.yml) and as anyone can run them from a clean checkout:
#   bash scripts/ci/ui-gates.sh
# It installs nothing system-wide (the CI hosts have no apt and no sudo). It needs, already on the host: Node 22, Python 3.12 with venv,
# git, Chromium's shared libraries, and fontconfig with /etc/fonts/conf.d (docs/operations/ui-gates.md, "CI").
set -euo pipefail
cd "$(dirname "$0")/../.."
journal="${TMPDIR:-/tmp}/herdr-ui-gates-$$.log"
echo "journal: $journal"        # first, so a run is readable even when the agent drops the tail of the step log
exec > >(tee "$journal") 2>&1

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing on this host: $1" >&2; exit 1; }; }
need node; need python3; need git
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) { console.error("Node 22 or newer is required, found " + process.version); process.exit(1); }'
test -d /etc/fonts/conf.d || { echo "no /etc/fonts/conf.d: the screenshots depend on its rendering settings" >&2; exit 1; }

# a private venv per run: two jobs on one agent must not share (and race on) one fixed path
venv="$(mktemp -d "${TMPDIR:-/tmp}/herdr-ui-gates-venv.XXXXXX")"
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install -q -e '.[dev]'
export PATH="$venv/bin:$PATH"     # the fixture server starts with `python3`
npm ci --no-audit --no-fund
npx playwright install chromium         # the build the baselines were made with (Playwright 1.64); a no-op when cached
npm run test:ui-gates
npm run ui-gates:baselines:check
git diff --exit-code -- docs/assets/screenshots
