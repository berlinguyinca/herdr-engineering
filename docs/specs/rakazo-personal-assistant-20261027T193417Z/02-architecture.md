# 02 — Architecture and ownership

Client: existing Herdr web/dashboard + mobile-responsive extension, using established design system and components.
API: existing Herdr control plane `/api/v1` plus assistant capabilities only; SSE/live event delivery through existing event transport.
Domain: assistant facade/service with user-level policy, durable task/run state, assistant configuration, tool registry and approval controller. Only one canonical owner per entity; reconcile existing code first.
Execution: Pi runtime / safely adapted Rakazo agent runner via a versioned **adapter**; integration runs behind policy and native Herdr host placement. Per task execution can be local/remote, supervised, stopped/recovered.
Inference: InferWeave model routing + provider credentials; no independent admission or GPU scheduling.
Memory: OpenViking per-user + per-assistant namespace where feasible; canonical memory access API, source metadata, retention and delete pathways; DB metadata in existing Herdr persistence layer.
Artifacts: existing Herdr artifacts + RustFS where already implemented. Write objects privately; URLs never bypass access grants.
Scheduler: existing Herdr scheduler/recovery, extended for assistant routines. Event-driven triggers attach to bus with dedupe.
Auth: existing Herdr login/tenant/user identity and RBAC. No separate Better Auth login.
Secrets: existing secret store, envelope encryption, no tokens visible to UI.
Notification: use existing notification channel or add adapter, never ad hoc push service.

### Suggested domain boundary
AssistantFacade: create/configure/list/archive, session list, send/cancel, approvals, run status, tool grants, notifications.
AssistantRunnerAdapter: begin/resume/cancel/stream state, checkpoint, tool registration, provider selection. Support upstream compatibility checks.
AssistantRoutineAdapter: idempotent schedule/event trigger handling, cron timezone/quiet hours, triggering authenticated principal.
AssistantMemoryAdapter: search/store/correct/forget/export through consent-aware namespace.
AssistantToolGateway: approves each tool operation based on actor, scope, context, sensitivity and recorded grant.

### Execution lifecycle
submitted → classified → authorized → queued → running → waiting_for_approval | waiting_for_external | completed | failed | cancelled | recovering. All transitions immutable event log + current projection; use stable task_id, run_id, step_id, idempotency_key; resume only verified-safe steps. No fake exactly-once guarantees for non-idempotent external services.

### Hard boundaries
- Personal assistants must not see other users’ conversations, private memory, computer files or secrets.
- Herdr's worker placement/blacklist and ephemeral agents honored. Laptop/Mac manual-placement-only policy honored.
- Avoid duplicate databases; preserve established migrations and deployment packaging.
- Reuse existing audit event bus and telemetry rather than introducing parallel logging.
