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

# Phase budgets. A healthy run is ~11 min for the suite and ~1 min for the baseline check, so a phase that blows through 2x that is a
# regression in the harness (the 2026-10-28 networkidle wait made every capture ~10 s slower: 11 min -> 49 min and never failed), not a
# slow host. Fail fast with a message that names the phase instead of letting the step run for the pipeline timeout.
suite_budget="${UI_GATES_SUITE_BUDGET_SECONDS:-1500}"
baseline_budget="${UI_GATES_BASELINES_BUDGET_SECONDS:-300}"
phase() {   # phase NAME BUDGET_SECONDS COMMAND...
  local name="$1" budget="$2" started=$SECONDS rc=0
  shift 2
  timeout -k 10 "$budget" "$@" || rc=$?
  if [ "$rc" -eq 124 ]; then
    echo "ui-gates: $name exceeded its ${budget}s budget; this is a harness slowdown to find, not a slow host (docs/operations/ui-gates.md, CI)" >&2
  fi
  echo "ui-gates: $name took $((SECONDS - started))s (budget ${budget}s)"
  return "$rc"
}

# Woodpecker checks out the PR head commit, not a merge with main, so this run measures the branch as forked. If main has since changed
# what runs here (a fix to the harness), a red or slow run may only mean "stale branch": say so, and say which files (merge main into
# the branch and push to re-run; Woodpecker does not re-run a pipeline when only main moves). Non-fatal; skipped when history is short.
stale_base_notice() {
  local fork files
  git fetch -q --no-tags --depth=200 origin main 2>/dev/null || return 0
  fork=$(git merge-base HEAD FETCH_HEAD 2>/dev/null) || return 0
  files=$(git diff --name-only "$fork" FETCH_HEAD -- .woodpecker scripts/ci tests/playwright/ui-gates-lib.mjs tests/visual package.json 2>/dev/null) || return 0
  [ -z "$files" ] && return 0
  echo "ui-gates: NOTICE main changed the harness since this branch forked; merge main and push if this run fails or is slow:" >&2
  echo "$files" | sed 's/^/ui-gates:   /' >&2
}
stale_base_notice

phase "test:ui-gates" "$suite_budget" npm run test:ui-gates
phase "baselines:check" "$baseline_budget" npm run ui-gates:baselines:check
git diff --exit-code -- docs/assets/screenshots
