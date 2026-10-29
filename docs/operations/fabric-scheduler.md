# Fabric Resource Scheduler operations

The scheduler owns placement of Herdr-controlled local agents. InferWeave remains the authority for model inference and GPU worker placement; scheduler GPU fields are diagnostic/local-capability constraints only.

## Safety defaults

- macOS hosts are `manual_only` by default, including aliases, reconnects, AC power, and child launches.
- `disabled`, `draining`, stale, unreachable, untrusted, conflicting-identity, and unowned hosts are not automatic candidates.
- A parent host is lineage metadata, not an implicit child placement pin.
- External collaborators can appear in the graph but never become owned scheduler hosts.
- `scheduler.migration` stays `false` unless the runtime family has a verified checkpoint/resume contract and single-writer fence tests.

## Rollout

Authenticated resource reports use the `scheduler_observation` event stream.
The control plane acknowledges storage before changing capacity; generic ingest
cannot write this stream. Admission and dashboard reads refresh the latest host
facts and operator policies under the existing PostgreSQL admission lock, so a
restart or a second replica cannot reset sequence/identity checks or silently
use different capacity. Resource reports preserve newer operator policy
revisions. Capture timestamps still expire normally; replay never renews a
host's freshness. In-memory-only operation remains suitable for tests and
preview, while native execution requires the acknowledged durable sink.
Backups include the committed authenticated observation map, including facts
loaded from another replica. Restore applies this snapshot before local event
replay and ignores older sample sequences. Ordinary CPU/memory heartbeats have
their own history and cannot renew resource freshness or reset its watermark.

1. Snapshot config and sessions; run the existing pytest/Node/Playwright commands and record baseline queue/launch metrics.
2. Enable stable identity/policy and telemetry shadow collection. Verify Mac aliases remain manual-only and stale hosts are visibly distinct from zero load.
3. Enable `plan_only`; compare structured explanations against the old placement path without reserving.
4. Enable `admission` for one collaboration canary. Confirm reservation idempotency, policy-generation recheck, release on launch failure, and no duplicate execution.
5. Enable `all_spawns` one adapter family at a time after its launch-path contract tests pass.
6. Enable quotas/workspace preparation and then the scheduling dashboard. Keep migration disabled.
7. Run overload, network-partition, stale-host, 100-concurrent, and cross-host parent/child tests before any recovery pilot.

## Rollback

The fleet control-plane configuration accepts `scheduler_admission: true` for
automatic queued-reservation admission and `native_scheduler_launcher: true`
for the host-side native launcher. Both default off. Native execution requires
a prepared workspace record for the selected UID and owner, plus a saved Herdr
machine route. The operator-only `POST /api/v1/scheduler/launch` uses these
records and claims execution before starting a pane. Container deployments
without Herdr machine access must keep the native launcher off.

`control_plane.scheduler_limits` can set `owner_concurrency_limit` (default 20),
`project_concurrency_limit` (50), `owner_queue_limit` (100),
`project_queue_limit` (100), `global_queue_limit` (1000), and
`reservation_ttl` (90 seconds). These settings are deployment-owned and persist
through scheduler refresh/retry. Workload class defaults supply CPU, RAM, and
disk demand when omitted; a manual host pin still obeys the same active budgets.
Queue caps count persisted queued reservations under the admission lock, so
restarts and additional replicas do not reset them. Lowering a cap preserves
existing entries and permits retry/draining. Cancellation or successful
admission frees a slot; immediately placeable work does not consume a queue
slot. The control plane no longer keeps a second process-local queue counter.

Additional limits include `mission_concurrency_limit` (20), `owner_cpu_limit`
(32 cores), `owner_memory_limit` (64 GiB), `maximum_spawn_depth` (3),
`parent_concurrency_limit` (8), and `parent_burst_limit` (20 child requests per
10 minutes). The scheduler derives child depth and inherited mission/project
from persisted parent requests. An unknown parent requires authoritative context
registration/integration; caller-supplied depth does not authorize a bypass.

Set `scheduler.all_spawns=false`, `scheduler.admission=false`, and `scheduler.plan_only=true`; preserve telemetry and audit history. Existing running agents are not killed. Drain queued reservations through release/cancel, inspect `placement_decisions` and execution leases, and return policy mutations using the recorded expected revision. Never roll back by deleting reservations, leases, checkpoints, or audit events.

## Troubleshooting

- `HOST_STALE`: inspect `/api/v1/hosts/<host>/telemetry`; never substitute a fabricated zero-load value.
- `HOST_MANUAL_ONLY`: use an explicit authorized manual host selection; this does not permit disabled/untrusted/unreachable hosts.
- `POLICY_CHANGED`: the host changed after ranking; retry only through `plan`/`reserve`, never by reusing the old reservation.
- `EXECUTION_UNCERTAIN`: reconcile the native session/lease before retrying; a disconnected SSH process is not proof the agent stopped.
- `CHECKPOINT_UNSUPPORTED` or `CHECKPOINT_REQUIRED`: leave the agent pinned/draining; do not kill-and-restart and call it migration.

## Recovery implementation and checkpoint format

Recovery execution defaults to `migration_enabled=false`. A capability declaration
alone does not enable it: the coordinator also requires a version-matched runtime
adapter and an acknowledged recovery journal. The adapter supplies quiescence,
checkpoint capture, supervised stop, authoritative fencing, destination lease
acquisition, restore, and heartbeat/workspace/single-writer verification.

Each transition commits before its side effect. PostgreSQL recovery uses a
dedicated session advisory lock, separate from the event writer connection, so
the lock serializes an operation without keeping journal entries uncommitted.
Interrupted operations return reconciling; retries do not repeat uncertain restores.
The fault inventory lists pending acceptance cases and is not itself test evidence.

Private checkpoints now use immutable single-file `HERDRCP1` containers named
`<checkpoint-id>.checkpoint` (default payload limit 256 MiB). Metadata and payload
publish together. Reusing an ID with different contents is rejected. The prior
experimental `.json`/`.bin` pairs are not read or overwritten by this implementation;
preserve any such files for an explicit verified conversion rather than treating
them as current checkpoints. Retrieval for recovery validates the source lease.

Scope stop proof checks the actual systemd cgroup, invocation ID, and control-group
kill mode. A start whose identity cannot be obtained returns `ScopeStartUncertain`
with unit identity and cleanup-attempt status, and requires reconciliation.

