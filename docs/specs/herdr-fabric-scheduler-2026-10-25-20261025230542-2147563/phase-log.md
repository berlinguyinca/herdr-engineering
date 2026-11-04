# Phase boundary log

## P00 — audit

- Changed files: `audit.md`, `launch-path-matrix.md`, implementation plan.
- Evidence: verified host `bender`, Mac SSH alias `macbook-pro` → `mac.lan`, archive SHA-256, repository branch/dirty state, code symbols and current launch/UI seams.
- Decision: extend `ControlPlaneRepo`, existing API/SSE, native Herdr adapter, and current dashboard; no second orchestrator.
- Remaining: implement all dependency phases.

## P01 — host identity/policy

- Changed files: `herdr_engineering/host_policy.py`, repository host projection/policy methods, ingest fields, migration `0005_fabric_scheduler.sql`, focused tests.
- Evidence: 29 targeted tests pass after red/green cycle.
- Decision: stable UID is independent of aliases; scheduler ownership/trust are explicit; missing legacy scheduler proof remains conservative/manual-only.
- Remaining: durable hydration of scheduler policy/reservation projections and CLI/UI policy mutation wiring.

## P02 — telemetry/capacity

- Changed files: `herdr_engineering/telemetry.py`, telemetry tests, ingest freshness fields.
- Evidence: synthetic stale/future/out-of-order/pressure/EWMA tests pass.
- Decision: freshness is bounded at 60 seconds by default; missing values remain unknown; PSI and memory pressure are hard signals.
- Remaining: host-side authenticated collector and process/cgroup accounting deployment.

## P03 — admission/placement

- Changed files: `herdr_engineering/scheduler.py`, `control_plane/scheduler_service.py`, API routes, migration, scheduler/API tests.
- Evidence: deterministic explainability, idempotent reservation, policy race, and 100-concurrent no-overcommit tests pass.
- Decision: scoring is pure after hard eligibility; reserve/commit rechecks policy generation; dry-run never mutates reservations.
- Additional implementation: repository event-sourced reservations, export/import restoration, scheduler restore, and persisted expiry reconciliation.
- Fresh evidence: persistence/recovery slice 9 passed; targeted Ruff, compilation, and diff checks passed.
- Remaining: durable lease records/reconciliation beyond reservation state and quota integration into the service.

## P04–P09 — integration and safety foundations

- Changed files: `collab-spawn.py`, scheduler dashboard/client, `workspace.py`, `quotas.py`, `recovery.py`, `replay.py`, focused tests.
- Evidence: Python focused suite and scheduler UI unit test pass.
- Decision: cross-host children create destination panes while retaining parent graph linkage; migration remains blocked without checkpoint/fence proof; replay/learning are advisory.
- Remaining: complete every non-collaboration adapter, durable lifecycle state, full dashboard interactions/history/simulator, and deployment collectors.

## P04 — adapters

- Changed files: `deploy/control-plane/collab-spawn.py`, scheduler service/API, launch-path matrix, adapter regression coverage.
- Evidence: collaboration runner compiles; existing collaboration spawn/join suite passes in rollback mode; scheduler API plan/reservation smoke test passes.
- Decision: `HERDR_SCHEDULER_ALL_SPAWNS=1` is required for scheduler-backed fleet launches during rollout. Disabled mode is the explicit rollback path, not a hidden placement bypass.
- Additional implementation: shared `AdapterRegistry` admits pi/claude-code/codex/agent families, releases reservations on native launcher failure, and never inherits a parent host.
- Fresh evidence: adapter registry tests pass; targeted lint/compile/diff checks pass.
- Additional implementation: `native_machine_launcher` resolves stable scheduler host identity to a saved Herdr machine and forwards only validated argv through `NativeHerdrAdapter.machine_targeted`.
- Fresh evidence: adapter/native bridge tests pass; targeted lint/compile/diff checks pass.
- Remaining: register family-specific safe command builders and invoke them from dashboard/Pi/Claude/Codex/AutoSpec/Plannator launch paths; the registry remains fail-closed until registered.

## P05 — workspaces

- Changed files: `herdr_engineering/workspace.py`, workspace tests, operations documentation.
- Evidence: preflight rejects unscoped credentials, dirty worktrees, tool mismatch, missing identity, and low disk; checksum helper is covered by unit execution.
- Decision: dirty state requires an explicit protected snapshot; no shared mutable NFS worktree or public checkpoint URL.
- Additional implementation: `WorkspaceManifestStore` and `WorkspacePreparer` persist identity before invoking native worktree creation and refuse failed preflight without calling the launcher.
- Fresh evidence: workspace/preparer tests pass; targeted lint/compile/diff checks pass.
- Remaining: connect the preparer to native Herdr worktrees, RustFS artifact transfer, and durable control-plane manifest tables.

## P06 — quotas/lifecycle

- Changed files: `herdr_engineering/quotas.py`, quota tests, scheduler reservation accounting.
- Evidence: owner/global caps and interactive aging tests pass; reservation release/expiry returns capacity before reuse.
- Decision: queue priority is owner-interactive → normal → background, with hard caps before aging.
- Additional implementation: `SchedulerService` now applies fair-queue owner/project/global caps to queued reservations before alternate spawn paths can bypass them.
- Fresh evidence: service quota test passes; targeted lint/compile/diff checks pass.
- Remaining: persist queue entries/lifecycle transitions and enforce workload envelopes across every native adapter/fanout path.

## P07 — dashboard

- Changed files: `packages/scheduler-view.js`, `packages/scheduler-view.test.js`, `packages/app.js`, web client, config/operations docs.
- Evidence: Node scheduler view test passes; UI explicitly labels manual-only Mac and stale telemetry.
- Decision: add a route within the existing shell; no disconnected admin SPA. Current view is read-only until authenticated policy controls are wired.
- Additional implementation: Scheduling view includes an accessible dry-run placement preview form with structured host explanations; it never reserves.
- Fresh evidence: scheduler UI test passes.
- Remaining: policy mutation/rollback, history charts, simulator panel, full Playwright screenshots/accessibility matrix.

## P08 — recovery

- Changed files: `herdr_engineering/recovery.py`, recovery tests, operations rollback guidance.
- Evidence: old lease invalidation and unsafe migration blocking tests pass.
- Decision: no live migration is enabled; unsupported families remain pinned/draining and recovery never infers process death from SSH loss.
- Additional implementation: execution leases are persisted event-sourced, commit retries are idempotent, release updates lease state, and export/import restores active leases.
- Fresh evidence: recovery/persistence slice 10 passed; targeted Ruff, compilation, and diff checks passed.
- Additional implementation: lease heartbeat renewal validates token and generation, stale writers are rejected, and expired running leases are fenced and persisted.
- Fresh evidence: recovery/API slice 13 passed; targeted Ruff, compilation, and diff checks passed.
- Additional implementation: private atomic checkpoint store with digest verification, explicit versioned resume contracts, conservative native-family defaults, and a complete ambiguous-failure fault matrix.
- Fresh evidence: checkpoint/resume/fault suite 12 passed; targeted Ruff, compilation, and diff checks passed.
- Remaining: certify individual native family checkpoint/restore implementations and run the matrix against real transport/storage faults; live migration remains disabled.

## P09 — simulator

- Changed files: `herdr_engineering/replay.py`, replay tests.
- Evidence: replay reports queue/overload metrics and explicitly low/observational confidence; estimator only proposes after sufficient samples.
- Decision: counterfactual output is advisory and cannot alter safety thresholds, ACLs, blacklist, or owner priority.
- Remaining: history API/UI and sanitized persisted placement history.

## P10 — rollout/validation

- Changed files: example scheduler flags, operator runbook, this phase log, verification report.
- Evidence: targeted lint, Python compilation, combined scheduler/collaboration regression suite (68 passed), and Node suite (267 passed) are recorded in `verification.md`.
- Decision: telemetry/policy enforcement are safe defaults; admission/all-spawns/migration are opt-in, with explicit rollback by flags.
- Additional implementation/evidence: full UI suite completed with one isolated timeout-sensitive failure that passed on rerun; native adapter/collaboration suite passed 57 tests; read-only host probes confirmed Mac Herdr availability and Linux destination provisioning gap.
- Remaining: stabilize the flaky full UI test, provision the Linux canary host, run benchmarks/fault matrix on real transport, create GitHub issue/PR linkage, and execute deployment only after explicit flag gates.

