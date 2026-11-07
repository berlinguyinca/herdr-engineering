# Releases

A release publishes the `herdr-engineering` image, the one every control-plane and fabric-stack service runs
(control-plane-web, -worker, llm-proxy, collab, share, the fabric gateway, jobs; `deploy/control-plane/compose*.yaml`),
built from `deploy/fabric-stack/Dockerfile` with the repository root as context. Consumers pin it by digest, never by tag.

## What a release produces

On a tag `vX.Y.Z`, `.woodpecker/release.yml`:

1. checks out the tagged commit (pinned git image), proves `HEAD` is `$CI_COMMIT_SHA` and that the commit is on `main`;
2. refuses any tag that is not strict semver `v[0-9]+.[0-9]+.[0-9]+` (no leading zeros, no `-rc`, no `+meta`) or that
   differs from any version source: `pyproject.toml` `[project].version`, `herdr_engineering/version.py` (what the CLI and
   `/api/v1/health` report) and `herdr_engineering/control_plane/__init__.py`;
3. builds the image for `linux/amd64` and `linux/arm64` with a throwaway BuildKit builder from a pinned `moby/buildkit`
   image, on the pinned `python:3.12-slim@sha256:...` base, stamped with the commit (`GIT_COMMIT`, OCI labels), and pushes
   it **by digest** (untagged) to `ghcr.io/berlinguyinca/herdr-engineering`;
4. reads the manifest back from the registry and fails unless its bytes hash to the build's digest and it is an image
   index listing both platforms. That multi-arch index digest, not a per-architecture one, is the release's digest;
5. writes `release.json` and the release notes, then points the convenience tag `vX.Y.Z` at the index and checks the
   tag resolves to exactly that digest;
6. creates the GitHub release for the tag as a **draft** with the notes (which carry
   `ghcr.io/berlinguyinca/herdr-engineering@sha256:...`), uploads `release.json`, reads the asset list back to confirm
   `release.json` is there with the right size, and only then publishes the draft.

A failure in steps 1-5 stops before any GitHub release exists. A failure inside step 6 (an upload error, a missing asset)
leaves at most an unpublished draft, which nobody sees and `herdr-fleet` never reads. Re-running the pipeline for the
same tag reuses that draft, replaces its `release.json` and publishes it, so the version is not burned. A release that is
already published is never touched; the run fails instead. So a published release always has a verified digest and its
`release.json`.

`release.json` (schema: `deploy/release.schema.json`, validated by `tests/test_release_pipeline.py`):

```json
{
  "version": "0.1.1",
  "image": {"repository": "ghcr.io/berlinguyinca/herdr-engineering", "digest": "sha256:<64 hex>"},
  "commit": "<40-hex git sha>"
}
```

Deploy with `image.repository@image.digest`. The `vX.Y.Z` image tag is mutable and only for browsing the registry;
`herdr-fleet pins update --from release.json` reads it and never resolves a tag (`herdr-fleet init` does not read it).

## The manual gate: the owner cuts the release

The pipeline runs only on a tag event matching `refs/tags/v*`, and its first real step rejects everything but
`v[0-9]+.[0-9]+.[0-9]+` equal to the repo version. It never runs on pull requests, pushes, forks or manual triggers.
**Creating that tag is the owner's act and is the release gate**: nobody else (no agent, no automation) creates or
pushes release tags. The first release additionally needs the owner's go-ahead because it makes the image public.

**The checks inside the pipeline are not a security boundary.** Woodpecker reads `release.yml` from the tagged commit, so
anyone who can push a `v*` tag can push it on a commit whose `release.yml` drops the semver and main-ancestry checks, and
that pipeline receives both secrets. The checks stop accidents. People are stopped by the tag ruleset below, which is
**required before the first release**. The main-ancestry check stays in the checkout step: it is reliable because
the step fetches the full history of `main` together with the tagged commit (no `--depth`), so `git merge-base
--is-ancestor` can see the commit if it is on main.

To cut a release, the owner:

1. bumps the version on `main` (through a normal PR) in all three sources named above. **A `v0.1.0` tag already exists
   (annotated, 2026-09-24), so the first release must be `v0.1.1` or later;**
2. creates and pushes an annotated tag on that main commit: `git tag -a vX.Y.Z -m vX.Y.Z <sha> && git push origin vX.Y.Z`;
3. watches the `release` pipeline; on success, checks the GitHub release lists the digest and has `release.json`.

A tag is never moved or reused. If the pipeline fails before step 6, fix the cause and re-run the pipeline for the
same tag (Woodpecker "restart"); it rebuilds and pushes a new index. If the fix needs a code change, bump to the next
patch version and tag again. An untagged pushed index from a failed run is harmless (nothing references it) and may be
deleted in GHCR. Leftover drafts of abandoned versions may be deleted in GitHub.

## One-time setup (owner), all required before the first release

- **Tag ruleset (GitHub, required)**: a repository ruleset targeting tags `refs/tags/v*`, with "Restrict creations",
  "Restrict updates" and "Restrict deletions" enabled and only the owner in the bypass list. Without it, any collaborator
  with write access can start a release with a `release.yml` of their choosing (see above).
- **Secrets** (Woodpecker repo settings, referenced only by name):
  - `ghcr_push_token`: a **classic** personal access token with `write:packages` (fine-grained tokens do not cover GitHub
    Packages), created by the account that owns `ghcr.io/berlinguyinca`, because `docker login` uses `$CI_REPO_OWNER`
    (`berlinguyinca`) as the username. It reaches `docker login` only through `--password-stdin`.
  - `github_release_token`: a **fine-grained** personal access token for this repository only, with "Contents: Read and
    write" (creates, uploads to and publishes the release). The script never follows an HTTP redirect, so the token
    cannot be forwarded to another host.
  - Scope both to the `tag` event only. Do not restrict them by image: Woodpecker hands image-restricted secrets only to
    plugin steps, and these steps run commands. No pull request ever receives them, because this pipeline never runs
    on `pull_request` and the other pipelines do not reference them.
- **Trusted repository** (Woodpecker, "volumes"): the docker steps mount the agent's docker socket, which needs the repo
  to be marked trusted. **Marking it trusted is the owner's call, and it widens exposure.** Woodpecker reads a
  `pull_request` pipeline's config from the PR head, so on a trusted repo any PR pipeline can add a step with
  `labels: backend: docker` and `/var/run/docker.sock`, which is root on the docker agent. Neither the tag ruleset nor
  the branch protection of `main` prevents that: `main` has only a ruleset that requires a PR with 0 approvals, so any
  writer can merge their own PR, and PR pipelines run before any merge. A PR pipeline cannot read the tag-scoped
  secrets, but it controls the host that later runs a release. Recommended before trusting the repo:
  - enable Woodpecker "require approval for pull requests" (at least forks; all PRs if the docker agent is shared);
  - run the release pipeline on a **dedicated docker agent** (label-restricted to this repo), not a shared one;
  - or avoid trusting this repo at all by moving the release job to a separate repo or agent.
- **Docker agent** (dedicated, see above): a Woodpecker agent with the docker backend (`labels: backend: docker`), like the one inferweave's
  `fry-gateway-image` pipeline uses. Agent prerequisites: buildx in the step image (`docker:27.5.1-cli` ships it) and
  arm64 emulation registered on the host, once as root and again after each reboot (or via the host's binfmt service):
  `docker run --privileged --rm tonistiigi/binfmt:qemu-v9.2.2@sha256:1b804311fe87047a4c96d38b4b3ef6f62fca8cd125265917a9e3dc3c996c39e6 --install arm64`.
  It is a host setup step, not a pipeline step, because it needs `--privileged`. Without it the build step fails with
  "the builder cannot build linux/arm64".
- **Public package**: the first push creates `ghcr.io/berlinguyinca/herdr-engineering` as a private package. After the
  first release the owner sets its visibility to public (GitHub, package settings) so fleets can pull without credentials.

## Rollback

Never re-tag to roll back. To roll back, consumers re-pin the previous release: `herdr-fleet pins update`
(or a manual edit) sets the image to the previous release's `release.json` `image.repository@image.digest` and redeploys.
Published releases and their GHCR versions are never deleted. Do not enable a GHCR retention or cleanup policy for
untagged versions, because every release index is untagged by design (the `vX.Y.Z` tag is a convenience that may
point elsewhere). A moved or deleted mutable tag cannot affect a consumer, since none resolves tags. A broken release is
superseded by the next patch version; its GitHub release can be edited to say "do not use".

## Builder cleanup

`build-push.sh` creates a BuildKit container (`buildx_buildkit_herdr-release-<pipeline>0`) and installs an EXIT trap
(plus HUP, INT and TERM traps that turn the signal into an exit) that removes it. The removal is **best effort**. On a
normal success or failure exit the trap runs. On a cancel, Woodpecker sends SIGTERM, but the step's commands run under
`sh` as PID 1, which defers the trap until the foreground `buildx build` returns, so the builder may be removed late
or, if the step is then killed (SIGKILL, agent crash, the stop grace period elapsing), not at all. The reliable path is
to find and remove leftovers on the agent host:
`docker ps -a --filter name=buildx_buildkit_herdr-release- --format '{{.Names}}' | xargs -r docker rm -f`.
The registry logout runs in the same trap, so it is best effort too; the token is passed only through
`--password-stdin` and the step's environment ends with the step.

## Memory-client variant (not published)

The published image is the **base image only**, built on `python:3.12-slim`. When the fleet config enables the memory
client, `deploy/control-plane/deploy.sh` builds a different image (`RUNTIME_BASE=herdr-memory-client:<sha>`, from the
private `herdr-memory` repository; `memory-client-deploy.sh`, `prepare-memory-client.sh`). That variant contains private
code, so **public CI cannot publish it**. A memory-enabled fleet works like this:

- the operator builds the memory-client variant and pins it as `images.memory-client`;
- the control-plane role on memory hosts runs that pinned image;
- a base web image that already has the memory CLI blocks with `memory-client-not-pinned`;
- never adopt the base release digest for control-plane-web/-worker on a memory host: the base image has no memory CLI,
  its health checks still pass, and memory features would break silently.

## Pins and their upkeep

Every image the pipeline runs and the build base are pinned by digest in `.woodpecker/release.yml`
(`tests/test_release_pipeline.py` fails on an unpinned one). To update one, resolve the new digest with
`docker buildx imagetools inspect <image>:<tag>` and replace both tag and digest. The Dockerfile's `apt-get install`
(curl, ca-certificates) and `pip install` still resolve at build time, so two builds of one commit are not byte-identical;
the digest in `release.json` identifies the exact image that was published.

## Testing without publishing

`python -m pytest tests/test_release_pipeline.py` parses the pipeline and runs `scripts/release/release.py` (tag check,
digest extraction, read-back verification, `release.json` assembly, the draft-then-publish GitHub release against a
fake API) on the fixtures in `tests/fixtures/release/`. It also runs the docker step scripts against a fake `docker`
on `PATH`. No network, no docker, no credentials.
