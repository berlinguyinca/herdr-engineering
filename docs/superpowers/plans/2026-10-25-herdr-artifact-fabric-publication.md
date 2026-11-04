# Herdr Fabric Artifact Registry and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve Herdr’s existing artifact primitives into a durable, versioned, cross-host artifact registry and later an isolated, approval-gated public publication gateway without creating a competing registry, scheduler, collaboration transport, or frontend.

**Architecture:** PostgreSQL remains the authority for artifact identity, versions, ACLs, provenance, jobs, reviews, and publications; RustFS/S3-compatible storage holds immutable content-addressed blobs. The existing in-memory/event projection, dashboard SPA, collaboration transport, service catalog, and Pi scheduler are extended at their current seams. Public delivery is a later isolated data plane that consumes approved immutable snapshots and never exposes the control plane or private object origin.

**Tech Stack:** Python 3, stdlib HTTP server, asyncpg-compatible repository interfaces, boto3-compatible S3 client, PostgreSQL migrations, vanilla ES-module dashboard, existing Pi TypeScript scheduler, pytest, Node test runner, Playwright where applicable.

**Spec:** `docs/specs/herdr-artifact-fabric-publication-20261025/herdr-artifact-fabric-publication-20261025/`

## Global Constraints

- Private by default; no public exposure provider is required for private deployments.
- PostgreSQL is authoritative for identities, permissions, versions, provenance, and publication state; RustFS-compatible S3 stores immutable bytes.
- Extend existing artifact, collaboration, workspace, service, scheduler, and dashboard components; do not add a parallel registry, orchestrator, attachment store, or UI framework.
- Public publication requires explicit trusted human approval and an immutable pinned version.
- Never expose Herdr control-plane routes, PostgreSQL, RustFS origin credentials, arbitrary private addresses, or source worktrees through the public edge.
- Do not change production Tailscale, DNS, provider, or infrastructure state; use fake/local adapters until explicit operator authorization exists.
- Preserve existing artifact upload/download callers and event replay compatibility.
- Do not place artifact bytes in PostgreSQL or event streams; use bounded/streaming paths.

---

### Task 1: Freeze repository audit and compatibility contracts

**Files:**
- Create: `docs/specs/herdr-artifact-fabric-publication-20261025/audit.md`
- Create: `docs/specs/herdr-artifact-fabric-publication-20261025/implementation-report.md`
- Modify: `docs/specs/herdr-artifact-fabric-publication-20261025/decision-log.md`
- Test: `tests/control_plane/test_db.py`, `tests/control_plane/test_artifacts.py`

**Interfaces:**
- Consumes: existing artifact, RustFS, migration, API, collaboration, dashboard, service catalog, scheduler, and Pi artifact surfaces.
- Produces: frozen reuse map, known conflicts/unknowns, and decision records used by every later task.

- [x] **Step 1: Record exact current paths, branch/worktree state, and external license findings.**
- [x] **Step 2: Verify the ZIP manifest and establish the extracted specification root.**
- [ ] **Step 3: Add the initial implementation report with phase status and explicit validation gaps.**
- [ ] **Step 4: Run the existing focused artifact/database tests before editing implementation.**
- [ ] **Step 5: Commit only the audit/report/spec extraction checkpoint if the user’s branch policy permits commits.**

### Task 2: Add forward-only registry/version/blob schema

**Files:**
- Create: `deploy/control-plane/migrations/0003_artifact_registry.sql`
- Modify: `deploy/control-plane/migrations/0001_control_plane.sql` only if a compatibility comment/index correction is required; otherwise leave it unchanged.
- Test: `tests/control_plane/test_db.py`, `tests/control_plane/test_artifact_registry_migration.py`

**Interfaces:**
- Consumes: existing `artifacts`, `artifact_bindings`, `artifact_materializations`, `hosts`, `sessions`, `missions`, and `schema_migrations` tables.
- Produces: `artifact_versions`, `artifact_blobs`, `artifact_idempotency`, `artifact_reviews`, `artifact_comments`, `artifact_dependencies`, `artifact_build_jobs`, `artifact_audit`, `artifact_collections`, and publication-ready indexes with foreign keys and uniqueness constraints.

- [ ] **Step 1: Write migration assertions for existing tables and the new tables/indexes.**
- [ ] **Step 2: Run migration tests against the existing fake connection and a live disposable PostgreSQL when configured; verify current migrations remain ordered and idempotent.**
- [ ] **Step 3: Add the new forward-only migration with nullable/backfill-safe columns and no destructive rewrite.**
- [ ] **Step 4: Add CAS uniqueness on `(artifact_id, version_number)`, scoped content identity, idempotency keys, and audit append-only constraints.**
- [ ] **Step 5: Run migration tests again and record the actual database/backend used.**
- [ ] **Step 6: Commit `feat: add versioned artifact registry schema`.**

### Task 3: Make RustFS content handling streaming and verifiable

**Files:**
- Modify: `herdr_engineering/control_plane/rustfs.py`
- Modify: `herdr_engineering/control_plane/artifacts.py`
- Test: `tests/control_plane/test_rustfs.py`, `tests/control_plane/test_artifacts.py`

**Interfaces:**
- Consumes: existing `RustFSClient.put/get/head/delete` and content-addressed key format.
- Produces: `put_stream(stream, expected_sha256=None, expected_size=None, media_type=None) -> StoredBlob`, `iter_get(content_key, expected_sha256, chunk_size=...)`, and bounded verification helpers. Existing `put/get` remain compatible wrappers.

- [ ] **Step 1: Write tests for chunked upload hashing, expected-size mismatch, expected-digest mismatch, bounded read iteration, and compatibility wrappers.**
- [ ] **Step 2: Run the focused RustFS tests and confirm failures for missing streaming methods.**
- [ ] **Step 3: Implement a stream adapter that sends the S3 body without accumulating the full payload; use a temporary verified spool only when the boto3-compatible client requires seekable multipart input.**
- [ ] **Step 4: Implement verified chunked reads and ensure the final digest/byte count is checked before returning completion.**
- [ ] **Step 5: Run focused tests plus tamper and missing-object cases.**
- [ ] **Step 6: Commit `feat: add bounded RustFS artifact streaming`.**

### Task 4: Extend ArtifactStore with immutable versions and CAS commits

**Files:**
- Modify: `herdr_engineering/control_plane/artifacts.py`
- Modify: `herdr_engineering/control_plane/ids.py` only if an existing ID helper cannot provide stable version IDs.
- Test: `tests/control_plane/test_artifacts.py`, `tests/control_plane/test_artifact_versions.py`

**Interfaces:**
- Consumes: Task 2 tables and Task 3 blob primitives.
- Produces: `register_logical(...) -> ArtifactRecord`, `commit_version(artifact_id, base_version_id, source_manifest, outputs, provenance, idempotency_key) -> VersionRecord`, `list_versions(artifact_id)`, `get_version(version_id)`, `checkout(version_id, target_dir)`, `materialize(version_id, host_id, session_id, target_path)`, and explicit `ArtifactConflict`/`ArtifactValidationError` exceptions.

- [ ] **Step 1: Write failing tests for initial version creation, immutable version records, source/output manifest hashes, stale CAS conflict, idempotent retry, and no metadata visibility after an upload crash.**
- [ ] **Step 2: Run focused tests to verify the new API is absent/failing.**
- [ ] **Step 3: Implement transactional version commit: verify all referenced blobs, insert the version, advance the logical head only when `base_version_id` matches, and append audit/event metadata without bytes.**
- [ ] **Step 4: Implement conservative orphan tracking and authorized materialization with canonical path checks, no symlink escape, and atomic destination placement.**
- [ ] **Step 5: Preserve existing `register`, `download`, `attach`, and `materialize` callers through compatibility adapters.**
- [ ] **Step 6: Run focused tests, including two concurrent commits where exactly one wins.**
- [ ] **Step 7: Commit `feat: add immutable artifact versions and CAS commits`.**

### Task 5: Add version-aware API routes and projection compatibility

**Files:**
- Modify: `herdr_engineering/control_plane/api.py`
- Modify: `herdr_engineering/control_plane/repo.py`
- Modify: `herdr_engineering/control_plane/bridge.py`
- Test: `tests/control_plane/test_artifacts_api.py`, `tests/control_plane/test_repo.py`, `tests/control_plane/test_bridge.py`

**Interfaces:**
- Consumes: Task 2–4 registry/store methods and current viewer/operator role checks.
- Produces: compatible `POST /api/v1/artifacts/uploads/init`, upload completion, ACL-filtered list/detail/history, version commit with `409` stale conflict, scoped materialization, and structured error envelopes while retaining current raw upload/download routes.

- [ ] **Step 1: Add API tests for upload-init, completion, list/detail/history, version commit, stale conflict, idempotent retry, denied role, and legacy upload/download behavior.**
- [ ] **Step 2: Run the focused API tests and capture current failures.**
- [ ] **Step 3: Implement request parsing, size/file limits, idempotency, server-derived provenance, and route-specific role enforcement.**
- [ ] **Step 4: Extend in-memory repo/event replay projections with version/head metadata only; keep payload bytes out of events.**
- [ ] **Step 5: Run focused API/replay tests and current control-plane tests.**
- [ ] **Step 6: Commit `feat: expose versioned artifact registry APIs`.**

### Task 6: Add native `herdr-artifact` skill and CLI contract

**Files:**
- Create: `skills/herdr-artifact/SKILL.md`
- Create: `skills/herdr-artifact/scripts/herdr-artifact`
- Modify: `herdr_engineering/cli.py`
- Modify: `herdr_engineering/control_plane/api.py` only for CLI client route reuse if required.
- Test: `tests/test_cli_artifacts.py`, `tests/control_plane/test_artifact_skill_contract.py`

**Interfaces:**
- Consumes: Task 5 API contract and existing CLI/control-plane URL/auth resolution.
- Produces: native commands `create`, `checkout`, `inspect`, `edit`, `validate`, `publish --base-version`, `history`, `diff`, `materialize`, `bundle`, and `share-request`; output is JSON-safe, includes stable artifact/version IDs, and never auto-publishes publicly.

- [ ] **Step 1: Write CLI contract tests for authenticated success, missing adapter, denied access, stale base version, and share-request pending approval.**
- [ ] **Step 2: Run contract tests to verify the commands are not yet available.**
- [ ] **Step 3: Implement the thin CLI wrapper; keep rendering/build execution in jobs and route scheduling through the existing scheduler boundary.**
- [ ] **Step 4: Define source archive, manifest, dependency lock, rendered output, preview, and validation report preservation fields.**
- [ ] **Step 5: Run CLI and Python tests and verify the skill has no copied Anthropic source or restricted assets.**
- [ ] **Step 6: Commit `feat: add native herdr-artifact authoring skill`.**

### Task 7: Add dashboard artifact registry and collaboration references

**Files:**
- Modify: `packages/app.js`
- Create: `packages/artifacts-view.js`
- Create: `packages/artifacts-view.test.js`
- Modify: `packages/conversation-composer.js`
- Modify: `packages/session-workspace.js`
- Modify: `herdr_engineering/control_plane/collab.py` and its tests.
- Modify: `packages/base.css`/`theme.css` only for existing token-based artifact styles.

**Interfaces:**
- Consumes: Task 5 list/detail/history API and existing composer/upload adapter.
- Produces: `/artifacts` and contextual artifact links with server-filtered list/detail/preview/version/provenance states; collaboration messages carry artifact references, never binary payloads.

- [ ] **Step 1: Add UI tests for list filters, preview states, version selection, provenance, loading/error/offline state, keyboard navigation, and safe preview fallback.**
- [ ] **Step 2: Add collaboration tests for artifact chips, unauthorized reference filtering, and session transport compatibility.**
- [ ] **Step 3: Implement the artifact view using existing vanilla modules/tokens and safe iframe/download behavior.**
- [ ] **Step 4: Wire composer upload results into the existing message/reference contract and preserve the current “unsupported native file delivery” behavior until transport support exists.**
- [ ] **Step 5: Run Node client/UI tests and targeted Playwright tests.**
- [ ] **Step 6: Commit `feat: add native artifact registry dashboard`.**

### Task 8: Add isolated public publication controller, gateway, and fake provider

**Files:**
- Create: `herdr_engineering/control_plane/publication.py`
- Create: `herdr_engineering/public_gateway.py`
- Create: `herdr_engineering/exposure.py`
- Create: `deploy/public-gateway/README.md`
- Create: `deploy/public-gateway/compose.yaml`
- Modify: `herdr_engineering/control_plane/api.py`
- Modify: `deploy/control-plane/README.md`
- Test: `tests/test_public_gateway.py`, `tests/control_plane/test_publication.py`, `tests/test_exposure_provider.py`

**Interfaces:**
- Consumes: approved immutable artifact versions and existing service catalog identities.
- Produces: provider-neutral `ExposureProvider` protocol (`capabilities`, `validate_config`, `prepare`, `expose`, `observed_state`, `revoke`, `reconcile`), private-only provider, fake provider conformance suite, and a delivery-only gateway with opaque hashed tokens, expiry, revoke, exact methods, rate/body limits, CSP/nosniff, and fail-closed policy cache.

- [ ] **Step 1: Write negative tests for guessed IDs/hashes, revoked/expired tokens, arbitrary origin URLs, SSRF/private IPs, path traversal, Host abuse, control-plane routes, and stale policy.**
- [ ] **Step 2: Implement publication state records and human-only approval checks without any external provider side effect.**
- [ ] **Step 3: Implement static pinned delivery through a dedicated public namespace or gateway read adapter; do not expose the private RustFS origin.**
- [ ] **Step 4: Implement provider protocol, private-only provider, and fake provider with idempotent conformance behavior.**
- [ ] **Step 5: Run security tests and local gateway integration tests; verify private-only mode with zero providers.**
- [ ] **Step 6: Commit `feat: add approval-gated private-safe publication gateway`.**

### Task 9: Add opt-in Tailscale Funnel adapter and publication dashboard

**Files:**
- Create: `herdr_engineering/exposure_tailscale.py`
- Modify: `packages/app.js`
- Create: `packages/publications-view.js`
- Modify: `deploy/public-gateway/README.md`
- Test: `tests/test_exposure_tailscale.py`, `packages/publications-view.test.js`

**Interfaces:**
- Consumes: Task 8 provider contract and existing private Tailscale Serve/config helpers.
- Produces: capability-reported, opt-in Funnel adapter and publication list/approval/revoke/suspend UI; no production Tailscale mutation in automated tests.

- [ ] **Step 1: Write fake-command tests proving no Funnel command runs when provider is disabled and that provider errors are explicit.**
- [ ] **Step 2: Implement capability/config validation and scoped, idempotent command adapter without authorization decisions in the adapter.**
- [ ] **Step 3: Implement publications view with desired-vs-observed state, approval confirmation, expiry, and global disable control.**
- [ ] **Step 4: Run fake provider/gateway/UI tests and document isolated-tailnet-only live validation.**
- [ ] **Step 5: Commit `feat: add opt-in Tailscale publication adapter`.**

### Task 10: Integrate jobs, observability, acceptance workflow, and operations docs

**Files:**
- Modify: `herdr_engineering/control_plane/worker.py`
- Modify: `herdr_engineering/observability.py`
- Modify: `herdr_engineering/control_plane/events.py`
- Create: `tests/integration/test_artifact_cross_host.py`
- Create: `docs/architecture/herdr-artifact-publication.md`
- Modify: `docs/specs/herdr-artifact-fabric-publication-20261025/implementation-report.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Tasks 2–9 artifact/job/publication contracts and existing worker/event/metrics systems.
- Produces: cross-host A→B→C workflow evidence, metrics/tracing with redaction, rollback/kill-switch docs, migration compatibility notes, and explicit unresolved deployment gaps.

- [ ] **Step 1: Add the integration fixture with independent host/session/agent principals and a durable fake RustFS/PostgreSQL backend.**
- [ ] **Step 2: Exercise create on A, checkout/edit on B, review on C, immutable dashboard history, approval, external delivery, and revoke denial.**
- [ ] **Step 3: Add artifact/job/publication metrics and event correlation fields without logging secrets, prompts, tokens, or private content.**
- [ ] **Step 4: Run Python, Node, integration, security, and UI tests in the smallest useful sequence.**
- [ ] **Step 5: Update implementation-report.md with actual files, tests, decisions, compatibility, and gaps.**
- [ ] **Step 6: Commit `docs: document artifact publication operations and evidence`.**

## Validation sequence

1. `pytest -q tests/control_plane/test_db.py tests/control_plane/test_artifacts.py tests/control_plane/test_artifacts_api.py`
2. Focused new registry/RustFS/CAS/API/security tests.
3. Full Python suite: `pytest -q`.
4. Frontend client tests: `npm test -- --runInBand` is not applicable to this Node runner; use the repository’s `npm run test:client` and `npm run test:ui` scripts.
5. Pi typecheck/tests only when the integration adds a TypeScript adapter; do not copy Pi’s local store.
6. Final source/secret scan, migration ordering/idempotency check, and private-only gateway smoke test.

## Coverage gaps kept explicit

- Real RustFS image versus current MinIO-compatible deployment must be tested in the operator’s target environment.
- Real Host A/B/C machines and an isolated Tailscale test tailnet are not available in this checkout; local independent-process fixtures prove protocol behavior only.
- Public DNS/certificate/provider credentials and production exposure remain operator-controlled and are intentionally not changed by this plan.

