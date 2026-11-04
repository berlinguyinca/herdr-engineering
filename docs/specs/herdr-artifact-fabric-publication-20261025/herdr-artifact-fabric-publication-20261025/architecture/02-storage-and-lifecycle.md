# Storage, identities, versioning and recovery

## Schema extension guidance (migrate existing tables, never blindly replace)
- `artifacts`: stable `artifact_id`, tenant/fabric/workspace ownership, logical name, type, current_version_id, lifecycle/retention, timestamps.
- `artifact_versions`: `version_id`, `artifact_id`, monotonic sequence, parent version(s), source_manifest_blob, output blobs, sha256, mime, created_by principal, agent ID, host ID, job ID, committed at, quality and review metadata; unique `(artifact_id,version_number)`.
- `artifact_blobs`: backend, content hash, object key, length, media type, verified time; composite scope policy to prevent cross-tenant hash leakage.
- Existing `artifact_bindings` and `artifact_materializations` retained and extended with workspace, room, issue, commit, graph snapshot where appropriate; no guessed identifier joins.
- `artifact_collections` and pinned collection revisions containing exact artifact version IDs.
- `artifact_reviews`, `artifact_comments`, `artifact_dependencies`, `artifact_build_jobs`, `artifact_audit` (append-only).
- `publications`, `publication_approvals`, `publication_routes`, `publication_events` kept separate from artifact lifecycle.

## State transitions
DRAFT → BUILDING → VALIDATING → APPROVED/READY → INTERNAL_PUBLISHED → SUPERSEDED; FAILED/REJECTED are terminal per build attempt and auditable. Public publication is orthogonal: REQUESTED → APPROVED → PROVISIONING → ACTIVE → SUSPENDED/EXPIRED/REVOKING → REVOKED. Never promote failed build or rejected review.

## Atomicity and concurrency
Upload and verify immutable payload, then transactional metadata update. CAS on base version/current version (HTTP If-Match or explicit `base_version_id`); mismatches yield conflict with latest version info. Never auto-merge binary files; text merge via explicit agent workflow/review. Retry with idempotency key; duplicate requests produce same result, not duplicate published versions. Orphan blobs cleaned conservatively after grace period, only when no live references. Crash after upload must not expose incomplete metadata.

## Upload/read operations
Stream/validate by content, enforce file count/size limits, support multipart resumable uploads. Short-lived scoped upload or download capability only; no permanent S3 credential to agents. Verify SHA-256, object availability and size. Do not hold entire multi-GB files in RAM (existing `register(data: bytes)` needs streaming path). Materialization must enforce authorized target, atomically place verified bytes and avoid path traversal/symlink escapes. Capture originating host as provenance, not as durable storage locator.

## Search and retention
Start with PostgreSQL indexed metadata and extracted text under ACL; avoid embedding sensitive text into unfiltered global retrieval. Pin collections and published versions; GC only after expiry/hold rules, never based only on blob age. Public share revocation denies future gateway requests, not recall of downloaded copies or third-party caches.
