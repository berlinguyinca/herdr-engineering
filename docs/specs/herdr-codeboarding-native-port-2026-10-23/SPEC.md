# Technical specification
## Architecture
Herdr owns UX, authorization, collaboration, control-plane API, events and persistence. Pi Engineering owns run/task/worker/worktree orchestration and execution history. CodeBoarding provides a selectively ported Python static analysis engine, symbol extraction, dependency edges, hierarchy/clustering, incremental fingerprints and structured outputs. Keep Python as isolated worker initially; do not rewrite into Go/Rust without benchmark justification.

## Hard rules
1. Audit existing source before coding, with exact path/symbol reuse matrix. Extend/refactor rather than duplicate graph renderer, scheduler, events, DB, API, identity or chat.
2. Each host owns its own repositories/worktrees per Herdr docs/architecture.md. Analyze on source host or explicitly authorized isolated clone. Central service holds metadata and derived graph snapshots, not arbitrary raw source copies.
3. Pin upstream commit and dependency lock; preserve MIT copyright/license and third-party notices. Do not assume CodeBoarding hosted frontend is open source.
4. No external code upload or telemetry by default; InferWeave model route is optional and explicitly authorized. Sandbox untrusted source, sanitize environment, no arbitrary execution, symlink/path escape protection, resource/time limits, cancellation.
5. Immutable snapshots keyed by host, repository, commit SHA, worktree identity, analyzer version, config hash and language toolchain hash. No cache keyed by branch name alone. Concurrent agents must never share mutable analysis output.
6. Static edges are grounded in source span, extractor, provenance and confidence. AI grouping/explanation and unresolved cross-repo links marked inferred; never invent call edges.

## User experience
Native Architecture tab in existing workspace: repository/host/branch/worktree/commit picker; architecture graph overview, drilldown to file/symbol/source line, search/filter/zoom, accessible tabular alternative, edge evidence, stale/partial/unsupported warnings. Snapshot comparison (added/removed/modified nodes and edges) with PR/commit link. Overlay mission, task, spawned agent, parent agent, worker, worktree, issues, CI, plans, optional Planotator review, autospec or non-autospec. Timeline tracks architecture and agent spawning independently with correlation links. Collaboration annotations link existing rooms/threads to stable graph entity and snapshot, respecting guest permissions. Dark/light and mobile/4K layouts.

## Execution pipeline
Existing Herdr request/API -> existing scheduler or job mechanism -> isolated analyzer on owning host -> normalized immutable graph artifact -> existing persistence/event stream -> existing web client graph view. Use existing event envelope and idempotency; emit analysis requested/started/progress/completed/failed/cancelled and snapshot published/diff computed. Debounce edits, dedupe identical requests, prioritize interactive analysis. Use bounded graph queries, pagination and lazy expansion. Store large outputs as existing artifact references.

## Incremental and cross-repo
Full baseline first; incremental mode only after full-vs-incremental parity tests. Invalidate on source, dependency graph, config, analyzer or toolchain changes. Cross-repo edges require resolved package/import/API evidence or explicit mapping; show unresolved/inferred separately. Preserve historical snapshots and attribution across branch switches, rebases and concurrent worktrees.

## Priorities
P0: first local graph, native UI, security, worktree isolation, accurate source links, reproducible snapshots. P1: PR diff, Pi attribution, collaboration, incremental parity, performance. P2: cross-repo graphs, drift alerts, source-backed AI explanations and historical architecture explorer.

## Non-goals
No second dashboard, second graph DB, separate agent scheduler, new chat backend, unverified AI-derived dependency assertions, or wholesale rewrite of unrelated systems.
