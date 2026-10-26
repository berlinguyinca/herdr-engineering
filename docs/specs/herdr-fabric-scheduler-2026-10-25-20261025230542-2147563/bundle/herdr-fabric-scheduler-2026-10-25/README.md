# Herdr Fabric Resource Scheduler — implementation bundle
Date: 2026-10-25 | Target: `berlinguyinca/herdr-engineering` | Status: implementation specification, not deployed.

## Objective
Every Herdr-controlled new agent, including delegated and recursively spawned agents, is automatically placed on a healthy eligible host using real capacity, reservations, fairness and placement policy. Owner's Mac is **manual-only**, never an implicit target. Preserve current Herdr workspace, session, collaboration, and dashboard semantics. Eventually rebalance checkpointable workloads safely, with no duplicate execution. No parallel orchestrator.

## Ordered execution
0. Inventory the live repo and establish invariants (00).
1. Hard eligibility, host identity and exclusions (01) — safety first.
2. Telemetry, freshness, capacity and process accounting (02).
3. Single placement decision/admission interface, atomically reserved launches (03).
4. Migrate every spawn path; change child inheritance (04).
5. Distributed workspace preparation and artifact/session transport (05).
6. Workload envelopes, quotas, fair queue, and workload lifecycle (06).
7. Resource scheduler UX, actions, graphs and explanations (07).
8. Failure safety, leases, fencing, recovery, and safe rehoming (08).
9. Simulator, learning and optimization (09).
10. Operational rollout, benchmarks, adversarial tests and gates (10).

## Recommended initial release slice
Phases 0–4 plus essential observability/UI in Phase 7, test gates and gradual rollout. Do **not** turn on migration until safe checkpoint/recovery is proven. Execute remaining phases in dependency order, using feature flags. This bundle specifies the complete program.

## Non-negotiables
* Audit and reuse existing code; consolidate, do not duplicate service/UI/API/schema.
* HerdR owns **agent host placement**, InferWeave owns **inference/model/GPU worker placement**; host GPU telemetry is informational except for explicitly GPU-bound local process constraints.
* Mac manual-only by default and resilient to alias changes/reconnections. A manually hosted parent cannot implicitly place children on it.
* Host eligibility is rechecked at final bind, not only during ranking. No eligible host => reasoned queued state, never blacklist bypass.
* No live-process teleportation. Only verified resumable checkpoint migration with fencing, single writer and rollback.
* Credentials never in CLI args, logs, artifact objects or public APIs. Remote collaboration rights never imply machine execution rights.
* Persist identity, task lineage, session IDs, Git links and audit across hosts.
* Implementation owns testing and deployment safety, not simply producing docs. Maintain existing behaviors via contract tests.

## Package files
`00-audit.md`, `01-host-policies.md`, `02-telemetry.md`, `03-scheduler.md`, `04-adapters.md`, `05-workspaces.md`, `06-quotas.md`, `07-ui.md`, `08-recovery.md`, `09-simulator.md`, `10-validation-rollout.md`, `contracts/api.md`, `contracts/schema.json`, `tasks.json`, `AGENT_PROMPT.md`.
