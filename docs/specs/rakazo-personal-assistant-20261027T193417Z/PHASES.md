# Phase status — Herdr Personal Assistant (Rakazo integration)

Dependency order per `08-phased-plan.md`. Status as of **2026-10-27**.

| Phase | Title | Status | Evidence |
|---|---|---|---|
| **P0** | Audit and decisions | ✅ **Done** | `audit/current-state.md`, `audit/adr-0001..0003` |
| **P1** | Domain and authorization | ✅ **Done (this slice)** | `assistant.py`; `test_assistant_service.py` (27 passed) |
| **P2** | Native settings and chat MVP | ✅ **Done (this slice)** | API routes + `personal-assistant-view.js`; `test_assistant_api.py` (11 passed) + client tests (5); screenshots 1280/375 |
| **P3** | Durable routines, approvals and memory | ✅ **Done** | `assistant.py` routines (timezone/quiet-hours/idempotent trigger), `correct_memory`, `/api/v1/assistant-routines` routes, Routines tab. |
| **P4** | Connectors / computer tools / delegation | ✅ **Done** | `Connector` registry + deny-by-default `HttpConnectorAdapter` (https-only, SSRF allowlist, no credential logging); **MCP + OpenAPI kind-specific adapters** (JSON-RPC tool/call + operation templating); computer-tool sandbox policy (browse/run_shell deny-by-default, host/command allowlist); **real browse executor** (HTTP fetch under policy) and **real shell executor** (arglist, no shell, timeout, allowlist-gated); `delegate` tool creates a tracked `DelegationRequest` (queued→running with artifact refs; never fabricates completion) + `execute_tool` executor. |
| **P5** | Voice and mobile | ✅ **Done (voice input/output + mobile CSS)** | `packages/voice.js` wraps the Web Speech API (webkitSpeechRecognition input, SpeechSynthesis output) with graceful text-only fallback; mic + speak controls in the chat composer; responsive 480px media query (stacked composer, comfortable touch targets). Voice is client-side; no new backend. Native client / offline transcription deferred. |
| **P6** | Production hardening | ✅ **Done (secrets redaction, rate limiting, validation, isolation tests)** | `redact_secrets()` applied to the audit log + connector errors (defense-in-depth); config/input validation (connector https + allowlist origins, routine timezone/interval range); per-key fixed-window rate limiter (`max_requests_per_minute`, HTTP 429) on the assistant API; cross-user isolation tests for all P4 entities. Load/failover, observability, backup/restore and durable Postgres store still deferred. |

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
