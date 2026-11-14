#!/bin/bash
# Cron proof that `main` is green (the pipeline: .woodpecker/main-cron.yml).
#
# `main` had no test coverage at all: unit-gates and ui-gates trigger on pull_request only, so the only pipeline that fires on a
# push to main is structural-gates, a 1.3s check of YAML/merge/pin that never runs pytest. #709 landed four deterministic test
# failures at 23:52 and nothing on main noticed for five hours (issue #719). This closes that without paying a full suite per push:
# the schedule decides *when to look*, and this script decides *whether there is anything new to prove*.
#
#     same commit as the last green run  -> exit 0 in about a second, no install, no suite
#     a commit that has never been proven -> run the whole gate, and record the commit only on success
#
# The memo lives in the shared CI store, the one path the exec agents mount read-write into every job. If it is not writable
# (a dev host, a docker agent, a first run) the script cannot tell, so it runs the suite - the same fail-open rule as
# unit-gates-applicable.sh: a guard that cannot read state must not hand back a green.
#
# Override the memo location with HERDR_CI_STATE for a dev host or a test.
set -euo pipefail
cd "$(dirname "$0")/../.."

state_dir="${HERDR_CI_STATE:-/home/user/woodpecker/ci/state/herdr}"
memo="${state_dir}/main-unit-gates-green.txt"
head_sha="$(git rev-parse HEAD)"

if [ -f "$memo" ] && [ "$(cat "$memo" 2>/dev/null)" = "$head_sha" ]; then
  echo "main-cron: ${head_sha:0:12} is already proven green; nothing to do"
  exit 0
fi

if ! mkdir -p "$state_dir" 2>/dev/null || [ ! -w "$state_dir" ]; then
  echo "main-cron: memo path ${state_dir} is not writable; cannot tell what is already proven, so running the full gate" >&2
  memo=""
fi

echo "main-cron: proving ${head_sha:0:12} on main"
bash scripts/ci/unit-gates.sh

# Only a green run earns the memo, and only when there is somewhere to write it.
if [ -n "$memo" ]; then
  printf '%s\n' "$head_sha" > "${memo}.tmp" && mv -f "${memo}.tmp" "$memo"
  echo "main-cron: recorded ${head_sha:0:12} as proven green"
fi
