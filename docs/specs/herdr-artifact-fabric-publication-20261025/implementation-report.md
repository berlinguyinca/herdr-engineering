# Herdr Artifact Fabric Publication — implementation report

Date: 2026-10-25  
Status: Phase 1 registry/storage foundation in progress  
Spec root: `docs/specs/herdr-artifact-fabric-publication-20261025/herdr-artifact-fabric-publication-20261025/`

## Completed in this checkpoint

- Transferred the specification from the discovered Mac host `mac.lan` and
  verified the ZIP and all 17 manifest hashes.
- Added the repository integration audit at `audit.md`.
- Added the executable implementation plan at
  `docs/superpowers/plans/2026-10-25-herdr-artifact-fabric-publication.md`.
- Added forward-only migration `0003_artifact_registry.sql` for immutable
  versions, scoped blobs, idempotency, reviews/comments, dependencies, build
  jobs, append-only audit, and pinned collections.
- Extended `RustFSClient` with disk-spooled `put_stream` and verified
  `iter_get`; existing `put` and `get` compatibility remains intact.
- Extended `ArtifactStore` with logical artifact registration, immutable version
  commits, idempotency keys, CAS conflict detection, version lookup/history,
  and audit writes. Legacy byte registration/download methods remain intact.
- Exposed version history and commit operations through the existing control
  plane as `/api/v1/artifacts/{id}/versions` and its detail/POST routes, with
  `409 artifact_version_conflict` responses for stale heads.
- Hardened the gated version slice: output manifests require verified blob
  references, API authorship is derived from authentication, version detail is
  scoped to its parent artifact, concurrent idempotency retries recheck after
  the artifact lock, database rows normalize date/time values, and version
  routes fail closed without an explicit `sync_call` adapter.
- Added a private-by-default publication controller, fake exposure provider,
  delivery-only gateway, approval/revoke/expiry behavior, and publication
  migrations `0004_artifact_publications.sql`.
- Added authenticated control-plane publication request/approve/revoke routes,
  artifact catalog/detail routes, and the first dashboard Artifacts view with
  version-history display.
- Added the native `skills/herdr-artifact/SKILL.md` contract for Pi, Claude,
  and compatible external agents.
- Added an executable `skills/herdr-artifact/scripts/herdr-artifact` wrapper
  delegating upload/history operations to the existing authenticated HerdR CLI
  and control-plane contracts.
- The wrapper now supports `share-request ARTIFACT_ID VERSION_ID`, creating a
  pending publication request with audience/expiry options while refusing any
  automatic approval or activation.
- Added deterministic pinned-version bundle manifests, reproducible ZIP export,
  and bounded text/binary diff helpers for the agent authoring contract.
- Added the artifact build-job lifecycle contract with supported profiles,
  scheduler enqueue injection, host claims, terminal validation/output state,
  and failure/cancel retention.
- Added an opt-in Tailscale Funnel provider adapter with capability reporting,
  loopback-target validation, exact scoped commands, and disabled-by-default
  private-only behavior.
- Wired provider selection to central fleet config: private-only remains the
  default, while `public_exposure_provider: tailscale_funnel` is explicit and
  carries the configured gateway loopback target into publication records.
- Added an opt-in Cloudflare Tunnel provider with the same transport-only,
  loopback-target, capability-reporting contract as Tailscale Funnel.
- Added `SyncArtifactAdapter`, a persistent event-loop bridge for asyncpg-backed
  ArtifactStore calls from the threaded HTTP server, and wired it into the real
  control-plane startup when PostgreSQL plus RustFS are configured.
- Added a runnable isolated public gateway HTTP server with `/s/<token>/...`
  delivery-only routing and explicit control-plane path isolation.
- Added the durable public-delivery resolver that hashes opaque tokens, reads
  only active publication rows joined to pinned artifact versions, and fetches
  only the requested private object output from S3-compatible storage.
- Added `herdr-eng control-plane public-gateway`, an isolated delivery-only
  process command that owns a persistent asyncpg/S3 adapter and exposes no
  dashboard or control-plane routes.
- Added an opt-in standalone `deploy/public-gateway/compose.yaml` and runbook;
  it has no host port publication by default and joins the existing private
  control-plane network only when explicitly configured.
- Added delivery-only `/healthz` liveness for provider health checks; it returns
  no publication, database, or infrastructure details.
- Added deterministic first renderer adapters for Markdown documents and HTML
  web artifacts, preserving editable source, outputs, validation, and toolchain
  metadata.
- Extended deterministic renderer coverage to JSON visualizations, inert
  Mermaid-source diagrams, and report/Markdown output profiles.
- Added bounded PDF metadata/page-count inspection and DOCX-to-PDF conversion
  adapters with structured unsupported/error fallback when host tools are absent.
- Added secure immutable-version materialization with required host/session
  provenance, RustFS hash verification, traversal/symlink checks, atomic file
  replacement, and materialization audit rows.
- Exposed artifact build-job submit/get/cancel routes and initialized the job
  manager in control-plane startup.
- Added migration `0005_artifact_binding_context.sql` and extended the existing
  binding writer for explicit workspace, room, issue, and commit context.
- Added a bounded renderer sandbox primitive with executable allowlisting,
  scrubbed environment, timeout, working-directory isolation, and output caps.
- Extended the existing background worker with durable `FOR UPDATE SKIP LOCKED`
  artifact-job claiming and completed/failed terminal updates through an
  injected renderer/commit runner.
- Added `ArtifactRenderRunner`, which uploads source manifests and rendered
  outputs content-addressed to S3-compatible storage before committing an
  immutable CAS version, and wired it into the control-plane worker command
  when RustFS is configured.
- Connected renderer execution to artifact jobs so successful renders commit
  output/validation metadata through an injected version boundary.
- Added immutable-version review state with requested/approved/rejected
  terminal decisions and reviewer comments.
- Exposed artifact-scoped review request/completion/read routes and added
  review state to the Artifact Studio detail view.
- Added private-by-default artifact ACL policy with expiring grants, revoke,
  migration `0006_artifact_acl.sql`, and catalog/detail/download filtering
  before aggregation.
- Added authenticated-guest publication enforcement: guest audiences carry an
  explicit allowlist and the gateway returns 401/403 for missing or unauthorized
  guest identity while unlisted links remain token-only.
- Added a live-service publication policy boundary that accepts only registered
  service IDs and exact allowlisted methods/paths; arbitrary origin URLs,
  private IPs, and traversal paths are rejected.
- Added server-side artifact catalog filters for query, type, status, creator,
  pagination, with ACL filtering applied before results are returned.
- Durable ArtifactStore-backed catalog/detail reads now apply principal ACL
  predicates in PostgreSQL before pagination/aggregation, preserving restart
  visibility for durable artifacts.
- Added a dashboard Publications view with pinned version, audience, expiry,
  state, approve, and revoke controls using the existing publication routes.
- Added MIME-aware Artifact Studio previews: PDF iframe, raster image preview,
  sandboxed HTML, text/JSON open flow, and safe download fallback for DOCX and
  unknown binaries.
- Added explicit safe preview policy for PDF/images/Markdown/JSON, sandboxed
  HTML/SVG, and download-only DOCX/unknown binary handling.
- Added durable ACL grant/revoke callbacks and authenticated artifact ACL
  routes; PostgreSQL-backed deployments now persist ACL state through the same
  ArtifactStore adapter as jobs and reviews.
- Added startup hydration for ACL, review, and build-job projections from their
  PostgreSQL tables so restart does not erase workflow state.
- Added `herdr-eng artifacts upload`, a harness-friendly raw-file upload path to
  the shared control-plane registry with filename, MIME, session, and mission
  bindings.
- Added an integration workflow fixture covering create/render on Host A,
  immutable review approval, human publication approval, external gateway
  delivery of the pinned version, and successful revoke denial.
- The workflow fixture now exercises the actual public gateway HTTP server,
  proving an external GET receives the pinned output and receives 404 after
  publication revoke.
- Added durable callback wiring for review and build-job state transitions when
  the PostgreSQL/RustFS ArtifactStore adapter is active; standalone mode still
  runs safely without persistence.
- Wired session workspace attachments into existing `artifact_ids` message
  transport and preserved artifact references in native agent prompts without
  sending binary payloads.
- Revalidated and restored native attachment delivery after current-state
  audit found an older rejection branch; attachment IDs now become explicit
  non-executable `herdr-artifact://...` references in native prompts.
- Wired collaboration room messages to carry up to 20 validated artifact IDs
  through the existing authenticated message/idempotency contract; no binary
  payloads enter the collaboration event stream.
- Added tests first for migration contracts and streaming integrity/size
  behavior, registry/CAS/idempotency behavior, and legacy fake-S3 compatibility.

## Validation evidence

```text
27 passed in 4.61s
pytest -q tests/control_plane/test_db.py \
  tests/control_plane/test_artifacts.py \
  tests/control_plane/test_artifacts_api.py \
  tests/control_plane/test_artifact_registry_migration.py \
  tests/control_plane/test_artifact_versions.py \
  tests/control_plane/test_rustfs.py \
  tests/control_plane/test_rustfs_streaming.py
```

After the API slice, the focused artifact/API/storage subset passed 23 tests in
5.29 seconds.

The final local slice verification passed 29 tests in 5.59 seconds, with Ruff,
Python compilation, and conflict-marker checks passing. An independent
architect review approved this gated foundation slice;
approval does not cover production activation or live PostgreSQL/RustFS
deployment.

The publication/dashboard/job/adapter follow-up verification passed 21 focused Python
tests, 206 JavaScript client/component tests, 12 session/composer Playwright
tests, and JavaScript syntax checks.

The gateway/renderer follow-up verification passed 26 focused Python tests with
Ruff passing.

The durable public-delivery follow-up verification passed with Ruff and a live
resolver contract test.

The public-gateway CLI parses, compiles, and exposes the isolated command in
the control-plane help surface.

The current artifact source was overlaid into the running Python 3.12
control-plane container without replacing it; all artifact/gateway modules
imported successfully, and the container exposed both `public-gateway` and
`artifacts upload` CLI surfaces. The temporary probe tree was removed.

The current source was also exercised inside that running container against
the real PostgreSQL 16/RustFS services using a temporary schema, seeded Host B
and session identity, and a temporary bucket: logical artifact registration,
content-addressed source/output upload, CAS version commit, and verified Host B
materialization all succeeded. The schema and bucket were removed afterward.

The live worker probe then seeded a queued job with a real worker host identity
and executed the current `FOR UPDATE SKIP LOCKED` path: renderer output was
uploaded to the real S3-compatible store, an immutable version was committed,
and the PostgreSQL job row reached `completed` with its result version ID.

Final live acceptance fixture: Host A created/committed the artifact, Host B
materialized the verified output, Host C completed the review row, an approved
pinned version was delivered over the real HTTP gateway, and PostgreSQL revoke
caused the same external URL to return 404. All temporary schema/bucket/files
were removed afterward.

The same live temporary-schema probe then inserted an active pinned publication
row, served the real object through `PublicGatewayServer` over HTTP, revoked the
publication in PostgreSQL, and verified the same URL returned 404 afterward.
The temporary schema, object bucket, and probe tree were removed.

A second live temporary-schema probe exercised review completion, build-job
state persistence, and ACL grant persistence through the actual ArtifactStore
against PostgreSQL 16; each projection row was observed before schema cleanup.

The public-gateway compose file renders and validates successfully with
explicit operator-provided image/network/fleet variables.

After removing stale validation containers, the Docker daemon recovered. The
dependency-free validation image built successfully as
`herdr-artifact-validation:current`; current artifact/gateway modules imported
inside it, and both `control-plane public-gateway` and `artifacts upload` CLI
surfaces were verified.

The validation image was rebuilt with `PYTHONPATH=/app` so its console script
uses the current overlaid source. A temporary container then started
`control-plane public-gateway`; `/healthz` returned `{"status":"ok"}` and
`/api/v1/health` returned 404. The container was removed afterward.

An operator-authorized Tailscale Funnel trial ran on port 10000 without
overwriting existing 443/9447 services: the public HTTPS health URL returned
200 and the public `/api/v1/health` path returned 404. Funnel port 10000 and
the temporary gateway container were disabled/removed immediately afterward.

The gateway health/durable-delivery follow-up verification passed 4 tests with
Ruff passing.

The bundle/diff follow-up verification passed 3 tests with Ruff passing.

The materialization follow-up verification passed 6 artifact-version tests with
Ruff passing.

The job/binding follow-up verification passed 19 focused tests with Ruff
passing.

The renderer-sandbox follow-up verification passed 3 tests with Ruff passing.

The worker-job follow-up verification passed 3 worker tests with Ruff passing.

The renderer-worker follow-up verification passed 4 tests with Ruff and CLI
Python compilation passing.

The review/dashboard follow-up verification passed 22 focused Python tests and
206 JavaScript client/component tests with syntax checks passing.

The ACL follow-up verification passed 4 focused tests with Ruff passing.

The durable-ACL/API follow-up verification passed 21 focused tests; changed
artifact modules passed Ruff.

The catalog-search follow-up verification passed 3 API tests with Ruff passing.

The durable-catalog follow-up verification passed 20 API/version tests with
Ruff passing.

The publications-dashboard follow-up verification passed 206 JavaScript
client/component tests with JavaScript syntax checks passing.

The collaboration artifact-reference follow-up passed 5 focused message tests
with Ruff and JavaScript syntax checks passing.

The preview follow-up passed the same 206 client/component tests with syntax
checks passing.

The renderer-profile follow-up passed 4 tests with Ruff passing.

The document-tool follow-up passed 2 tests with Ruff passing.

Broad regression pass: 67 focused Python artifact/publication/worker/gateway
tests passed, and the JavaScript client/component suite passed 206 tests.

The guest-publication follow-up verification passed 8 gateway tests with Ruff
passing. Durable provider provisioning failures now persist an auditable
`failed` publication state instead of masking the original error; the
publication-failure regression suite passed 26 focused tests with Ruff.

The provider-selection follow-up verification passed 9 provider/publication
tests with Ruff passing.

The multi-provider follow-up verification passed 11 provider/publication tests
with CLI compilation passing.

The live-service publication policy follow-up passed 2 tests with Ruff.

The preview-policy follow-up verification passed 2 tests with Ruff passing.

The live S3-compatible object store passed a temporary bucket/object
write-read-delete round trip. Restart hydration follow-up verification passed
22 focused tests with Ruff passing.

The CLI upload command parses and displays successfully; its control-plane
transport remains private/authenticated by the existing host configuration.

All migrations `0001` through `0006` were executed successfully against the
running PostgreSQL 16.15 service inside a temporary schema and rolled back;
this validates DDL ordering, foreign keys, indexes, and JSONB types without
persisting validation tables or modifying application data.

The render/review follow-up verification passed 10 focused tests with Ruff
passing.

The end-to-end workflow fixture passed with Ruff, covering create → review →
approve → deliver → revoke without touching production networking.

The HTTP-level workflow follow-up passed 4 integration/gateway tests with Ruff
passing.

The durable-transition follow-up verification passed 25 focused tests with
Ruff passing.

The repository was already dirty before this work. Existing unrelated changes
were preserved. No production database migration, public provider, Tailscale
configuration, DNS, or public service was changed or activated.

The full Python suite was also started for regression checking. It reached
`1438 passed, 4 skipped, 1 xfailed` before it was stopped after 23 minutes;
two unrelated existing failures were observed in
`tests/control_plane/test_collab_tasks.py::test_many_agents_and_tasks_stay_fast`
and `tests/test_deploy_scripts.py::test_changed_collab_slice_force_recreates_collab_public`.
Neither failure imports or exercises the changed artifact storage/registry
modules.

## Deliberate decisions

- The migration is additive and leaves the legacy artifact upload/download
  contract untouched until the versioned API/store slice is implemented.
- Streaming uploads spool to a temporary file, not memory, because the current
  boto3-compatible path requires a seekable body for reliable large-object
  upload. The S3 object remains content-addressed by SHA-256.
- The implementation does not vendor Anthropic skill source or assets; only the
  high-level workflow is distilled into the planned native Herdr skill.

## Remaining work and limits

- Legacy upload/download callers remain supported alongside the versioned
  registry API by design.
- The current source and validation image have been exercised against the live
  PostgreSQL 16/RustFS services using temporary schemas/buckets; production
  deployment still requires the normal operator image rollout.
- Public provider activation, public DNS, and production Tailscale/Cloudflare
  changes remain intentionally unactivated because they require explicit
  operator authorization. Private-only mode is the default and is verified.
- The full Python suite also contains two unrelated pre-existing failures
  outside the artifact surfaces; focused artifact/publication/worker/gateway
  and JavaScript suites pass.
