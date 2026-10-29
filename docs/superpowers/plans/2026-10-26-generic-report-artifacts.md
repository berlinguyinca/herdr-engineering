# Generic report artifacts implementation plan

**Goal:** Implement all five approved features end to end: a machine-readable
report descriptor, semantic version differences, independent review stages,
version-pinned finding/figure feedback, and bounded previews with separate assets.

**Architecture:** Extend the existing immutable ArtifactStore/CAS authority and
worker renderer. Keep descriptor validation/diffs and HTML rendering pure;
persist report attestations/feedback in native additive PostgreSQL migrations.
HTTP routes enforce artifact permissions before lookup/download; the dashboard
selects pinned versions and consumes only bounded metadata until assets are
explicitly requested. Domain algorithms and thresholds are outside this layer.

**Working state:** The checkout contains ongoing uncommitted artifact, scheduler,
and collaboration work. Preserve all existing changes. Each agent owns bounded
files; the leader owns integration into ArtifactStore/API/CLI and final proof.
No production database, service deployment, public sharing, push, or merge.

## Public contract

- A version optionally carries `report_descriptor` (`schema_version: 1`) and
  persisted renderer `validation`. Existing non-report commits remain compatible.
- Descriptor collections: `questions`, `evidence`, `findings`, `figures`, `assets`;
  stable IDs and checked references; domain-neutral `analysis`, `limitations`,
  `next_steps`. Evidence can be qualitative; no fabricated numeric requirements.
- Assets reference separately uploaded content by SHA-256, MIME, size and safe
  name. Roles: data/source/figure/result/preview. No arbitrary URLs/worktree paths.
- GET/POST report operations live under
  `/api/v1/artifacts/{artifact}/versions/{version}/`: `report`, `diff?base_version_id=`,
  `feedback`, `checks`, `preview`, and `assets/{asset}`. Blob upload is
  `POST /api/v1/artifacts/{artifact}/blobs` and requires edit permission.
- Report response: `{artifact_id, version_id, report_descriptor, checks, state,
  feedback, assets, preview_url}`. Diff classifies data, analysis, editorial and
  presentation changes, with added/removed/changed stable IDs.
- Checks are separately attributed `rendered`, `evidence_checked`, `reviewed`
  events with passed/failed status, notes and evidence references. Format success
  alone never sets evidence/review success. Version changes do not inherit checks.
- Feedback targets `report`, `finding`, or `figure`, with exact immutable version,
  server-derived author and validated target; old feedback stays on the old version.
- Private preview is bounded and inert; evidence download is explicit, authorized,
  version-bound and streamed/spooled with integrity verification. No large raw
  evidence is loaded to build a preview.

## Implementation and proof

- [x] Pure descriptor validator/schema, two unrelated-domain fixtures, semantic diff.
- [x] Native `0008_artifact_reports.sql`, durable checks and feedback, real PostgreSQL tests.
- [x] Generic descriptor HTML renderer, bounded preview, worker validation persistence.
- [x] Store integration: CAS/idempotency includes descriptor/validation; scoped blob references.
- [x] Authenticated API, asset upload/download and CLI report operations.
- [x] Dashboard version selector, evidence-linked findings, diff categories, stage controls,
  anchored comments, lightweight preview and explicit asset downloads.
- [x] Canonical skill/client contract updated to implemented commands and descriptor.
- [x] Targeted Python/client tests, PostgreSQL persistence/restart/foreign-version tests,
  browser workflow, permission failures, XSS/integrity/large-asset checks.
- [x] Independent review and requirement-by-requirement completion audit; no claims
  of deployment or evidence checking based solely on rendering success.

Local completion and verification record: [2026-10-28 evidence](../../handoffs/2026-10-28-generic-report-completion.md). No deployment or production migration was performed.
