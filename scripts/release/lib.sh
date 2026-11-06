#!/bin/sh
# Shared by the docker steps of .woodpecker/release.yml (docs/releases.md). Sourced, not run.

fail() { printf 'release: %s\n' "$*" >&2; exit 1; }

# Run $1 on every exit, including a cancelled step: Woodpecker stops a step's container with SIGTERM before it kills it,
# and a signal trap that exits makes the EXIT trap run. Only SIGKILL skips it (docs/releases.md, Builder cleanup).
on_exit() {
  # shellcheck disable=SC2064 # $1 is expanded now on purpose
  trap "$1" EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# sha256 of stdin as hex: busybox/coreutils sha256sum in the step image, shasum elsewhere (the tests run on macOS too).
sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi | cut -d' ' -f1
}

require_env() {
  for name in "$@"; do
    eval "value=\${$name:-}"
    [ -n "$value" ] || fail "$name is not set"
  done
}

# The digest the digest step wrote, checked again: it is interpolated into registry references below.
release_digest() {
  digest=$(cat .release/digest) || fail "no .release/digest (the digest step did not run)"
  case "$digest" in
    sha256:*) ;;
    *) fail ".release/digest is not a sha256 digest" ;;
  esac
  hex=${digest#sha256:}
  [ "${#hex}" -eq 64 ] || fail ".release/digest is not a sha256 digest"
  case "$hex" in *[!0-9a-f]*) fail ".release/digest is not a sha256 digest" ;; esac
  printf '%s\n' "$digest"
}

# Log in to the registry of HERDR_RELEASE_REPOSITORY with the push token, in a private DOCKER_CONFIG that is removed on exit.
# The token goes over stdin only: it is never an argument, never printed, never written to the workspace.
registry_login() {
  require_env HERDR_RELEASE_REPOSITORY GHCR_PUSH_TOKEN CI_REPO_OWNER
  DOCKER_CONFIG=$(mktemp -d)
  export DOCKER_CONFIG
  registry=${HERDR_RELEASE_REPOSITORY%%/*}
  printf '%s' "$GHCR_PUSH_TOKEN" | docker login "$registry" --username "$CI_REPO_OWNER" --password-stdin >/dev/null \
    || fail "docker login to $registry failed"
}

registry_logout() {
  if [ -n "${DOCKER_CONFIG:-}" ]; then
    docker logout "${HERDR_RELEASE_REPOSITORY%%/*}" >/dev/null 2>&1 || true
    rm -rf "$DOCKER_CONFIG"
  fi
}
