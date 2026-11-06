#!/bin/sh
# .woodpecker/release.yml, step build-push (docs/releases.md): build the herdr-engineering image (deploy/fabric-stack/Dockerfile,
# the image every control-plane and fabric-stack service runs) for linux/amd64 and linux/arm64 and push it BY DIGEST: the
# registry gets an untagged multi-arch index; .release/build-metadata.json records its digest for the digest step.
set -eu
# shellcheck source=scripts/release/lib.sh
. scripts/release/lib.sh

require_env HERDR_RELEASE_REPOSITORY HERDR_BUILDKIT_IMAGE HERDR_RUNTIME_BASE CI_REPO CI_COMMIT_SHA CI_COMMIT_TAG CI_PIPELINE_NUMBER
case "$CI_PIPELINE_NUMBER" in *[!0-9]*) fail "CI_PIPELINE_NUMBER is not a number" ;; esac
docker buildx version >/dev/null 2>&1 || fail "the step image has no docker buildx plugin"

mkdir -p .release
builder="herdr-release-$CI_PIPELINE_NUMBER"
cleanup() {
  docker buildx rm --force "$builder" >/dev/null 2>&1 || true
  registry_logout
}
trap cleanup EXIT
registry_login

# A throwaway BuildKit builder from a pinned image, so the build tooling does not float with the agent host's docker.
docker buildx create --name "$builder" --driver docker-container --driver-opt "image=$HERDR_BUILDKIT_IMAGE" >/dev/null
docker buildx inspect --bootstrap "$builder" >.release/builder.txt
for platform in linux/amd64 linux/arm64; do
  grep -q "$platform" .release/builder.txt \
    || fail "the builder cannot build $platform: register binfmt on the agent host (docs/releases.md, Agent prerequisites)"
done

docker buildx build --builder "$builder" \
  --platform linux/amd64,linux/arm64 \
  -f deploy/fabric-stack/Dockerfile \
  --build-arg "RUNTIME_BASE=$HERDR_RUNTIME_BASE" \
  --build-arg "GIT_COMMIT=$CI_COMMIT_SHA" \
  --label "org.opencontainers.image.source=https://github.com/$CI_REPO" \
  --label "org.opencontainers.image.revision=$CI_COMMIT_SHA" \
  --label "org.opencontainers.image.version=$CI_COMMIT_TAG" \
  --provenance=false --sbom=false \
  --output "type=image,name=$HERDR_RELEASE_REPOSITORY,push-by-digest=true,name-canonical=true,push=true" \
  --metadata-file .release/build-metadata.json \
  .
