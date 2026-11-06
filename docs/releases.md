# Releases

A release publishes the `herdr-engineering` image, the one every control-plane and fabric-stack service runs
(control-plane-web, -worker, llm-proxy, collab, share, the fabric gateway, jobs; `deploy/control-plane/compose*.yaml`),
built from `deploy/fabric-stack/Dockerfile` with the repository root as context. Consumers pin it by digest, never by tag.

## What a release produces

On a tag `vX.Y.Z`, `.woodpecker/release.yml`:

1. checks out the tagged commit (pinned git image), proves `HEAD` is `$CI_COMMIT_SHA` and that the commit is on `main`;
2. refuses any tag that is not strict semver `v[0-9]+.[0-9]+.[0-9]+` (no leading zeros, no `-rc`, no `+meta`) or that
   differs from `pyproject.toml` `[project].version`;
3. builds the image for `linux/amd64` and `linux/arm64` with a throwaway BuildKit builder from a pinned `moby/buildkit`
   image, on the pinned `python:3.12-slim@sha256:...` base, stamped with the commit (`GIT_COMMIT`, OCI labels), and pushes
   it **by digest** (untagged) to `ghcr.io/berlinguyinca/herdr-engineering`;
4. reads the manifest back from the registry and fails unless its bytes hash to the build's digest and it is an image
   index listing both platforms. That multi-arch index digest, not a per-architecture one, is the release's digest;
5. writes `release.json` and the release notes, then points the convenience tag `vX.Y.Z` at the index and checks the
   tag resolves to exactly that digest;
6. creates the GitHub release for the tag with the notes (which carry `ghcr.io/berlinguyinca/herdr-engineering@sha256:...`)
   and attaches `release.json`. An existing release for the tag is never overwritten.

Any failure stops the pipeline before step 6, so a release that exists always has a verified digest.

`release.json` (schema: `deploy/release.schema.json`, validated by `tests/test_release_pipeline.py`):

```json
{
  "version": "0.1.0",
  "image": {"repository": "ghcr.io/berlinguyinca/herdr-engineering", "digest": "sha256:<64 hex>"},
  "commit": "<40-hex git sha>"
}
```

Deploy with `image.repository@image.digest`. The `vX.Y.Z` image tag is mutable and only for browsing the registry;
`herdr-fleet init` and `herdr-fleet pins update` read `release.json` and never resolve a tag.

## The manual gate: the owner cuts the release

The pipeline runs only on a tag event matching `refs/tags/v*`, and its first real step rejects everything but
`v[0-9]+.[0-9]+.[0-9]+` equal to the repo version. It never runs on pull requests, pushes, forks or manual triggers.
**Creating that tag is the owner's act and is the release gate**: nobody else (no agent, no automation) creates or
pushes release tags. The first release additionally needs the owner's go-ahead because it makes the image public.

To cut a release, the owner:

1. bumps `[project].version` in `pyproject.toml` on `main` (through a normal PR);
2. creates and pushes an annotated tag on that main commit: `git tag -a vX.Y.Z -m vX.Y.Z <sha> && git push origin vX.Y.Z`;
3. watches the `release` pipeline; on success, checks the GitHub release lists the digest and has `release.json`.

A tag is never moved or reused. If the pipeline fails after the push, fix the cause, bump to the next patch version and
tag again; an untagged pushed index from a failed run is harmless (nothing references it) and may be deleted in GHCR.

## One-time setup (owner)

- **Secrets** (Woodpecker repo settings, referenced only by name, available to the `tag` event only):
  - `ghcr_push_token`: a token with `write:packages` for `ghcr.io/berlinguyinca`; used for `docker login --password-stdin`.
  - `github_release_token`: a token with `contents: write` on this repository; creates the GitHub release.
- **Trusted repository** (Woodpecker, "volumes"): the docker steps mount the agent's docker socket.
- **Docker agent**: a Woodpecker agent with the docker backend (`labels: backend: docker`), like the one inferweave's
  `fry-gateway-image` pipeline uses. Agent prerequisites: buildx in the step image (`docker:27.5.1-cli` ships it) and
  arm64 emulation registered on the host, e.g. once as root `docker run --privileged --rm tonistiigi/binfmt --install arm64`
  (pin it by digest). Without it the build step fails with "the builder cannot build linux/arm64".
- **Public package**: the first push creates `ghcr.io/berlinguyinca/herdr-engineering` as a private package. After the
  first release the owner sets its visibility to public (GitHub, package settings) so fleets can pull without credentials.

## Pins and their upkeep

Every image the pipeline runs and the build base are pinned by digest in `.woodpecker/release.yml`
(`tests/test_release_pipeline.py` fails on an unpinned one). To update one, resolve the new digest with
`docker buildx imagetools inspect <image>:<tag>` and replace both tag and digest. The Dockerfile's `apt-get install`
(curl, ca-certificates) and `pip install` still resolve at build time, so two builds of one commit are not byte-identical;
the digest in `release.json` identifies the exact image that was published.

## Testing without publishing

`python -m pytest tests/test_release_pipeline.py` parses the pipeline and runs `scripts/release/release.py` (tag check,
digest extraction, read-back verification, `release.json` assembly, the GitHub release upload against a fake) on the
fixtures in `tests/fixtures/release/`: no network, no docker, no credentials.
