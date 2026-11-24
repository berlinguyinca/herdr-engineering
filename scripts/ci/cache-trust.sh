#!/usr/bin/env bash
# Decide whether this pipeline may WRITE the shared CI dependency cache (sourced by unit-gates.sh).
# Trusted = code already merged to main of this repo: push, cron or manual on branch main. A pull_request pipeline (fork or
# not) runs unreviewed code and is never trusted. Unknown or missing event/branch: untrusted (a private cache is only slower).
herdr_ci_cache_trusted() {
  local event="${CI_PIPELINE_EVENT:-${CI_EVENT:-}}"
  case "$event" in
    push|cron|manual) ;;
    *) return 1 ;;
  esac
  [ "${CI_COMMIT_BRANCH:-}" = "main" ] || return 1
  [ -n "${CI_REPO:-}" ] || return 1
  return 0
}

# herdr_ci_cache_setup TAG: point pip, npm and Playwright at a cache directory and set HERDR_CI_PRIVATE_CACHE to a directory the
# caller must delete on exit (empty when the shared cache is used).
#  - trusted event + writable shared store: use the shared store (what main and cron execute is built only by main/cron).
#  - everything else, above all pull_request: a private empty per-job directory. A PR never installs into, reads executables
#    from, or seeds from the shared store, so nothing a PR (or an edit of this file) leaves there can be run by a trusted
#    pipeline. It is cold (Chromium ~150 MB is downloaded per PR run); that is the price of not trusting the mount.
# This is advisory isolation: a PR job can still write the rw mount directly. Real isolation needs the runner
# (woodpecker-slurm) to mount the shared cache read-only for pull_request pipelines; that is a follow-up outside this repo.
herdr_ci_cache_setup() {
  local tag="$1" shared="${HERDR_CI_CACHE:-/home/wohlgemuth/woodpecker/ci/cache/herdr}" root
  HERDR_CI_PRIVATE_CACHE=""
  if herdr_ci_cache_trusted && mkdir -p "$shared" 2>/dev/null && [ -w "$shared" ]; then
    root="$shared"
    echo "$tag: trusted event; using the shared cache $root" >&2
  else
    HERDR_CI_PRIVATE_CACHE="$(mktemp -d "${TMPDIR:-/tmp}/herdr-ci-cache.XXXXXX")"
    root="$HERDR_CI_PRIVATE_CACHE"
    echo "$tag: untrusted event or shared cache unwritable; private cold cache $root" >&2
  fi
  export PIP_CACHE_DIR="$root/pip" npm_config_cache="$root/npm" PLAYWRIGHT_BROWSERS_PATH="$root/playwright"
  export PLAYWRIGHT_SKIP_BROWSER_GC=1   # an install must never delete a browser build another running job still uses
  mkdir -p "$PIP_CACHE_DIR" "$npm_config_cache" "$PLAYWRIGHT_BROWSERS_PATH"
  HERDR_CI_CACHE_ROOT="$root"
}
