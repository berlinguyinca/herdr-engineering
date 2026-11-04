# Phase 6 — resource envelopes, fair queue and lifecycle
Workload classes: interactive coding, planning, reviewer, background observer, build/test, GPU benchmark, maintenance. Separate `requested`, `minimum`, `maximum` CPU threads, RAM, disk, ephemeral IO, expected duration and latency preference. Profile defaults are conservative and learned over time, but cannot override operator hard caps. Per-host, per-owner, per-project, per-mission, per-workspace concurrency and resource budgets. Important workloads reserve capacity; optional background observers yield on interactive pressure.

## Fairness
Use weighted fair admission with aging, owner-interactive priority and project minimum shares; avoid indefinite background starvation. Apply backpressure to AutoSpec waves and recursive spawn fanout (depth, rate, burst budget, global queued cap). Define concurrency across all runtime families; account for non-Herdr processes. Do not confuse external inference concurrent seats/quotas with host CPU slots; consult InferWeave admission signals only to inform workload waiting versus local execution, not to override its scheduler.

## Lifecycle
States: `requested -> queued -> reserved -> preparing -> starting -> running -> draining -> finished/failed/cancelled`; uncertainty branch `reconciling`, recovery branch `checkpointing -> suspended -> restoring`. Idle hibernation only for supported runtime; preserves session and releases reservations after verifiable stop. Add per-host automatic busy/drain at sustained overload and recover after hysteresis. During overload consider lowering background concurrency or slowing new launches before attempting migration.

## Acceptance
100 spawns from one project cannot starve other project's interactive run, caps enforce under races, quotas cannot be bypassed by child spawn or alternate harness, hibernated sessions restore safely or are labeled unsupported.
