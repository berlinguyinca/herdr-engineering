# API v1 proposal (adapt to existing route scheme after audit)

Authenticated internal:
- `POST /api/v1/artifacts/uploads/init` (name, type, size, expected SHA, workspace, idempotency)
- `PUT /api/v1/artifacts/uploads/{upload_id}/parts/...`; `POST .../complete` (validated blob)
- `GET /api/v1/artifacts?filters...` ACL-filtered pagination
- `POST /api/v1/artifacts` register logical ID and initial source; `GET /api/v1/artifacts/{id}` metadata, current version
- `GET /api/v1/artifacts/{id}/versions` and `GET .../versions/{version_id}`
- `POST /api/v1/artifacts/{id}/versions` with `base_version_id`, source blob/rendition refs and idempotency; 409 on stale head
- `POST /api/v1/artifacts/{id}/materializations` scoped host/session; explicit authorization
- `POST /api/v1/artifact-jobs` / `GET .../{job_id}` / `POST .../{job_id}/cancel`
- `POST /api/v1/artifact-collections` and `POST .../{id}/revisions`
- `POST /api/v1/publications/requests` → pending; `POST .../{id}/approve`, `/revoke`, `/suspend` (requires named rights)
- `GET /api/v1/publications` / `GET .../{id}/status` include desired vs observed
- `GET /api/v1/exposure-providers/capabilities` redacted, health-aware

Public data plane:
- `GET|HEAD https://edge-host/s/{opaque-token}/...` approved pinned static snapshot, deny on revoke; token stored hashed at rest, URL no auth cookies; scoped non-indexed links use noindex.
- Live host route proxy only approved methods/path; protocol upgrades only when explicitly authorized.

Error envelope `{code,message,request_id,details}`. Distinguish unauthenticated 401, unauthorized 403 (avoid leaking private artifact existence), stale version 409, precondition 412, quota 429, unavailable provider 503. Enforce server-side permissions regardless of UI.

Event topics (integrate existing event bus, not a new bus): `artifact.created`, `artifact.version.published`, `artifact.review.requested`, `artifact.review.completed`, `artifact.dependency.changed`, `artifact.job.failed`, `publication.requested`, `publication.approved`, `publication.activated`, `publication.revoked`, `publication.drift_detected`. Consumers must be idempotent and versioned.
