# Fabric Resource Scheduler operations

The scheduler owns placement of Herdr-controlled local agents. InferWeave remains the authority for model inference and GPU worker placement; scheduler GPU fields are diagnostic/local-capability constraints only.

## Safety defaults

- macOS hosts are `manual_only` by default, including aliases, reconnects, AC power, and child launches.
- `disabled`, `draining`, stale, unreachable, untrusted, conflicting-identity, and unowned hosts are not automatic candidates.
- A parent host is lineage metadata, not an implicit child placement pin.
- External collaborators can appear in the graph but never become owned scheduler hosts.
- `scheduler.migration` stays `false` unless the runtime family has a verified checkpoint/resume contract and single-writer fence tests.

## Rollout

1. Snapshot config and sessions; run the existing pytest/Node/Playwright commands and record baseline queue/launch metrics.
2. Enable stable identity/policy and telemetry shadow collection. Verify Mac aliases remain manual-only and stale hosts are visibly distinct from zero load.
3. Enable `plan_only`; compare structured explanations against the old placement path without reserving.
4. Enable `admission` for one collaboration canary. Confirm reservation idempotency, policy-generation recheck, release on launch failure, and no duplicate execution.
5. Enable `all_spawns` one adapter family at a time after its launch-path contract tests pass.
6. Enable quotas/workspace preparation and then the scheduling dashboard. Keep migration disabled.
7. Run overload, network-partition, stale-host, 100-concurrent, and cross-host parent/child tests before any recovery pilot.

## Rollback

Set `scheduler.all_spawns=false`, `scheduler.admission=false`, and `scheduler.plan_only=true`; preserve telemetry and audit history. Existing running agents are not killed. Drain queued reservations through release/cancel, inspect `placement_decisions` and execution leases, and return policy mutations using the recorded expected revision. Never roll back by deleting reservations, leases, checkpoints, or audit events.

## Troubleshooting

- `HOST_STALE`: inspect `/api/v1/hosts/<host>/telemetry`; never substitute a fabricated zero-load value.
- `HOST_MANUAL_ONLY`: use an explicit authorized manual host selection; this does not permit disabled/untrusted/unreachable hosts.
- `POLICY_CHANGED`: the host changed after ranking; retry only through `plan`/`reserve`, never by reusing the old reservation.
- `EXECUTION_UNCERTAIN`: reconcile the native session/lease before retrying; a disconnected SSH process is not proof the agent stopped.
- `CHECKPOINT_UNSUPPORTED` or `CHECKPOINT_REQUIRED`: leave the agent pinned/draining; do not kill-and-restart and call it migration.

