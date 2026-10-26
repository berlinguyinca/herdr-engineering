# Herdr Fabric Artifacts + Public Publication — implementation bundle

Date: 2026-10-25 | package: `herdr-artifact-fabric-publication-20261025`
Target: `berlinguyinca/herdr-engineering`, integrating any active `pi-engineering` components and existing Herdr collaborations. This is a **specification**, not evidence of a completed implementation.

## Goal
Every authorized fabric member can publish, find, render, reference, edit and version artifacts across hosts; optionally publish explicit immutable artifacts and allowlisted services publicly using transport-independent infrastructure. RustFS-compatible S3 is durable bytes, PostgreSQL is authority for identities, permissions, versions and provenance. Private-by-default. No competing orchestrator, registry, attachment storage or frontend.

## Mandatory implementation order
1. Inspect current tree/branch, open PRs and in-progress implementation; write `audit.md` with exact paths and design conflicts before edits.
2. Implement slices in `plans/` using existing modules. Require green focused tests, then integration tests and review between dependent slices.
3. Never expose internal Herdr UI/control-plane/Postgres/RustFS through the public edge. No publication without explicit human approval.
4. Record real design decisions and deviations in `decision-log.md`, deliver report in `implementation-report.md`.

## Bundle contents
- `architecture/`: bounded contexts, storage/identity, network edge and publication, UI, agent skills
- `contracts/`: representative API, manifest and policy schemas
- `plans/`: phased executable tickets and validations
- `security/`: threat model and adversarial acceptance cases
- `qa/`: end-to-end conformance matrix
- `agent-prompt.md`: copy-and-run prompt with Mac-to-remote transfer instructions

## Existing repository breadcrumbs (verify against HEAD)
- `herdr_engineering/control_plane/artifacts.py`: ArtifactStore register/get/download/attach/materialize/mark_available; SHA-256 keys, PostgreSQL.
- `herdr_engineering/control_plane/rustfs.py`: content-addressed object backend.
- `herdr_engineering/control_plane/api.py`: artifact upload path and in-memory fallback, possibly changed.
- `herdr_engineering/control_plane/graph_artifact.py`: deterministic sanitized HTML graph snapshot.
- `packages/conversation-composer.js`, `packages/collab-view.js`, `packages/session-workspace.js`: attachment and reference UI; October 24 C0 note said upload plumbing absent, but may be under construction.
- `docs/superpowers/notes/collab-editor-recon.md`: collaboration recon.
- `docs/specs/herdr-system-graph-20261020/05-artifacts-archify.md`: deferred Artifact Studio; Archify was explicitly dropped and must **not** be vendored.
- `deploy/control-plane/README.md`: references MinIO S3 substitute; verify running implementation before changing anything.
- `berlinguyinca/pi-engineering/src/artifacts/ArtifactStore.ts` may overlap; audit without copy/paste duplication.

Source references (2026-10-25 inspected):
https://github.com/berlinguyinca/herdr-engineering/blob/main/herdr_engineering/control_plane/artifacts.py
https://github.com/berlinguyinca/herdr-engineering/blob/main/docs/superpowers/notes/collab-editor-recon.md
https://github.com/berlinguyinca/herdr-engineering/blob/main/docs/specs/herdr-system-graph-20261020/05-artifacts-archify.md

## Non-goals
No arbitrary host filesystem publishing, public S3 bucket, public Herdr administration endpoint, general document editor, parallel Git or second orchestrator, or requirement that every installation use Tailscale.
