# 08 — Dependency-ordered implementation roadmap

P0 Audit and decisions: verify current repo HEAD and deployments, map existing components, classify Rakazo reuse, produce compatibility matrix/ADRs. No implementation until ownership and security boundary settled. Deliver manifest of reused/refactored/introduced entities and interface definitions.
P1 Domain and authorization: assistant entity, settings, grants, task states, thread/run records, event contracts, policy-aware tool gateway, runtime adapter skeleton. Unit/contract/migration tests and feature flag off by default.
P2 Native settings and chat MVP: use existing theme/design system, sidebar settings and assistant drawer, streaming via existing event transport, host/model choice through canonical provider. Read-only operations first.
P3 Durable routines, approvals and memory: scheduler integration, idempotent executions, audit timeline, OpenViking per-user namespace, provenance, privacy UX. Recovery and rollback tests.
P4 Connectors/computer tools/delegation: authorization flows, MCP/OpenAPI and existing app adapters; browser/shell sandbox policy; Pi Engineering mission handoff; artifact output registration.
P5 Voice and mobile: transcription/voice adapters and permissions; small-screen UX; opt-in mic and retention; notifications, quiet hours, push if platform-supported.
P6 Production hardening: load and failover tests, least-privilege audits, observability, backup/restore, migration, operational runbook, rollouts and documentation.

### Phase gates
Each phase: current-state evidence → narrow implementation → tests + security review → screenshot/interaction checks desktop/mobile → canary with flag → documented acceptance and rollbacks. No broad refactors unrelated to this feature. Parallel agents only on isolated worktrees with integration owner and explicit merge order.
