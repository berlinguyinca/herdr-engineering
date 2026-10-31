# Herdr Artifact Fabric Publication — integration audit

Date: 2026-10-25  
Repository: `berlinguyinca/herdr-engineering`  
Execution host: `bender`  
HEAD: `88c4d5e9efe700f9641bc408e5b3504fc124c0b0` (`main`)

The specification bundle was transferred from the discovered Mac host `mac.lan`
as SSH user `wohlgemuth`, verified with SHA-256
`f9215bcfd2a66c15a9c924d58394cd5db6afb24e12bf5b27cc9aa654bc079743`, and
extracted at:

`docs/specs/herdr-artifact-fabric-publication-20261025/herdr-artifact-fabric-publication-20261025/`

The ZIP test passed and all 17 entries in `MANIFEST.sha256.json` match.

## Repository state and overlap

- The checkout is `main`, 609 commits behind `origin/main`.
- The worktree already contains unrelated user changes, including `.gitignore`,
  `cleanmainwt/`, existing system-graph specifications, and superpowers
  documents. They must not be reverted or folded into this feature.
- Numerous branches/worktrees touch collaboration, workspace, fabric, and UI
  code. The audit found no existing artifact-version/publication implementation
  beyond the Phase-1 artifact code described below. Relevant active surfaces
  include `feat/collab-settings`, `feat/collab-data-platform`,
  `feat/session-workspace`, `feat/fabric-parent`, and `fix/fabric-forward-host`.
- Artifact history is limited to `9e85568` (Phase-1 control-plane foundation)
  and `782516d` (artifact model/bindings/materialization). There is no existing
  versioned registry branch to merge or copy.

## Existing components to extend

### Registry, storage, and persistence

- `herdr_engineering/control_plane/artifacts.py` contains `ArtifactStore` with
  `register`, `get`, `download`, `attach`, `materialize`, and
  `mark_available`. It writes one row per logical upload and currently accepts
  `bytes`, hashes them in memory, writes to RustFS, then inserts metadata.
- `herdr_engineering/control_plane/rustfs.py` contains the S3-compatible
  `RustFSClient`. It already uses content-addressed SHA-256 keys and verifies
  reads, but `put` and `get` are whole-buffer operations.
- `deploy/control-plane/migrations/0001_control_plane.sql` owns the existing
  PostgreSQL tables `artifacts`, `artifact_bindings`, and
  `artifact_materializations`. It does not contain artifact versions, blob
  records, ACLs, jobs, reviews, publications, or publication audit tables.
- `herdr_engineering/control_plane/db.py` applies ordered SQL migrations in a
  transaction and is the correct migration entry point.
- `herdr_engineering/control_plane/repo.py` is the testable in-memory/event
  projection used by the dashboard. It stores artifact metadata in
  `_artifacts`, payload bytes in `_blobs`, and records `ArtifactRegistered`
  events. Its state export/import and `bridge.py` replay paths must be extended
  without putting large bytes in events.
- `herdr_engineering/control_plane/bridge.py` explicitly reconstructs artifact
  metadata from `fabric_events`; object bytes are intentionally external.

### API and authorization

- `herdr_engineering/control_plane/api.py` exposes raw-body
  `POST /api/v1/artifacts` and
  `GET /api/v1/artifacts/{id}/download`. With a RustFS client, downloads read
  from object storage; without one, the in-memory repo is used.
- The API already has viewer/operator/ingest roles, request rate limiting,
  bounded JSON bodies, and fail-closed non-loopback binding checks. New
  artifact routes must use this authorization boundary and must not accept
  claimed owner or approver fields from request JSON.
- The current upload path is a compatibility surface. It must remain valid while
  new versioned upload/commit routes are added or the route is explicitly
  adapted with a compatibility response.

### Collaboration, workspaces, and dashboard

- `packages/conversation-composer.js` already has the correct generic upload
  adapter and emits artifact objects to `onSend`, but
  `packages/session-workspace.js` deliberately rejects attachments because
  native session transport cannot deliver files.
- `packages/collab-view.js` and `herdr_engineering/control_plane/collab.py`
  implement rooms, agent identities, approvals, tasks, and public collaboration
  isolation. Artifact references should be added to the existing message/event
  contracts; binary bytes must not enter collaboration event streams.
- The dashboard is a vanilla ES-module/hash-routed SPA, not React. Navigation
  is defined in `packages/app.js`; shared tokens/components live in
  `packages/base.css`, `packages/theme.css`, `packages/components.js`, and
  `packages/herdr-web-components/`. Artifact Studio must reuse these patterns
  and add routes rather than introducing a framework.
- `packages/session-workspace.js` is the contextual integration point for
  session-linked artifacts; mission and graph views already provide contextual
  links that can be extended with artifact IDs.

### Fabric, services, and scheduling

- `herdr_engineering/devfabric.py`, `fabric.py`, and `fabric_agent.py` provide
  private host/service registration, routing, probing, leases, and host-aware
  service identity. They are not a public artifact gateway and must not be
  exposed directly.
- `herdr_engineering/control_plane/service_catalog.py` and the `services`
  migration provide registered service IDs, host ownership, route metadata, and
  health. Live publication must resolve an approved service ID through this
  catalog; it must never accept arbitrary origin URLs.
- `pi-engineering/src/sched/Scheduler.ts` supplies bounded weighted-fair
  concurrency and backpressure. It is the reusable scheduling primitive for
  render/build jobs; it is not a reason to add a second scheduler.
- The Pi runtime’s `src/artifacts/ArtifactStore.ts` is a hardened local,
  filesystem-backed immutable evidence store with lock-domain and symlink
  protections. It is useful as a security/reference pattern for checkout and
  local materialization, but it is not a replacement for Herdr’s PostgreSQL +
  RustFS registry and must not be copied into this repository.

### Deployment and network boundaries

- `deploy/control-plane/README.md` documents PostgreSQL plus a `minio/minio`
  S3-compatible container currently standing in for RustFS. The Python client is
  boto3/S3-agnostic, so a real RustFS deployment remains configuration-level
  compatible, but the live image must be verified separately.
- `deploy/control-plane/compose.collab.yaml`, `collab-edge/nginx.conf`,
  `collab-only` mode, and `COLLAB.md` already establish a separate public
  collaboration data plane with no database/outbound access in the public
  container. This is the closest reusable isolation pattern for the future
  artifact gateway, but it cannot be widened into a generic control-plane proxy.
- `deploy/control-plane/collab-tailscale-serve.sh` is a private Tailscale Serve
  helper. It is not Funnel and does not authorize public publication. A future
  Funnel adapter must be opt-in and provider-neutral.
- `deploy/fabric-stack/` is the private dev-fabric gateway and DNS stack. It
  must remain private-only unless an explicit, separately authorized exposure
  configuration is added.

## External artifact tooling review

Anthropic’s public `anthropics/skills` repository documents `web-artifacts-builder`
as a React/TypeScript/Vite/Tailwind/Parcel workflow: initialize, develop, bundle
to a self-contained HTML file, and optionally test/visualize. The repository’s
README identifies many example skills as Apache-2.0, but the web-artifacts skill
itself declares `LICENSE.txt`; the current Anthropic license text reserves the
materials, prohibits extracting/retaining copies outside authorized use, and
prohibits reproduction and derivative works. Therefore this implementation will
reuse only the high-level workflow ideas (source preservation, deterministic
bundle output, render/test validation), not upstream source, prompts, assets, or
generated project files. The native `herdr-artifact` skill will be authored in
this repository under its own license and will call Herdr APIs/CLI.

## Integration decisions and conflicts

1. **Single authority:** PostgreSQL metadata and ACLs remain authoritative;
   RustFS stores immutable blobs; the in-memory repo remains a test/durable-event
   projection, not a second production registry.
2. **Compatibility first:** retain the existing upload/download behavior and
   `ArtifactStore` methods while adding streaming and version-aware APIs behind
   explicit new methods/routes.
3. **No bytes in events:** event payloads carry IDs, hashes, provenance, and
   state transitions only.
4. **CAS edits:** every new version commits against `base_version_id` and
   returns a conflict with current head metadata when stale.
5. **Publication isolation:** public delivery gets its own policy/data-plane
   boundary and approved immutable snapshot. It never exposes control-plane
   routes, RustFS credentials/origin, or arbitrary private addresses.
6. **Private by default:** no provider configured is a supported state; this
   phase will not alter production Tailscale/Funnel/DNS configuration.
7. **First vertical slice:** implement durable registry/version/blob metadata,
   streaming verification, CAS commits, source/output manifest preservation,
   CLI/skill contract, and focused cross-host/concurrency tests before dashboard
   or public gateway work.

## Phase-1 file plan

- Modify `deploy/control-plane/migrations/0001_control_plane.sql` only through
  a new ordered migration for version/blob/provenance/job/audit tables and
  indexes; do not rewrite existing tables destructively.
- Extend `herdr_engineering/control_plane/rustfs.py` with streaming upload/read
  helpers and bounded verification while preserving `put`/`get` compatibility.
- Extend `herdr_engineering/control_plane/artifacts.py` with logical artifact,
  immutable version, source/output manifest, CAS, idempotency, and scoped
  materialization operations.
- Extend `herdr_engineering/control_plane/repo.py`, `bridge.py`, and event
  tests with version/head projections and conflict behavior; keep test payload
  fallback bounded and explicit.
- Extend `herdr_engineering/control_plane/api.py` with compatible upload-init,
  completion, version/history, and materialization endpoints using existing
  role checks and error conventions.
- Add a native `skills/herdr-artifact/SKILL.md` plus a small CLI wrapper using
  existing CLI registration, with `create`, `checkout`, `inspect`, `edit`,
  `validate`, `publish --base-version`, `history`, `diff`, `materialize`,
  `bundle`, and `share-request` contract coverage. Public activation remains
  unavailable until the later publication phase.
- Add focused Python tests, migration tests, API compatibility tests, CAS race
  tests, bounded streaming tests, and security/path validation tests.

## Known unknowns to resolve during implementation

- The deployed object store is currently documented as MinIO, not proven
  RustFS; test against the S3 interface and leave image/provider selection to
  deployment configuration.
- Live external guest identity and publication approver RBAC are not yet a
  general control-plane contract; phase 1 records trusted internal principals
  and defers public guest policy to the publication phase.
- No live cross-host test harness is configured in this checkout. The first
  vertical slice will use two independent clients/processes against one
  durable backend and a transport-level cross-host fixture; live multi-host
  execution remains an explicit validation gap until hosts are available.

