# Fabric Resource Scheduler operations

The scheduler owns placement of Herdr-controlled local agents. InferWeave remains the authority for model inference and GPU worker placement; scheduler GPU fields are diagnostic/local-capability constraints only.

## Safety defaults

- macOS hosts are `manual_only` by default, including aliases, reconnects, AC power, and child launches.
- `disabled`, `draining`, stale, unreachable, untrusted, conflicting-identity, and unowned hosts are not automatic candidates.
- A parent host is lineage metadata, not an implicit child placement pin.
- External collaborators can appear in the graph but never become owned scheduler hosts.
- Automatic migration remains unavailable until runtime checkpoint/resume and single-writer fencing are certified.

## Admission configuration and persistence

The central fleet config's `control_plane` section accepts
`scheduler_admission: false` (the default) and `scheduler_limits`. Limit keys
include `reservation_ttl`, `owner_queue_limit`, `project_queue_limit`,
`global_queue_limit`, `owner_concurrency_limit`, `project_concurrency_limit`,
`mission_concurrency_limit`, `owner_cpu_limit`, `owner_memory_limit`,
`maximum_spawn_depth`, `parent_concurrency_limit`, and `parent_burst_limit`.
These settings stay on the server. The host-visible `scheduler` section accepts
`telemetry: false` to opt out of resource collection. Defaults and bounds are
defined by `SchedulerService`; preview requests cannot override operator limits.

The existing fleet host loop posts observations to
`POST /api/v1/fleet/config/telemetry` using `X-Host-Token`. An operator API key
does not authenticate a host report. Generic ingest cannot grant ownership,
trust, stable UID, capacity, or operator policy. Reports use the acknowledged
event store before becoming eligible capacity. Observations and policies
refresh during admission, and their original capture timestamps expire after
60 seconds. Ordinary liveness reports do not renew resource freshness.

PostgreSQL admission serializes reservations, queue caps, policy checks, lease
transitions, and execution claims across replicas. Queue caps count persisted
rows; lower caps retain existing entries and allow retry/draining. A claim
requires a live matching execution lease and a final capacity/policy check.
Receipts and subscriber notifications become visible after transaction commit.
Commit failure restores prior policy, reservation, and lease projections.
Backups wait for in-flight admission before copying scheduler state; a backup
inside the same uncommitted admission is rejected. Refreshed peer observations,
reservations, and leases are retained in the scheduler snapshot.
It marks submission ownership; it does not prove a native process is running.
Native launch adapters and supervised process reconciliation remain separate
rollout gates.

Active legacy reservations without a saved request context produce
`LEGACY_CONTEXT_UNKNOWN` and block new placement until reconciled. Reserved
entries may expire or be cancelled; committed entries require verified process
reconciliation. Never invent owner/project/workspace attribution or delete the
audit log to bypass this gate. Historical client policy envelopes are ignored
as operator receipts; contaminated policy snapshots are disabled pending review.
Audit historical host ownership and obtain fresh token-bound observations
before enabling admission for any fleet host.

## Rollout

1. Snapshot config and sessions; run the existing pytest/Node/Playwright commands and record baseline queue/launch metrics.
2. Enable stable identity/policy and telemetry shadow collection. Verify Mac aliases remain manual-only and stale hosts are visibly distinct from zero load.
3. Use `POST /api/v1/scheduler/plan` for previews while admission remains disabled.
4. Set `control_plane.scheduler_admission=true` for canary requests. Verify idempotency, policy and capacity rechecks, queue limits, and rollback before wiring native launches.
5. Integrate each native launch family only after its launch, stop, ownership, and reconciliation contracts pass. This admission increment does not enable all-spawn routing.
6. Enable quotas/workspace preparation and then the scheduling dashboard. Keep migration disabled.
7. Run overload, network-partition, stale-host, 100-concurrent, and cross-host parent/child tests before any recovery pilot.

## Rollback

Set `control_plane.scheduler_admission=false` and restart the control plane
using the existing deployment procedure. Server admission and limit values are
read at startup; changing the file alone does not toggle a running server.
New reservations, commit, claim,
and retry return 503. Preview, cancellation of unclaimed work, and maintenance
of existing leases remain available. Preserve telemetry and audit history;
existing running agents are not killed. Claimed executions retain capacity
until a runtime provides verified stop evidence. Never roll back by deleting
reservations, leases, checkpoints, or audit events.

## Troubleshooting

Checkpoints now publish as one immutable private `HERDRCP1` container named
`<checkpoint-id>.checkpoint`, with metadata and payload committed together.
The default payload limit is 256 MiB. Duplicate IDs may replay identical bytes
but cannot replace an existing checkpoint with different contents. Recovery
callers should pass `expected_lease` to validate agent, host, and generation.
The store pins a private directory descriptor and rejects symlink/nonregular
checkpoint entries. Legacy experimental `.json`/`.bin` pairs remain untouched;
preserve them for explicit verified conversion rather than treating them as
current checkpoint containers. This storage change does not enable migration.

Linux supervised workloads can use `SystemdExecutionScope` with the existing
collaboration sandbox. It owns a named systemd service with process, memory,
and CPU limits (512 tasks, 8 GiB, 200% CPU by default). The external-scope marker
keeps bubblewrap and its descendants in that cgroup instead of creating a second
scope. Receipt identity includes the lease-derived unit name, systemd invocation,
actual cgroup, and control-group kill mode. A stop is verified only after that
bound service stops and the exact cgroup is empty or gone. Failed start identity
acquisition attempts cleanup and returns reconciliation metadata.

Run the real no-model smoke test with `HERDR_SCHEDULER_SCOPE_LIVE=1` against
`tests/test_execution_scope_live.py`. It starts isolated parent/child writers
and verifies both stop writing after cgroup revocation. This execution boundary
does not certify interactive harness checkpoint/resume or enable migration.

- `HOST_STALE`: inspect `/api/v1/hosts/<host>/telemetry`; never substitute a fabricated zero-load value.
- `HOST_MANUAL_ONLY`: use an explicit authorized manual host selection; this does not permit disabled/untrusted/unreachable hosts.
- `POLICY_CHANGED`: the host changed after ranking; retry only through `plan`/`reserve`, never by reusing the old reservation.
- `EXECUTION_UNCERTAIN`: reconcile the native session/lease before retrying; a disconnected SSH process is not proof the agent stopped.
- `CHECKPOINT_UNSUPPORTED` or `CHECKPOINT_REQUIRED`: leave the agent pinned/draining; do not kill-and-restart and call it migration.

