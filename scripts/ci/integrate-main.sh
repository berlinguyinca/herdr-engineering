#!/bin/bash
# Make a pull-request pipeline prove "PR + current main", not "PR as forked".
#
# Woodpecker checks out the PR head commit only. Branch protection `strict` ("must be up to date") used to force a rebase after
# every other merge so the green run meant something; with `strict` off, this step is what makes the green mean "this PR merged
# with the main that exists right now". It runs right after `checkout` (and after the freshness guard, which compares
# CI_COMMIT_SHA with the PR head ref and is unaffected: CI_COMMIT_SHA is never rewritten, only HEAD moves).
#
#     event is not pull_request          -> no-op, exit 0 (push, cron and manual run the commit as pushed)
#     main already contained in HEAD     -> no-op, exit 0
#     main merges cleanly                -> HEAD becomes a throwaway merge commit, exit 0
#     conflict (or main unreachable)     -> merge aborted, a message naming the files, exit 1
#
# The merge commit uses a one-shot identity (`git -c`); nothing is written to any git config. The merge honours .gitattributes, so
# CHANGELOG.md's `merge=union` driver applies here (it does not apply to GitHub's server-side merge button).
set -eu

[ "${CI_PIPELINE_EVENT:-${CI_EVENT:-}}" = "pull_request" ] || exit 0

target="${CI_COMMIT_TARGET_BRANCH:-${CI_TARGET_BRANCH:-main}}"
ref="refs/remotes/origin/${target}"

# A merge needs a merge base. unit-gates and structural-gates fetch full history; deepen anything shallower rather than fail with
# "refusing to merge unrelated histories".
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
  git fetch -q --no-tags --unshallow origin "$(git rev-parse HEAD)" \
    || git fetch -q --no-tags --unshallow origin \
    || { echo "integrate-main: could not deepen the shallow checkout; cannot merge ${target}" >&2; exit 1; }
fi

if ! git fetch -q --no-tags origin "+refs/heads/${target}:${ref}"; then
  echo "integrate-main: could not fetch origin ${target}; refusing to report a green for the PR alone" >&2
  exit 1
fi

if git merge-base --is-ancestor "$ref" HEAD; then
  echo "integrate-main: HEAD already contains origin/${target} ($(git rev-parse --short "$ref")); nothing to merge"
  exit 0
fi

if out="$(git -c user.name="woodpecker-integrate" -c user.email="woodpecker-integrate@localhost" -c commit.gpgsign=false \
    merge --no-edit "$ref" 2>&1)"; then
  echo "integrate-main: merged origin/${target} ($(git rev-parse --short "$ref")) into the PR head; testing $(git rev-parse --short HEAD)"
  exit 0
fi

files="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
git merge --abort 2>/dev/null || git reset -q --hard "${CI_COMMIT_SHA:-ORIG_HEAD}" 2>/dev/null || true
if [ -n "$files" ]; then
  echo "integrate-main: this pull request does not merge cleanly with ${target}." >&2
  echo "integrate-main: conflicting files:" >&2
  printf '%s\n' "$files" | sed 's/^/integrate-main:   /' >&2
else
  echo "integrate-main: merging ${target} into this pull request failed without a file conflict:" >&2
  printf '%s\n' "$out" | sed 's/^/integrate-main: /' >&2
fi
echo "integrate-main: rebase or merge main into your branch, resolve the conflicts and push." >&2
exit 1
