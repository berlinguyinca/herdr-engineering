# Phase 3 — one admission + placement service
Build a single small scheduling module within existing Herdr control plane, called by ALL launch adapters. It must expose `plan`, `reserve`, `commit`, `release`, `cancel` and state reconciliation behind the existing API/auth transport. No second orchestrator. Persist reservations durably and uniquely by `request_id`. Coordinator uses atomic compare-and-set transaction in existing durable store to prevent simultaneous overcommit. Reservation TTL + lease renewal, limits on outstanding operations. Dashboard preview is dry-run and never reserves.

## Algorithm
1. Resolve user, task, parent, project, room scopes, resource profile, runtime family, workspace revision, data/host affinities and hard constraints.
2. Enumerate owned trusted hosts; normalize stable identities and current telemetry; intersect host policy, capability, reachability, ACL, repo access, disk and site rules. Apply hard CPU/IO/memory/thermal health exclusions; reject unknowns safely.
3. Account for measured non-Herdr load + running Herdr workload + reserved-but-not-running demand + headroom and per-host/project quotas.
4. Apply capacity fit and then score surviving hosts with weighted normalized remaining headroom (CPU, RAM, IO), data locality, latency, failure-domain spread, expected queue time, host energy class and soft affinity. Scores must be interpretable. Favor user responsiveness over minimizing raw agent-count variance. Top-scored near-ties use seeded fair rotation to avoid persistent hotspots.
5. Acquire reservation atomically, re-check policy generation/capacity/host lease at bind, create isolated execution lease, launch once through native adapter, confirm heartbeat. On failure release reservation or reconcile uncertain launch before retry.
6. If no eligible host, queue with structured rejection reasons and retry on telemetry/capacity events with bounded backoff. No hidden fallback to local Mac.

## Semantics
Explicit user-selected host overrides score but **not** identity, ownership, capability, access or disabled policy. `manual_only` accepts explicit selection, never implied parent placement. Preferred/affinity host is soft unless explicitly pinned and authorized. Parent-child lineage is a metadata edge independent of physical location. Support project/owner minimum seat guarantee. InferWeave remains external source of model-serving status; schedule only local agent workloads. Preserve launch request idempotency across restarts and duplicate API submissions.

## Tests
20 and 100 concurrent launches; simultaneous budgeted spawns; admission races, policy revision flip, reservations expiry, SIGKILL between commit/launch, duplicate HTTP retry, WAN disappearance, no eligible hosts, global overload, interactive priority under load, scoring explanation deterministic under same snapshot.
