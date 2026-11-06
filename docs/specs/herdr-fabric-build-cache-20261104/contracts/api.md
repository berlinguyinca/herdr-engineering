# API/event/CLI contract (adapt to existing conventions)
Routes are proposals: audit actual `/api/v1` naming, authentication, streaming and pagination first.

GET /api/v1/build-cache/status: aggregate health, backend capability, configuration *without secrets*, age and errors
GET /api/v1/build-cache/hosts: paginated host capability/cache occupancy/writes, freshness
GET /api/v1/build-cache/builds: paginated build records, filters by mission/session/host/repository, redacted commands
GET /api/v1/build-cache/builds/{id}: lifecycle, inputs digest, execution backend, cache counters, exit evidence
GET /api/v1/build-cache/policies: effective policy + its source and scope
POST /api/v1/build-cache/cleanup/preview: operator only; show candidate IDs and reason, no mutation
POST /api/v1/build-cache/cleanup/execute: authorized operator with preview token, bounded scope and idempotency key
POST /api/v1/build-cache/builds/{id}/cancel: authorized operator, bounded ownership and structured reason
SSE: build_cache.build_started / build_finished / cache_stats_updated / storage_degraded / lease_changed / cleanup_completed. Follow existing event envelope, resume and auth conventions.

Build identity: UUID, repository ID, source revision, dirty-tree input fingerprint where supported, toolchain digest, target triple, profile, features, trust domain, command classification, attempt ID, owner session and mission IDs, host+job IDs, timestamps, terminal status. Never record raw secrets from env or command-line arguments.

CLI proposals: `herdr-eng build-cache status`, `... diagnose --host`, `... cleanup --dry-run`, `... benchmark --profile`. Extend existing CLI commands, don't duplicate `build-coordinate`.

Authorization: read-only for appropriately scoped viewers; mutations only authorized operators; logs and remote toolchain credentials redacted; deny cross-tenant cache access by default.
