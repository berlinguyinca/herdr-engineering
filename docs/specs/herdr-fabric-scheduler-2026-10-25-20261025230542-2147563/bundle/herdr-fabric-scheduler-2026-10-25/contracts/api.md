# Proposed API contracts — adapt to actual existing endpoints
These are **behavioral contracts**, not mandate to create duplicate `/api` routes. During Phase 0 map to existing control-plane `/api/v1/...` and event transport. Authentication/authorization follows existing operator/room boundaries. All mutating calls require owner/operator rights, idempotency key and audit.

`GET hosts`: host UID, aliases, health/freshness, status, policy, capabilities, telemetry summary, active/reserved counts (scope-filtered).
`PATCH host-policy`: host_uid, expected_revision, mode, allow/deny lists, reason, expiry => authoritative revision. No silent conversion from legacy auto_agents false to true.
`POST placement/plan`: request/task snapshot => dry-run ranked candidates, exclusions {host_uid, reason_codes}, measured values, policy/telemetry generations, ttl.
`POST placement/reservations`: request_id/idempotency_key, request spec => reservation_id, host_uid, expiry, generation or `queued` with reasons. Atomic.
`POST placement/commit`: reservation_id, host revision => launch state and execution lease. Must recheck eligibility.
`POST placement/release`: reservation_id + terminal state or idempotent cancellation.
`GET placement/decisions/{id}`: decision evidence, authorization-scoped host details. Public guests never see private host topology.
`GET placement/queue`: pagination, project/team filters; bounded privacy and sorting.
`POST migration/plan` and `POST migration/execute`: restricted, checkpoint capability required, idempotent lease transitions.
`GET simulator/replay` and `POST simulator/preview`: sanitized historical simulation, no mutations.

## Standard reason codes
`HOST_MANUAL_ONLY`, `HOST_DRAINING`, `HOST_DISABLED`, `HOST_QUARANTINED`, `HOST_STALE`, `HOST_UNREACHABLE`, `HOST_UNTRUSTED`, `HOST_NOT_OWNED`, `FAMILY_UNSUPPORTED`, `CPU_PRESSURE`, `MEMORY_PRESSURE`, `IO_PRESSURE`, `DISK_LOW`, `RESERVATION_CAPACITY`, `PROJECT_QUOTA`, `USER_QUOTA`, `WORKSPACE_LOCK`, `DATA_ACCESS_DENIED`, `POLICY_CHANGED`, `CAPABILITY_UNKNOWN`, `EXECUTION_UNCERTAIN`.

Events with stable ids and dedup: `host_telemetry`, `host_policy_changed`, `placement_planned`, `placement_queued`, `placement_reserved`, `placement_committed`, `agent_started`, `agent_draining`, `migration_requested`, `migration_completed`, `migration_blocked`, `reconcile_needed`, `placement_failed`. Redact secrets and protect topology.
