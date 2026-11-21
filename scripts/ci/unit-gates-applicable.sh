#!/bin/bash
# Decide whether this pipeline has to run the Python/client unit gates, and exit accordingly.
#
#     exit 0  -> applicable: run the suite
#     exit 3  -> not applicable: no covered path changed, so there is nothing for the suite to prove
#     other   -> a real error; the caller must fail the pipeline rather than skip
#
# Why this exists instead of Woodpecker's `when: {path: {include: [...]}}`: a path-filtered pipeline is never **created**, so on a
# docs-only PR the `ci/woodpecker/pr/unit-gates` context never reports at all. A context that never reports cannot be a required
# check - GitHub would wait forever - which is the only reason unit-gates is not required today, and why a PR whose unit-gates
# failed has been mergeable (issue #719: #709 merged red and main stayed red for five hours). Moving the decision here means the
# pipeline always exists and always reports a verdict; a docs-only PR pays a few seconds instead of nothing, and gets a green
# check that says why it was fast.
#
# Fail-open on purpose. Anything this script cannot work out - no merge base, a shallow fetch, an unrecognised event, a detached
# head it cannot resolve - is reported as **applicable**, so the suite runs. A guard that skips coverage because it is confused is
# worse than no guard: it turns a routing bug into a silent green.
set -euo pipefail

# One decision, two gates. `unit-gates-applicable.sh [unit|ui]` (default unit) selects which gate's covered paths are asked about;
# the ui-gates pipeline (.woodpecker/ui-gates.yml) had the same never-reports problem and uses `ui`. An unknown gate is an error,
# never a skip.
gate="${1:-unit}"
case "$gate" in
  unit)
    # Anything that can change what the suite proves. Deliberately a prefix match on directories, so a new module under
    # herdr_engineering/ or a new test file is covered without editing this list.
    COVERED_PREFIXES='herdr_engineering/
tests/
packages/
scripts/ci/
deploy/
lock/'
    COVERED_FILES='pyproject.toml
.woodpecker/unit-gates.yml
.woodpecker/unit-gates-shard-1.yml
.woodpecker/unit-gates-shard-2.yml
.woodpecker/unit-gates-shard-3.yml
.woodpecker/main-cron.yml
.woodpecker/unit-gates-main.yml
.woodpecker/structural-gates.yml
.woodpecker/artifact-handoff.yml
scripts/ci-pipeline-freshness.sh
scripts/ci/integrate-main.sh'
    ;;
  ui)
    # Anything that can change the browser gates' result: the old `when: path: include` list of ui-gates.yml, plus the routing
    # config and this script, which decide whether the gate runs at all.
    COVERED_PREFIXES='packages/
tests/playwright/
tests/visual/
docs/assets/screenshots/
herdr_engineering/control_plane/
herdr_artifact_host/member_static/
herdr_artifact_host/share_static/
schemas/
playwright.'
    COVERED_FILES='scripts/ci/ui-gates.sh
eslint.communications.config.mjs
eslint.scratch.config.mjs
tsconfig.communications.json
scripts/ci-pipeline-freshness.sh
package.json
package-lock.json
.woodpecker/ui-gates.yml
scripts/ci/unit-gates-applicable.sh
pyproject.toml
scripts/ci/journal.sh
scripts/ci/cache-trust.sh'
    ;;
  *) echo "applicability: unknown gate '${gate}' (expected unit or ui)" >&2; exit 2 ;;
esac
label="${gate}-gates"

herdr_covered_path() {
  local path="$path"
  while [ "${path#./}" != "$path" ]; do path="${path#./}"; done
  local prefix
  while IFS= read -r prefix; do
    [ -n "$prefix" ] && [ "${path#"$prefix"}" != "$path" ] && return 0
  done <<< "$COVERED_PREFIXES"
  local file
  while IFS= read -r file; do
    [ "$path" = "$file" ] && return 0
  done <<< "$COVERED_FILES"
  return 1
}

# Only a pull request can be "not applicable": a push to main is the thing main-cron-gates.sh exists to prove, and a manual run is
# someone asking for the suite by name. With no CI environment at all (a local run) the same question is asked against origin/main.
# Woodpecker exports CI_PIPELINE_EVENT (CI_EVENT is the legacy name); read both, like ci-pipeline-freshness.sh and cache-trust.sh.
event="${CI_PIPELINE_EVENT:-${CI_EVENT:-}}"
case "$event" in
  pull_request|'') ;;
  *) echo "${label} applicability: event '${event}' always runs the suite"; exit 0 ;;
esac

base_ref="${HERDR_APPLICABILITY_BASE:-origin/${CI_TARGET_BRANCH:-main}}"
head_ref="${CI_COMMIT_SHA:-HEAD}"

# The checkout step fetches only the pipeline commit, so the base has to be fetched before a merge base exists.
git rev-parse --verify -q "$base_ref" >/dev/null || git fetch -q --no-tags origin \
  "+refs/heads/${CI_TARGET_BRANCH:-main}:${base_ref}" 2>/dev/null || true
merge_base="$(git merge-base "$base_ref" "$head_ref" 2>/dev/null || true)"
if [ -z "$merge_base" ]; then
  echo "${label} applicability: no merge base against ${base_ref}; running the suite (fail open)" >&2
  exit 0
fi

changed="$(git diff --name-only "$merge_base" "$head_ref" 2>/dev/null)" || {
  echo "${label} applicability: could not diff ${merge_base}..${head_ref}; running the suite (fail open)" >&2
  exit 0
}

if [ -z "$changed" ]; then
  echo "${label} applicability: ${head_ref} is identical to the merge base; nothing to prove"
  exit 3
fi

while IFS= read -r path; do
  [ -n "$path" ] || continue
  if herdr_covered_path "$path"; then
    echo "${label} applicability: '${path}' is covered; running the suite"
    exit 0
  fi
done <<< "$changed"

echo "${label} applicability: no covered path in $(echo "$changed" | grep -c .) changed file(s); skipping the suite"
exit 3
