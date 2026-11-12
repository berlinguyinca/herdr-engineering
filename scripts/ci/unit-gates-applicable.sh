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

# Covered paths: anything that can change what the suite proves. Deliberately a prefix match on directories, so a new module under
# herdr_engineering/ or a new test file is covered without editing this list.
COVERED_PREFIXES='herdr_engineering/
tests/
packages/
scripts/ci/
deploy/
lock/'
COVERED_FILES='pyproject.toml
.woodpecker/unit-gates.yml
.woodpecker/main-cron.yml'

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
case "${CI_EVENT:-}" in
  pull_request|'') ;;
  *) echo "unit-gates applicability: event '${CI_EVENT}' always runs the suite"; exit 0 ;;
esac

base_ref="${HERDR_APPLICABILITY_BASE:-origin/${CI_TARGET_BRANCH:-main}}"
head_ref="${CI_COMMIT_SHA:-HEAD}"

# The checkout step fetches only the pipeline commit, so the base has to be fetched before a merge base exists.
git rev-parse --verify -q "$base_ref" >/dev/null || git fetch -q --no-tags origin \
  "+refs/heads/${CI_TARGET_BRANCH:-main}:${base_ref}" 2>/dev/null || true
merge_base="$(git merge-base "$base_ref" "$head_ref" 2>/dev/null || true)"
if [ -z "$merge_base" ]; then
  echo "unit-gates applicability: no merge base against ${base_ref}; running the suite (fail open)" >&2
  exit 0
fi

changed="$(git diff --name-only "$merge_base" "$head_ref" 2>/dev/null)" || {
  echo "unit-gates applicability: could not diff ${merge_base}..${head_ref}; running the suite (fail open)" >&2
  exit 0
}

if [ -z "$changed" ]; then
  echo "unit-gates applicability: ${head_ref} is identical to the merge base; nothing to prove"
  exit 3
fi

while IFS= read -r path; do
  [ -n "$path" ] || continue
  if herdr_covered_path "$path"; then
    echo "unit-gates applicability: '${path}' is covered; running the suite"
    exit 0
  fi
done <<< "$changed"

echo "unit-gates applicability: no covered path in $(echo "$changed" | grep -c .) changed file(s); skipping the suite"
exit 3
