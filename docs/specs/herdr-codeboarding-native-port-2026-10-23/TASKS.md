# Ordered implementation tasks
A01 Pin HEAD SHAs for all three repositories and audit upstream licensing/dependencies, network and telemetry behavior.
A02 Audit Herdr graph UI, routes, migrations, events, artifact and scheduling infrastructure; record exact symbols and tests.
A03 Audit Pi WorkGraph, worktree/session/runtime and collaboration mapping; produce reuse matrix and integration design.
A04 Update spec based on actual HEAD; identify conflicts with ongoing work; plan feature flag and rollback.
B01 Selectively vendor/fork/pin upstream Python analysis modules and tests with provenance and license notices.
B02 Implement isolated on-host analyzer adapter, cancellation, resource budgets, sanitized env, no network by default.
B03 Normalize upstream output into snapshot/node/edge artifacts with source evidence and diagnostics.
B04 Implement worktree-safe immutable snapshot identity, cache and job deduplication; tests for parallel agents.
C01 Add native Architecture pane using existing Herdr components and navigation; no separate SPA.
C02 Add repository/commit/worktree selector, progress/errors, searchable and accessible graph/list view.
C03 Add source navigation and bounded lazy graph expansion; light/dark/mobile/4K Playwright tests.
D01 Implement baseline/head architecture diff and GitHub PR/issue/CI overlays.
D02 Correlate Pi mission/run/task/worker/parent-worker/worktree events with changed symbols and snapshots.
D03 Link existing collaboration rooms/comments and optional plan reviews; support autospec and non-autospec.
E01 Benchmark full vs incremental analysis, repair invalidation defects, record parity and speedup.
E02 Add cross-repo verified dependency resolution, failure diagnostics and performance guardrails.
E03 Add optional source-grounded LLM explanations via authorized InferWeave route, no external leakage.
F01 Tests, docs, metrics, licensing inventory, rollback, feature flag, screenshots and implementation report.

Each task requires exact modified files, tests, evidence and acceptance report. Deliver working vertical slice B+C before expanding to D/E. If existing functionality is equivalent, mark task reused and do not duplicate it.
