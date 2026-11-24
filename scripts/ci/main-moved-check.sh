#!/bin/bash
# Mid-run freshness re-check for a pull-request unit-gates shard: has main moved since integrate-main merged it?
#
# A shard runs for ~10 minutes after scripts/ci/integrate-main.sh merged `origin/main`; main takes dozens of merges a day, so by the
# time the shard has a verdict main has often moved. This step runs after the tests passed and costs one `git fetch`:
#
#     not a pull request                          -> exit 0 (push, cron, manual: the commit as pushed is the thing under test)
#     origin/main is what integrate-main merged   -> exit 0, "main unchanged" (the common cheap case)
#     main moved, no covered path changed         -> exit 0, says so (the unit-gates-applicable.sh path list decides "covered")
#     main moved in covered paths, merges cleanly -> exit 0 with a WARNING by default: the suite did not see those commits. The design
#                                                    (strict off, docs/ci-and-reviews.md) accepts that: the push-to-main run
#                                                    (.woodpecker/unit-gates-main.yml) proves the combination on main. Re-running the
#                                                    suite here would restart a ~10 min race against the next merge, forever.
#                                                    HERDR_MAIN_MOVED_POLICY=fail turns this into exit 1 for a repo that wants the strict reading.
#     main moved and no longer merges cleanly     -> exit 1 always, naming the conflicting files: the green was for a merge that can
#                                                    no longer be made.
#     origin/main cannot be fetched               -> exit 1 (a green that cannot be re-validated is not reported)
#
# The merge is tried with `git merge-tree --write-tree` (git 2.38+): nothing in the worktree or index changes.
set -eu

[ "${CI_PIPELINE_EVENT:-${CI_EVENT:-}}" = "pull_request" ] || { echo "main-moved-check: not a pull request; nothing to re-check"; exit 0; }

policy="${HERDR_MAIN_MOVED_POLICY:-warn}"
case "$policy" in warn|fail) ;; *) echo "main-moved-check: HERDR_MAIN_MOVED_POLICY must be warn or fail, got '$policy'" >&2; exit 2 ;; esac

target="${CI_COMMIT_TARGET_BRANCH:-${CI_TARGET_BRANCH:-main}}"
old_ref="refs/remotes/origin/${target}"
new_ref="refs/remotes/origin/${target}-recheck"
here="$(cd "$(dirname "$0")" && pwd)"

old="$(git rev-parse --verify -q "$old_ref" || true)"
[ -n "$old" ] || { echo "main-moved-check: no ${old_ref} in this workspace (integrate-main did not run); cannot re-check" >&2; exit 1; }

if ! git fetch -q --no-tags origin "+refs/heads/${target}:${new_ref}"; then
  echo "main-moved-check: could not fetch origin ${target}; refusing to report a green that cannot be re-validated" >&2
  exit 1
fi
new="$(git rev-parse "$new_ref")"
if [ "$new" = "$old" ]; then
  echo "main-moved-check: main unchanged since integrate-main ($(git rev-parse --short "$old")); the verdict stands"
  exit 0
fi

echo "main-moved-check: ${target} moved while the tests ran: $(git rev-parse --short "$old") -> $(git rev-parse --short "$new")"

# The merge that was tested (HEAD) against the new main. Clean or not, exit status 1 of merge-tree means conflicts.
if ! out="$(git merge-tree --write-tree --name-only HEAD "$new" 2>&1)"; then
  files="$(printf '%s\n' "$out" | sed '1d' | sed '/^$/,$d')"      # line 1 is the tree id, then the conflicted paths up to a blank line
  echo "main-moved-check: this pull request no longer merges cleanly with ${target} ($(git rev-parse --short "$new"))." >&2
  [ -n "$files" ] && { echo "main-moved-check: conflicting files:" >&2; printf '%s\n' "$files" | sed 's/^/main-moved-check:   /' >&2; }
  echo "main-moved-check: rebase or merge ${target} into your branch, resolve the conflicts and push." >&2
  exit 1
fi

# Did the commits main gained touch anything the suite covers? Reuse the applicability decision: base = the old main, head = the new one.
rc=0
HERDR_APPLICABILITY_BASE="$old" CI_COMMIT_SHA="$new" bash "$here/unit-gates-applicable.sh" unit || rc=$?
case "$rc" in
  3) echo "main-moved-check: no covered path changed on ${target}; the verdict stands"; exit 0 ;;
  0) ;;
  *) echo "main-moved-check: could not decide whether the new commits are covered (exit $rc); treating them as covered" ;;
esac

if [ "$policy" = fail ]; then
  echo "main-moved-check: main moved in covered paths while the suite ran, and HERDR_MAIN_MOVED_POLICY=fail: re-run this pipeline to test PR + the new main" >&2
  exit 1
fi
echo "main-moved-check: WARNING: ${target} gained covered changes after integrate-main; this green is for the PR plus ${target} at $(git rev-parse --short "$old"). The merge with the new main is clean, and the push-to-main run (unit-gates-main) proves the combination once it lands."
exit 0
