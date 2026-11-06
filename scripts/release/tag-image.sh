#!/bin/sh
# .woodpecker/release.yml, step tag-image (docs/releases.md): point the version tag at the verified index, for people browsing
# the registry. The tag is mutable and nothing deploys by it; release.json carries the digest. The tag must resolve to exactly
# the verified digest afterwards, or the release stops before the GitHub release is written.
set -eu
# shellcheck source=scripts/release/lib.sh
. scripts/release/lib.sh

require_env CI_COMMIT_TAG
on_exit registry_logout
registry_login
digest=$(release_digest)
docker buildx imagetools create --tag "$HERDR_RELEASE_REPOSITORY:$CI_COMMIT_TAG" "$HERDR_RELEASE_REPOSITORY@$digest" >/dev/null
tagged=$(docker buildx imagetools inspect --raw "$HERDR_RELEASE_REPOSITORY:$CI_COMMIT_TAG" | sha256_hex)
[ "sha256:$tagged" = "$digest" ] || fail "$HERDR_RELEASE_REPOSITORY:$CI_COMMIT_TAG resolves to sha256:$tagged, not $digest"
