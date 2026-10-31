# 01 — Product requirements

**Positioning**: A persistent user-scoped AI assistant, not necessarily a coding agent. Voice/text/chat, routines, memory, integrations, computer tools, and delegation are capabilities, each separately switchable and permissioned.

### Primary navigation and entry points
- Settings → Personal Assistant: enable, identity/name/persona, default vs selected model, agent instructions, autonomy, memory policies, routines, voice, app connections, computers, usage, privacy, export/delete, diagnostic status.
- Global assistant button accessible across Herdr; desktop drawer/full page and mobile bottom-sheet/full-screen, shared thread state.
- Dedicated pages or tabs: Overview, Conversations, Assistants, Routines, Integrations, Memory, Computers, Activity, Settings. Reuse existing navigation conventions.
- New user onboarding: consent → choose model → scoped tools → memory preference → first message → optional routine. Disable assistant by default until explicit onboarding/opt-in; do not infer permission from Herdr login.

### Functional capabilities
- Persistent assistants, sessions and histories; named specialist assistants and delegated short-lived workers; context limits/compaction and retry rules.
- User-scoped memory with source, timestamp, provenance, correction/delete/export, freshness and consent; do not indiscriminately persist secrets or sensitive facts.
- Scheduled and event-driven routines with approvals, quiet hours, retries and notification receipts.
- Supported tool adapters: browser, terminal, files, computer, MCP/OpenAPI, existing internal tools. Least privilege and isolated sandboxes.
- Model provider: InferWeave auto routing or manual choice, quotas/budgets, audit of effective model/fallback. Do not hard-wire model placement.
- Task delegation: forward engineering work to canonical Pi Engineering/Herdr agents; return status and artifacts, not a new orchestration framework.
- Artifacts: register generated files into existing Herdr artifact registry/RustFS if available, with private ACL and explicitly authorized sharing.
- Streaming chat, partial output, tool progress, cancellations, approvals and source provenance, mobile parity, eventual voice.

### Non-goals
Building another login/auth stack, React SPA, PostgreSQL schema domain that duplicates canonical data, orchestration engine, GPU scheduler, public exposure layer, CI engine, or independent assistant-host fleet. No financial transaction or external communication without policy confirmation.

### Success metrics
Routine completion and verified result rate; user interventions per completed task; duplicate side-effect rate 0 for idempotent actions; permission-denial correctness 100%; memory isolation correctness 100%; graceful recovery across host loss; task observability; mobile task parity.
