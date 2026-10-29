# Herdr Fabric Scheduler — handoff from Codex to Pi

## Stop state and objective

**Resume update, 2026-10-28:** the user explicitly asked Codex to continue after
usage returned. The stop state below is historical; it no longer instructs the
current Codex session to remain stopped. The Pi package was still queued and
unclaimed at resume. Consult current files and the release safety audit for work
after this handoff.

The user explicitly stopped Codex implementation and requested this handoff.
The Codex goal is PAUSED. Do not automatically resume this Codex session or start
another agent from it. Pi should continue only when the user starts the next session.

The full objective is to finish the Herdr Fabric Resource Scheduler from the
original ZIP, including P00–P10 and documented acceptance. The product is NOT
finished, and no full-release or migration-safety claim is justified yet.

## Location and provenance

- Execution host: `bender`.
- Worktree: `/home/wohlgemuth/IdeaProjects/herdr-engineering`.
- Repository: `berlinguyinca/herdr-engineering`.
- HEAD at handoff: `06e3d501ad95061a53157c05aa915c86d4482bfa`.
- Branch at handoff: `main`, ahead 6 / behind 619. Recheck before working.
- This is a shared, heavily dirty worktree containing concurrent artifact,
  publication, handoff, report, frontend, and infrastructure changes.
- Scheduler work has NOT been separated into clean commits or a PR. Do not reset,
  stash, clean, pull/rebase, or stage all files. Preserve concurrent work.
- Acceptance tracking: https://github.com/berlinguyinca/herdr-engineering/issues/290
- Original archive was pulled using the configured `macbook-pro` SSH endpoint
  from `/Users/wohlgemuth/Downloads/herdr-fabric-scheduler-2026-10-25.zip`.
- SHA256: `85c84d7f72c9e668aca47e0196a8ce9af75cefa87bc13a44403b4d1d9beae39c`.
- Specification directory: this directory; complete original bundle is under
  `bundle/herdr-fabric-scheduler-2026-10-25/`.

## Required reading, in order

1. The repository's current instructions and current `git status`/diff.
2. Original bundle `AGENT_PROMPT.md`, `README.md`, phases `00-audit.md` through
   `10-validation-rollout.md`, `contracts/api.md`, `contracts/schema.json`, `tasks.json`.
3. This directory's `release-safety-audit.md` (most current technical evidence),
   `uncertain-launch-verification.md`, and `launch-path-matrix.md`.
4. `phase-log.md` and `verification.md` for older evidence. These are historical;
   earlier notes overstate completion. Verify claims against current code.
5. `docs/operations/fabric-scheduler.md` and
   `docs/superpowers/plans/2026-10-25-herdr-fabric-scheduler.md`.

## Implemented components and their limits

- Host policy: stable identities, aliases, explicit manual mode, hard exclusions,
  revision checks, acknowledged policy persistence, and policy replay.
- Resource reporting: existing fabric-agent loop reports through a fleet-token
  endpoint. Linux CPU deltas/PSI/memory/disk and lower-confidence macOS metrics.
  Unknown metrics block usable capacity. Headroom and pressure enter placement.
- Admission: shared scorer, reservations, retries, fairness/aging, workspace gate,
  durable projections, PostgreSQL advisory admission transaction and execution claim.
- PostgreSQL tests exercise 100 requests across four processes, coordinator crash
  before commit, one launch claim across processes, and cross-coordinator policy flips.
- Native callbacks: real Herdr `workspace create` + `agent start` contracts for
  Pi/Claude/Codex. Worktree helper corrected to `worktree create` CLI contract.
- Workspaces: persisted manifests and prepared-host resolver, real Git origin,
  clean-state/commit/branch checks, native preparation callback, registry artifact
  materialization with ACL/checksum checks, and single-writer admission gate.
- Linux scope supervisor: existing bubblewrap sandbox inside a systemd service
  cgroup; stop verifies cgroup emptiness/removal. A real test stops a writer and
  child in a separate session. This does NOT automatically fence arbitrary
  interactive native agents; they must use this supervised boundary.
- Per-scope resource accounting reads CPU, memory/peak, I/O, pressure and persists
  observations against reservation identity. Not yet wired periodically everywhere.
- Dashboard: scheduling overview, real reservation history/counts, policy editing,
  placement preview, queued retry, historical counterfactual simulation. Policy
  edits survive SSE updates; browser tests and four screenshot widths exist.
- Recovery scaffolding: private checkpoint files, versioned resume declarations,
  unsupported native defaults. Actual runtime migration remains uncertified.
- Most recent edits: mission concurrency, owner CPU/RAM ceilings, maximum declared
  spawn depth, and conservative per-workload CPU/RAM/disk defaults.

## Scheduler-owned/new files to inspect

Core: `herdr_engineering/{host_policy,telemetry,resource_probe,scheduler,quotas,
workspace,adapter_registry,execution_scope,checkpoint,resume,replay,recovery}.py`.

Control plane: `herdr_engineering/control_plane/scheduler_service.py` plus scheduler
hunks in `repo.py`, `sink.py`, `bridge.py`, `ingest.py`, `api.py`, and `cli.py`.

Integrations: `fabric_agent.py`, `herdr_adapter.py`,
`deploy/control-plane/collab-spawn.py`, and both sandbox script copies:
`deploy/control-plane/collab-sandbox.sh`, `packages/collab-scripts/sandbox.sh`.

UI: `packages/scheduler-view.js`, its test, and scheduler hunks in `app.js` and
`herdr-web-client/client.js`. Browser tests in `tests/playwright/ui.test.mjs`.

Schema: `deploy/control-plane/migrations/0005_fabric_scheduler.sql`.
Many neighboring migrations belong to concurrent work. The event-store path is
currently the primary scheduler persistence implementation; do not claim all
structured scheduler SQL tables are wired.

Tests: `tests/test_{host_policy,telemetry,resource_probe,scheduler,adapter_registry,
checkpoint_resume,execution_scope,execution_scope_live,workspace_native,
workspace_quotas_recovery_replay,scheduler_simulator,herdr_adapter}.py`,
`tests/control_plane/test_{scheduler_service,scheduler_persistence,
scheduler_postgres,host_scheduler_policy}.py`, plus modified API/ingest/sink/
collaboration tests. Inspect ownership of mixed files before editing/staging.

## Important correctness gaps to address next

1. Complete the actual launch-path matrix: CLI, dashboard, Pi/Claude/Codex,
   AutoSpec, Plannator, handoffs, scheduled/resumed work, and recursive subagents.
   A helper/registry being implemented is not proof every path invokes it.
2. Connect real host preparation, immutable artifact transfers, native execution
   receipts, periodic lease renewal/accounting, and supervised stop/reconciliation.
   Prepared-record callbacks must be backed by actual verified host observations.
3. Audit native interactive execution vs supervised scopes. A boolean `fenced`
   state does not revoke writes. Never release a claimed execution's capacity
   just because SSH or a heartbeat disappeared. Uncertainty retains capacity.
4. Audit `RecoveryCoordinator.migrate` and `fault_matrix` in `recovery.py`:
   capability booleans and enumerated outcomes are scaffolding, NOT executable
   migration certification or a full fault-injection suite. Require real origin
   quiescence/stop, authoritative fencing, private checkpoint transfer, restore,
   destination heartbeat and Git consistency. Keep migration OFF until proven.
5. Audit persistence/privacy: token-bearing reservation/lease records must not
   leak via generic events/SSE/history; lease renewal/reconcile must use current
   authoritative state across processes. The recent scope-stop/accounting APIs
   require review for persisted receipt/generation binding and fail-closed recovery.
6. Complete resource requirements: capability/family registration and enforcement,
   sustained-pressure hysteresis, thermal/swap signals, GPU diagnostics without
   taking InferWeave inference ownership, and process-tree reporting for all agents.
7. Finish quota enforcement. Latest `spawn_depth` is declared input; derive and
   validate ancestry/fanout server-side so resetting a caller's depth cannot
   bypass it. Complete mission/workspace resource budgets and hibernation support.
8. Finish live dashboard updates without clearing edits; complete errors,
   cancellation/reconciliation controls, graph host linkage, history charts,
   accessibility and screenshot inspection. Simulator is independent-snapshot
   counterfactual scoring, not measured performance forecasting.
9. Finish real Linux/macOS canary, baseline vs scheduler load measurements,
   comprehensive fault matrix, flag rollout and tested rollback.
10. Scope review, clean commits/PRs, full-repository checks, and audit every
    original requirement before marking issue #290 or the goal complete.

## Live fleet facts and external changes

- Mac SSH alias `macbook-pro` resolves to `mac.lan`; configured control-plane URL
  is `http://100.104.39.6`. Native Herdr is `/Users/wohlgemuth/.local/bin/herdr`.
- Beast has Herdr 0.9.1 at `~/.local/bin/herdr`; a prior non-login PATH probe
  incorrectly suggested it was missing. Use explicit paths or configured login env.
- Live native machine list on bender included saved machine `beast`/session default.
- Requested Mac safety change was applied to the EXISTING legacy fleet API for
  `mac.lan` and `macbook-pro`. Subsequent GET verified both `auto_agents=false`.
  POSTs initially timed out; state was reconciled by readback. Do not blindly retry
  uncertain mutations or re-enable either alias.
- The live scheduler endpoint was serving an older implementation, not the full
  current dirty worktree. Do not equate local tests with deployed acceptance.
- No automatic migration was enabled. Deployment gates remain unfinished.
- Another agent's lifecycle/API pytest process was running at handoff; do not kill
  other agents' processes. No scheduler test started by this session remains pending.

## Verification evidence (historical, rerun after relevant edits)

- Broad scheduler/resource/workspace/native/ingest/bridge/sink/fabric-agent selection:
  **187 passed in 79.48s** (before the final active-budget edits).
- Most recent focused run after mission/resource/depth additions:
  **53 passed in 0.21s** across scheduler/service/persistence/adapter tests.
- Real PostgreSQL: 100-process contention and crash rollback selection **2 passed**;
  duplicate execution-claim test **1 passed**; cross-coordinator policy-flip test
  **1 passed**. See audit for per-run durations and exact scope.
- Real Linux scope: parent and child stop writing, CPU/memory accounting read:
  live/unit scope selection **3 passed**.
- Real Git/native-preparation/registry-artifact tests pass (see audit).
- Scheduler policy editing and simulator Playwright selection: **2 passed**;
  view tests **2 passed**; screenshots at 390/768/1440/3840 under
  `tests/playwright/screenshots/scheduler-*.png` (390 image inspected).
- Legacy broad compatibility: **48 passed, 1 HTTP claim timeout**; failed test
  passed alone. Later targeted helper-exclusion/split-tab/no-host tests:
  **3 passed in 91.83s**. Preserve the broad-suite failure in release reporting.
- Full repository pytest has NOT been completed green. A mixed test selection once
  hit `acknowledged_sink` fixture discovery; the API test passed separately.
- Earlier full UI suite: **60/61**, with one timeout-sensitive history test passing
  in isolation. Do not label this full suite green.
- Targeted Ruff/compilation/diff checks passed at the respective edit boundaries.
  Repository-wide Ruff has unrelated existing violations; never auto-fix all files.

## Useful commands for Pi

Run in the existing checkout; use the virtualenv explicitly:

```bash
cd /home/wohlgemuth/IdeaProjects/herdr-engineering
git status --short --branch
./.venv/bin/python -m pytest -q tests/control_plane/test_scheduler_service.py tests/control_plane/test_scheduler_persistence.py tests/test_scheduler.py tests/test_adapter_registry.py
./.venv/bin/python -m pytest -q tests/control_plane/test_scheduler_postgres.py
HERDR_SCHEDULER_SCOPE_LIVE=1 ./.venv/bin/python -m pytest -q tests/test_execution_scope.py tests/test_execution_scope_live.py
node --test --test-name-pattern='scheduler policy changes|scheduler historical simulation' tests/playwright/ui.test.mjs
node --test packages/scheduler-view.test.js
git diff --check
```

The PostgreSQL test creates/stops disposable local databases. The live scope test
creates only a temporary sandbox workspace and named test systemd service; it
does not invoke an LLM. Other integration tests can be slow under disk contention.
Wait on the same live process rather than restarting on observation timeout.

## Instructions to give Pi

Read this entire handoff and original bundle, inspect current diffs, claim and
accept the local Codex-to-Pi package, then resume the full original objective.
Use existing architecture; preserve concurrent changes. Prioritize complete
end-to-end integration and executable acceptance gates. Correct earlier
overstatements rather than inheriting them. Continue autonomously within the
user's existing implementation authorization. Leave unsupported migration off.
Do not declare completion based on narrow green tests. Report only what current
files, runtime behavior, test results, and deployment evidence actually prove.
