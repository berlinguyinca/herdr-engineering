# Phase status — Herdr Personal Assistant (Rakazo integration)

Dependency order per `08-phased-plan.md`. Status as of **2026-10-27**.

| Phase | Title | Status | Evidence |
|---|---|---|---|
| **P0** | Audit and decisions | ✅ **Done** | `audit/current-state.md`, `audit/adr-0001..0003` |
| **P1** | Domain and authorization | ✅ **Done (this slice)** | `assistant.py`; `test_assistant_service.py` (27 passed) |
| **P2** | Native settings and chat MVP | ✅ **Done (this slice)** | API routes + `personal-assistant-view.js`; `test_assistant_api.py` (11 passed) + client tests (5); screenshots 1280/375 |
| **P3** | Durable routines, approvals and memory | ✅ **Done (routines + memory correction)** | `assistant.py` routines (timezone/quiet-hours/idempotent trigger), `correct_memory`, `/api/v1/assistant-routines` routes, Routines tab; +2 unit +2 contract +4 client tests. Real InferWeave runner + durable memory backend still deferred to a later phase. |
| **P4** | Connectors / computer tools / delegation | ⏳ **Not implemented** | Requires MCP/OpenAPI adapters, sandbox policy, Pi-mission handoff |
| **P5** | Voice and mobile | ⏳ **Not implemented** | Responsive UI done; transcription, native client deferred |
| **P6** | Production hardening | ⏳ **Not implemented** | Load/failover, observability, backup/restore, runbook |

## What this slice actually delivers
A tested, production-shaped **vertical slice of P0 + P1 + P2**:

- Canonical assistant domain + policy (deny-by-default tools, grants with
  scope/expiry/revocation, approval gates, fail-closed, idempotency,
  user/tenant isolation, audit log, feature flag off by default).
- `/api/v1/assistants` (+ runs/approvals/memory) routes on the existing control
  plane, reusing its auth, persistence, and health-capability model.
- Native Settings → Personal Assistant surface (vanilla JS / Web Components,
  design tokens) with chat, permissions, memory, approvals, diagnostics.
- 43 tests (27 unit + 11 contract + 5 client) and rendered UI screenshots.

## What is honestly NOT claimed
- Real model inference (runs use the deterministic `LocalRunnerAdapter`).
- Durable routines, real memory backend, connectors/computer tools/delegation,
  voice, native mobile, production hardening.
- Any production deployment, DB migration, flag enablement, or external side
  effect — none were performed.
