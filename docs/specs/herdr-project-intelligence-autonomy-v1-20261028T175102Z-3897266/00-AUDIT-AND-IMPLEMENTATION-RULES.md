# 00 — Mandatory repository audit and execution rules

## First action, before building
Discover and inspect the exact current repositories and deployed services for Herdr dashboard, Herdr/Pi Engineering, InferWeave, InferWeave Bench, artifact registry, CI orchestrator, existing assistant/Rakazo integration, OpenViking and storage configuration. Use actual repo roots and commits; do not assume repository names or locations. Inspect README, architecture docs, packages, manifests, schema migrations, REST/MCP/OpenAPI contracts, UI routes, tests, workflow definitions, deployment manifests, secrets patterns and feature flags. Inspect in-flight code/worktrees; avoid conflict with ongoing work.

Write `AUDIT.md` with a matrix: capability; actual source path and commit; present/partial/missing; reuse approach; ownership boundary; gap; risk; proposed tests. Write `INTEGRATION-MAP.md` with actual interfaces and event sources. Write `DECISIONS.md` describing any divergence from this spec and why. Do not implement a new service when extension of existing one meets requirements.

## Required discovery questions
1. Where do agents, sessions, executions, model routing, placement, approval and review live? Which events exist?
2. Where do artifacts, version histories, external shares, ACLs, content hashes and RustFS object keys live? Which operations are already implemented?
3. Where is CI state ingested? Are GitHub App credentials, webhook signature verification, merge queue and PR review available?
4. Are Gmail/Calendar connectors available from Rakazo, Herdr or user-identity infrastructure? Is there an existing sync cursor/notification bus?
5. Is there a shared PostgreSQL schema/event table, search index, OpenViking integration, dashboard navigation abstraction and auth boundary?
6. Which UI components, responsive layout, visual tokens, activity cards and project views must be reused?
7. Which code remains under active construction? Which endpoints are experimental?
8. Which hosts are local vs remote, and which are prohibited from autonomous agent placement (notably portable laptop/Mac unless explicitly allowed)?

## Non-negotiable engineering requirements
- Implement incrementally in existing codebases where possible; make a package only where architectural boundary warrants it.
- Every phase ends in executable tests, migrations with rollback, screenshots or snapshots where relevant, and a ledger entry.
- Never print or commit secrets, message contents, participant PII, credentials, private artifact URLs, or customer data into logs.
- Use least-privilege GitHub App and scoped Google OAuth; no personal access token with broad repo administration when narrower scopes suffice.
- All mutations use existing application auth/RBAC and tenant/project isolation; MCP is an adapter, not a bypass.
- Avoid backend APIs whose sole purpose duplicates already supported service commands.
- Provide dry-run, replay, backfill, pause and kill-switch controls for ingestion and automation.
- Existing Pi agent implementation and review capabilities must be called, not reimplemented.
- Build against current checkout; pin upstream Rakazo integration boundaries to audited version rather than assuming upstream internals remain stable.

## Deliverables at phase zero
`AUDIT.md`, `INTEGRATION-MAP.md`, `DECISIONS.md`, phased issue breakdown with testable milestones, migration plan, risks and feature flag strategy. Execute implementation after audit; do not stop at recommendations if code and permissions are available.
