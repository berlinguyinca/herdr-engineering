# 06 — Rakazo reuse and adaptation analysis

Observed upstream repo (2026-10-27): apps/api, apps/worker, apps/web, apps/mobile, apps/desktop; packages/core, packages/memory, packages/adapters, packages/adapter-kit, packages/contracts, packages/chat-ui, packages/ui-tokens, packages/ui-web, packages/auth, packages/db. README documents persistent bots, routines, Pi models, voice, sandbox computer tools, integration providers, and delegation; TypeScript/React/Vite/Hono/oRPC/Prisma/Postgres/Graphile Worker/Better Auth.

### Reuse candidates to evaluate (NOT blanket approval)
- packages/core: agent lifecycle and Pi bridge. Adapt behind AssistantRunnerAdapter.
- packages/memory: memory contracts and policies. Prefer Herdr/OpenViking canonical storage; adapt algorithms if useful.
- packages/adapter-kit and packages/adapters: MCP/OpenAPI, Composio/Pipedream interfaces and computer adapters; ensure scopes/secret isolation.
- packages/chat-ui: interaction patterns and UX reference; do not import whole React stack into existing vanilla JS/Web Components dashboard.
- apps/mobile: evaluate patterns for future Herdr mobile client; do not introduce separate identity/server.
- Voice flow and agent delegation from upstream; adapt selectively after threat assessment.

### Avoid parallel installations
Don't deploy upstream web/auth, its standalone scheduler, its model routing, or its DB as a second control plane. Sidecar runner allowed only if isolated/stateless from canonical Herdr identity and persistence and documented.

### Upstream audit tasks
Pin Git SHA, inspect licenses and transitives, list candidate files with dependencies and test coverage, record Rakazo vs Herdr feature matrix, identify license notice obligations, write ADR for each adapted package, add upstream update strategy and regression tests. Use external package references as a temporary adapter or selective copied modules with retained NOTICE; don't fork the entire UX.
