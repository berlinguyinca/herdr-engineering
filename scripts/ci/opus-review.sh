#!/usr/bin/env bash
# The Woodpecker step for the per-PR `Opus agent review` status. Installed once on the review host, called by the tiny
# per-repo .woodpecker/opus-review.yaml, so the logic lives here and not in every repository. Operator guide:
# docs/operations/per-pr-opus-review.md.
#
#   herdr-opus-review [--config FILE]       reads CI_PIPELINE_EVENT, CI_REPO, CI_COMMIT_PULL_REQUEST and the secret
#                                            OPUS_REVIEW_GITHUB_TOKEN, which Woodpecker sets; nothing else is configured here
#
# Install:  ln -s <checkout>/scripts/ci/opus-review.sh ~/.local/bin/herdr-opus-review   (the script finds
# deploy/ci-review/always-on-review.py relative to the link's target, never the working directory, which is the PR's).
#
# What it does, in order: skips anything but a pull_request event; refuses a malformed repo or PR number (exit 2); skips,
# successfully, when Woodpecker injected no token (a fork's pipeline gets no secrets); skips a PR whose head is in a fork;
# waits for the host's one review slot; then runs `always-on-review.py review --repo "$CI_REPO" --pr N --post`, which posts
# the status on the PR head SHA. Exit codes of the review: 0 done, 4 another run holds the PR or the head is already
# reviewed, 3 the head moved (the new push triggers its own review): all three exit 0. 1 (failed) and 2 (refused) are kept:
# the status stays pending and the pipeline goes red.
set -euo pipefail

say() { printf 'opus-review: %s\n' "$*" >&2; }
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
script="$here/../../deploy/ci-review/always-on-review.py"
config="$HOME/.config/herdr-engineering/always-on-review.yaml"
while [ $# -gt 0 ]; do
  case "$1" in
    --config) [ $# -ge 2 ] || { say "--config needs a file"; exit 2; }; config=$2; shift 2 ;;
    *) say "unknown argument: $1"; exit 2 ;;
  esac
done

if [ "${CI_PIPELINE_EVENT:-}" != "pull_request" ]; then
  say "event '${CI_PIPELINE_EVENT:-}' is not a pull request; nothing to review"
  exit 0
fi
repo=${CI_REPO:-}
pr=${CI_COMMIT_PULL_REQUEST:-}
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && "$repo" != -* ]] || { say "CI_REPO is not OWNER/NAME"; exit 2; }
[[ "$pr" =~ ^[0-9]+$ ]] || { say "CI_COMMIT_PULL_REQUEST is not a pull request number"; exit 2; }
[ -r "$script" ] || { say "review script not found next to this install: $script"; exit 2; }
[ -r "$config" ] || { say "config not readable: $config"; exit 2; }

# The one credential: the status token. An ambient GitHub token on the agent is never used instead of it.
token=${OPUS_REVIEW_GITHUB_TOKEN:-}
unset OPUS_REVIEW_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN
if [ -z "$token" ]; then
  say "no opus_review_github_token was injected (a fork's pipeline gets no secrets); not reviewing $repo#$pr"
  exit 0
fi
export GH_TOKEN=$token

# Defense in depth for forks: even with the secret present, only a PR whose head branch is in this repository is read.
cross=$(gh pr view "$pr" --repo "$repo" --json isCrossRepository) || { say "cannot read $repo#$pr; nothing reviewed"; exit 1; }
if [[ "$cross" =~ \"isCrossRepository\":[[:space:]]*true ]]; then
  say "$repo#$pr comes from a fork; not reviewing it (no code from a fork runs with the token)"
  exit 0
fi

# One review at a time per host, across repos and across this and the timer sweep's reviews (the sweep also holds the
# per-PR lock, so it never reviews the same head twice).
lock_dir="${XDG_STATE_HOME:-$HOME/.local/state}/herdr-review"
mkdir -p "$lock_dir"
exec 9>"$lock_dir/opus-review-host.lock"
flock -w 10800 9 || { say "waited 3 h for the host's review slot; giving up"; exit 1; }

rc=0
python3 "$script" review --repo "$repo" --pr "$pr" --config "$config" --post || rc=$?
case "$rc" in
  0) ;;
  3) say "the head moved during the review; the new push triggers its own"; rc=0 ;;
  4) say "another run holds $repo#$pr or this head is already reviewed"; rc=0 ;;
esac
exit "$rc"
