#!/bin/sh
# .woodpecker/release.yml, step read-back (docs/releases.md): fetch the raw manifest the registry serves for repository@digest.
# The release-json step then proves those bytes hash to the digest and are the multi-arch index; an empty or failed read
# fails here, before anything is tagged or published.
set -eu
# shellcheck source=scripts/release/lib.sh
. scripts/release/lib.sh

on_exit registry_logout
registry_login
digest=$(release_digest)
docker buildx imagetools inspect --raw "$HERDR_RELEASE_REPOSITORY@$digest" >.release/index.json \
  || fail "could not read $HERDR_RELEASE_REPOSITORY@$digest back from the registry"
[ -s .release/index.json ] || fail "the registry returned an empty manifest for $digest"
