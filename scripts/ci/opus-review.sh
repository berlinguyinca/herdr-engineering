#!/usr/bin/env bash
# The Woodpecker step for the per-PR `Opus agent review` status. Installed once on the review host, called by the tiny
# per-repo .woodpecker/opus-review.yaml, so the logic lives here and not in every repository. Operator guide:
# docs/operations/per-pr-opus-review.md.
#
#   herdr-opus-review [--config FILE] [--home DIR] [--lock-wait SECONDS] [--token-file FILE]
#
# Reads CI_PIPELINE_EVENT, CI_REPO and CI_COMMIT_PULL_REQUEST, which Woodpecker sets. The GitHub status token is NOT a
# Woodpecker secret (a missing secret fails every pipeline of the repo at compile time): it is read from a host file,
# default <real home>/.config/herdr-engineering/opus-review.token, overridable with --token-file or the agent's
# OPUS_REVIEW_TOKEN_FILE. The file must be owned by the running user with no group/other permissions (mode 0600).
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
set +x   # never trace this script: Woodpecker's log masking no longer covers a token that comes from a file

say() { printf 'opus-review: %s\n' "$*" >&2; }
here=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
script="$here/../../deploy/ci-review/always-on-review.py"
home=""
config=""
lock_wait=1800
token_file=${OPUS_REVIEW_TOKEN_FILE:-}
while [ $# -gt 0 ]; do
  case "$1" in
    --config) [ $# -ge 2 ] || { say "--config needs a file"; exit 2; }; config=$2; shift 2 ;;
    --home) [ $# -ge 2 ] || { say "--home needs a directory"; exit 2; }; home=$2; shift 2 ;;
    --lock-wait) [ $# -ge 2 ] || { say "--lock-wait needs seconds"; exit 2; }; lock_wait=$2; shift 2 ;;
    --token-file) [ $# -ge 2 ] || { say "--token-file needs a file"; exit 2; }; token_file=$2; shift 2 ;;
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

# The one credential: the status token, from a host file (never a Woodpecker secret, never the environment). It is read
# into this shell's memory only: never printed, never put on a command line. An ambient GitHub token is never used.
unset OPUS_REVIEW_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN
[ -n "$token_file" ] || token_file="$home/.config/herdr-engineering/opus-review.token"
case "$token_file" in -*) say "token file path may not start with '-': $token_file"; exit 2 ;; esac
if [ ! -f "$token_file" ]; then
  say "token file missing: $token_file"
  say "create it as $(id -un): install -m 600 /dev/null '$token_file' && \$EDITOR '$token_file'  (one line: a fine-grained PAT with commit statuses + pull requests write on the enrolled repos)"
  exit 1
fi
# Open once; check and read through the same descriptor, so the file cannot be swapped between the check and the read.
exec {tfd}< "$token_file" || { say "cannot open token file $token_file"; exit 1; }
[ "$(stat -Lc %u "/dev/fd/$tfd")" = "$(id -u)" ] \
  || { say "token file $token_file is not owned by $(id -un) (the user the agent runs as); refusing it"; exit 1; }
mode=$(stat -Lc %a "/dev/fd/$tfd")
if [ $(( 8#$mode & 8#077 )) -ne 0 ]; then
  say "token file $token_file has mode $mode; it must be 0600 (no group or other access): chmod 600 '$token_file'"
  exit 1
fi
token=$(tr -d '[:space:]' <&$tfd) || { say "cannot read token file $token_file"; exit 1; }
exec {tfd}<&-
[ -n "$token" ] || { say "token file $token_file is empty"; exit 1; }
export GH_TOKEN=$token         # reaches gh and the review script, as their credential; nothing prints it

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
