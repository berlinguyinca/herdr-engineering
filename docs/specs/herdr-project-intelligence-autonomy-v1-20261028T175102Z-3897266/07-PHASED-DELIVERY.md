# 07 — Sequence, deliverables and gates

## P0 — Discovery / architecture
Audit actual repositories and in-progress work; write evidence-based reuse map, ADRs, integration diagram and ordered issues. Gate: owners and data boundaries agreed; no hidden second scheduler.

## P1 — Event foundation
Schema/migrations, source registry, inbox/outbox, idempotent normalized events, project/investigation records, permission checks, provenance, baseline timeline API. Gate: replay and tenant isolation tests.

## P2 — Connectors / artifact integration
Gmail incremental + historical opt-in, signed GitHub webhooks and reconciliation, existing CI/agent/artifact event adapters; originals + thumbnails via existing registry/RustFS. Gate: complete source lineage and cursor restart tests.

## P3 — Intelligence
Project resolution, typed claims and relationships, inference review, entity corrections, commitments/decisions, evidence graph, OpenViking permitted retrieval, timeline read models. Gate: evidence completeness and precision evaluation.

## P4 — Dashboard and mobile
Responsive project overview, timeline, investigation, graph, artifact provenance, assistant source-link cards, typed deep links, approvals. Gate: E2E UX with existing style system and phone widths.

## P5 — Autonomous operations
Detect->diagnose->delegate->review->verify workflow based on existing Pi/CI/agent scheduler; deterministic policy evaluator; initial suggest-only; supervised actions; optional low-risk auto-merge behind separately authorized flag. Gate: negative security tests and exact commit check revalidation.

## P6 — Backfill and production
Historical reconstruction controls, progressive source enablement, observability, data deletion/revoke, runbooks, performance capacity, ongoing model evaluation. Gate: operational rollback and failure drills.

## Team/agent parallelization
Only split independent work after audit into contract/schema, source connectors, intelligence, dashboard and test/security lanes. Shared APIs and migrations require one accountable owner, independent reviewers and contract tests; no agents editing the same files/branches without worktree isolation. Review phase completion before enabling downstream risky features.

## Progress ledger
For each phase track: state, dependencies, actual changed file/commit, tests and measured result, artifact link, security review, open blockers. On completion include a concise 'what already existed', 'what changed', 'what remains' report, plus screenshots and example project timeline.
