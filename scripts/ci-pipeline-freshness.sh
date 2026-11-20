#!/bin/bash
# Stop a pipeline whose commit has been superseded on its pull request.
#
# .woodpecker/unit-gates.yml and .woodpecker/ui-gates.yml declare
#     concurrency: {limit: 1, group: "${CI_WORKFLOW_NAME}-${CI_COMMIT_SOURCE_BRANCH}"}
# so a new commit on a branch queues its pipeline instead of running it on a
# second agent beside the older one. Without this guard, every superseded
# pipeline that reaches the queue would still spend the whole gate (a full
# venv + the entire test suite) on a commit that is no longer the head of its
# pull request. The guard compares this pipeline's commit against the current
# head of the PR: refs/pull/N/head (it exists for fork PRs too), falling back
# to the source branch. Woodpecker checks out the PR head commit, not a merge
# with main. Only source-branch variables are used: CI_COMMIT_BRANCH is the
# TARGET branch (main) for a pull_request pipeline.
# A superseded pull-request pipeline exits 1 here, before any install work; the newer
# pipeline is the one that runs the gate.
#
# Every other event (push, cron, manual) always runs: re-running an old commit
# by hand is how a pipeline is debugged, and push/cron pipelines run on main.
#
# Fail open on anything unexpected (missing variables, no remote, empty
# answer): a guard that cannot tell must not stop a pipeline that is the
# newest on its branch.
set -eu

event="${CI_PIPELINE_EVENT:-${CI_EVENT:-}}"

# A push to main is superseded the same way: .woodpecker/unit-gates-main.yml queues one run at a time (concurrency group
# unit-gates-main). A run whose commit is no longer the head of origin/main prints "superseded by newer push", writes the marker
# file .ci-main-superseded and exits 0 (NOT 1): a red pipeline on main shows up as a critical "CI failed - main" in the attention
# inbox until the newer head's run finishes (~45 min), although nothing is wrong. The later unit-gates step sees the marker and
# skips, and never runs `--record-green`, so a superseded commit is never recorded as proven. A superseded pull_request run still
# exits 1 (below). Pushes to any other branch, cron and manual runs always run.
if [ "$event" = "push" ] && [ "${CI_COMMIT_BRANCH:-}" = "main" ]; then
  rm -f .ci-main-superseded
  commit="${CI_COMMIT_SHA:-}"
  [ -n "$commit" ] || exit 0
  head="$(git ls-remote origin refs/heads/main 2>/dev/null | awk '{print $1}' || true)"
  if [ -z "$head" ]; then
    echo "freshness guard: could not read the head of refs/heads/main; running the pipeline" >&2
    exit 0
  fi
  if [ "$head" != "$commit" ]; then
    echo "superseded by newer push: commit ${commit} is no longer the head of refs/heads/main (now ${head}); the newer pipeline runs the gate"
    touch .ci-main-superseded
  fi
  exit 0
fi

[ "$event" = "pull_request" ] || exit 0

pr="${CI_COMMIT_PULL_REQUEST:-}"
branch="${CI_COMMIT_SOURCE_BRANCH:-}"
if [ -n "$pr" ]; then ref="refs/pull/${pr}/head"
elif [ -n "$branch" ]; then ref="refs/heads/${branch}"
else exit 0
fi

# The checkout step created `origin` in this workspace.
head="$(git ls-remote origin "$ref" 2>/dev/null | awk '{print $1}' || true)"
if [ -z "$head" ] && [ -n "$branch" ] && [ "$ref" != "refs/heads/${branch}" ]; then
  ref="refs/heads/${branch}"
  head="$(git ls-remote origin "$ref" 2>/dev/null | awk '{print $1}' || true)"
fi
if [ -z "$head" ]; then
  echo "freshness guard: could not read the head of ${ref}; running the pipeline" >&2
  exit 0
fi

commit="${CI_COMMIT_SHA:-}"
[ -n "$commit" ] || exit 0

if [ "$head" != "$commit" ]; then
  echo "superseded: commit ${commit} is no longer the head of ${ref} (now ${head}); the newer pipeline runs the gate"
  exit 1
fi
