#!/usr/bin/env bash
# The Woodpecker step for the per-PR `Opus agent review` status. Installed once on the review host, called by the tiny
# per-repo .woodpecker/opus-review.yaml, so the logic lives here and not in every repository. Operator guide:
# docs/operations/per-pr-opus-review.md.
#
#   herdr-opus-review [--config FILE] [--home DIR] [--lock-wait SECONDS]
#
# Reads CI_PIPELINE_EVENT, CI_REPO, CI_COMMIT_PULL_REQUEST and the secret OPUS_REVIEW_GITHUB_TOKEN, which Woodpecker sets.
#
# Install (once, as the user that owns the Claude login):
#   sudo ln -s <checkout>/scripts/ci/opus-review.sh /usr/local/bin/herdr-opus-review
# The script finds deploy/ci-review/always-on-review.py relative to the link's target, never the working directory.
#
# HOME is not trusted: the Woodpecker local backend replaces it with a throwaway directory
# (WOODPECKER_BACKEND_LOCAL_ISOLATED_HOME, on by default), so the config, the gh login and the Claude login would not
# be found. The real home is taken from the passwd entry of the user the agent runs as (--home overrides it).
#
# What it does, in order: skips an event that is not a pull request event; refuses a malformed repo or PR number (exit 2)
# and an empty token (exit 1); skips a PR whose head is in a fork (`gh pr view --jq .isCrossRepository` must say exactly
# `false` to continue; anything else is refused); then runs `always-on-review.py review --repo "$CI_REPO" --pr N --post`,
# which waits for the host's one review slot (at most --lock-wait seconds, default 1800: keep it below the pipeline
# timeout) and posts the status on the PR head SHA. Review exit 3 (head moved) and 4 (nothing to do, or the head is
# already reviewed) become 0; the cases behind a 4 are listed in the docs. 1 (failed) and 2 (refused) are kept.
set -euo pipefail

say() { printf 'opus-review: %s\n' "$*" >&2; }
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
script="$here/../../deploy/ci-review/always-on-review.py"
home=""
config=""
lock_wait=1800
while [ $# -gt 0 ]; do
  case "$1" in
    --config) [ $# -ge 2 ] || { say "--config needs a file"; exit 2; }; config=$2; shift 2 ;;
    --home) [ $# -ge 2 ] || { say "--home needs a directory"; exit 2; }; home=$2; shift 2 ;;
    --lock-wait) [ $# -ge 2 ] || { say "--lock-wait needs seconds"; exit 2; }; lock_wait=$2; shift 2 ;;
    *) say "unknown argument: $1"; exit 2 ;;
  esac
done
[[ "$lock_wait" =~ ^[0-9]+$ ]] || { say "--lock-wait is not a number of seconds"; exit 2; }

case "${CI_PIPELINE_EVENT:-}" in
  pull_request|pull_request_metadata) ;;
  *) say "event '${CI_PIPELINE_EVENT:-}' is not a pull request event; nothing to review"; exit 0 ;;
esac
repo=${CI_REPO:-}
pr=${CI_COMMIT_PULL_REQUEST:-}
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && "$repo" != -* ]] || { say "CI_REPO is not OWNER/NAME"; exit 2; }
[[ "$pr" =~ ^[0-9]+$ ]] || { say "CI_COMMIT_PULL_REQUEST is not a pull request number"; exit 2; }

# The real home, not the isolated one the local backend sets in $HOME. Every other login-dependent path follows from it.
if [ -z "$home" ]; then
  home=$(getent passwd "$(id -un)" | cut -d: -f6) || home=""
fi
[ -n "$home" ] && [ -d "$home" ] || { say "cannot determine the home directory of $(id -un)"; exit 2; }
export HOME=$home
unset XDG_CONFIG_HOME XDG_STATE_HOME XDG_DATA_HOME XDG_CACHE_HOME USERPROFILE
[ -n "$config" ] || config="$home/.config/herdr-engineering/always-on-review.yaml"
[ -r "$script" ] || { say "review script not found next to this install: $script"; exit 2; }
[ -r "$config" ] || { say "config not readable: $config"; exit 2; }

# The one credential: the status token. An ambient GitHub token on the agent is never used instead of it.
token=${OPUS_REVIEW_GITHUB_TOKEN:-}
unset OPUS_REVIEW_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN
[ -n "$token" ] || { say "OPUS_REVIEW_GITHUB_TOKEN is empty; the secret is not configured for this event"; exit 1; }
export GH_TOKEN=$token

# A PR whose head is in a fork is not reviewed here: the token must not sit next to code from outside the repository.
# Only the exact answer `false` continues. This is a safety net, not the control: see the fork section of the docs.
cross=$(gh pr view "$pr" --repo "$repo" --json isCrossRepository --jq .isCrossRepository) \
  || { say "cannot read $repo#$pr; nothing reviewed"; exit 1; }
case "$cross" in
  false) ;;
  true) say "$repo#$pr comes from a fork; not reviewing it"; exit 0 ;;
  *) say "unexpected answer for isCrossRepository ('$cross'); refusing to review $repo#$pr"; exit 1 ;;
esac

rc=0
python3 "$script" review --repo "$repo" --pr "$pr" --config "$config" --host-lock-wait "$lock_wait" --post || rc=$?
case "$rc" in
  0) ;;
  3) say "the head moved during the review; the new push triggers its own"; rc=0 ;;
  4) say "nothing to do for $repo#$pr: already reviewed, or a draft, cooldown, budget, disabled tier or waiting gate (see the docs)"; rc=0 ;;
esac
exit "$rc"
