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
