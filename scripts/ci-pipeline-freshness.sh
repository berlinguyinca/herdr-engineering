#!/bin/bash
# Stop a pipeline whose commit has been superseded on its branch.
#
# .woodpecker/unit-gates.yml and .woodpecker/ui-gates.yml declare
#     concurrency: {limit: 1, group: "{{branch}}"}
# so a new commit on a branch queues its pipeline instead of running it on a
# second agent beside the older one. Without this guard, every superseded
# pipeline that reaches the queue would still spend the whole gate (a full
# venv + the entire test suite) on a commit that is no longer the head of its
# branch. The guard compares this pipeline's commit against the current head:
#   push          -> head of main (push pipelines exist only on main)
#   pull_request  -> head of the PR's source branch (Woodpecker checks out the
#                    PR head commit, not a merge with main)
# A superseded pipeline exits 1 here, before any install work; the newer
# pipeline is the one that runs the gate.
#
# Manual runs (and any event this script does not recognise) always run:
# re-running an old commit by hand is how a pipeline is debugged.
#
# Fail open on anything unexpected (missing variables, no remote, empty
# answer): a guard that cannot tell must not stop a pipeline that is the
# newest on its branch.
set -eu

case "${CI_EVENT:-}" in
  push|pull_request) ;;
  *) exit 0 ;;
esac

if [ "$CI_EVENT" = "push" ]; then
  branch="main"
else
  branch="${CI_BRANCH:-${CI_COMMIT_BRANCH:-}}"
fi
[ -n "$branch" ] || exit 0

# The checkout step created `origin` in this workspace.
head="$(git ls-remote origin "refs/heads/${branch}" 2>/dev/null | awk '{print $1}' || true)"
if [ -z "$head" ]; then
  echo "freshness guard: could not read the head of ${branch}; running the pipeline" >&2
  exit 0
fi

commit="${CI_COMMIT_SHA:-}"
[ -n "$commit" ] || exit 0

if [ "$head" != "$commit" ]; then
  echo "superseded: commit ${commit} is no longer the head of ${branch} (now ${head}); the newer pipeline runs the gate"
  exit 1
fi
