# Phase 10 — tests, rollout, deployment and final acceptance
Test pyramid: unit policy/score/TTL; state machine/model-based event ordering; integration adapter/native CLI; load/soak/fault injection; browser UX Playwright; real 2–3 host canary including Linux and macOS manual-only. Introduce flags `scheduler.telemetry`, `scheduler.policy_enforcement`, `scheduler.plan_only`, `scheduler.admission`, `scheduler.all_spawns`, `scheduler.migration` default OFF except read-only telemetry and explicit existing opt-outs. **Policy safety enforcement for manual-only/disabled must not depend on optional score flag**. Existing spawns maintain behavior during controlled migration, but cannot bypass hard exclusions.

## Ordered release
1. Snapshot configuration and running sessions; run baseline synthetic 1/10/50/100 concurrent spawns with load and queue metrics.
2. Ship stable host identity + deny policy API + tests; set Mac manual-only and verify aliases.
3. Ship telemetry shadow collection and capacity dashboard; verify 24h healthy and stale-host semantics.
4. Enable plan-only, compare placement versus round robin, collect rejections/score explanations.
5. Enable scheduler for collaboration fleet on 1 canary host while keeping existing native launcher.
6. Enforce for every audited agent creation path, incrementally; reject bypasses in integration tests.
7. Enable reservations, quotas and workload profiles, then distributed worktree prep and UX.
8. Simulate overload and network partitions, test safe queueing and draining. No live migration until all fault-matrix tests pass and explicit approval.
9. Enable checkpoint/recovery for one certified agent family at a time, then simulator/autotune in advisory mode.
10. Document operational runbooks, rollback instructions, diagrams, performance measurements and final acceptance report.

## Required commands/artifacts from implementing agent
Run project's existing format/lint/unit/integration/e2e commands discovered in audit; do not invent CLI names. Capture commands, versions, results and failures in `verification.md`. Provide operator-facing policy setup, dashboard screenshots and `docs/operations/fabric-scheduler.md`; add release notes, migration/rollback checklist and alerts. Open small cohesive PRs per phase and track dependency issues/links in agent graph, not a single risky long-lived branch. Preserve in-progress UI work and use existing style tokens. Document unsupported runtime migration honestly.

## Go / no-go gate
No new auto agent on Mac, no double assignment under concurrent 100 launches, no unsafe move, no stale-host selection, queue when all blocked, native session/room/Git lineage unbroken, user sees reason for decision, rollback tested, cross-host parent-child run verified.
