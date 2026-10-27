# 09 — Test plan and executable acceptance criteria

Unit: policy, routing adapter, state machine, memory namespace, grant revocation, approval expiry, scheduler timezone/DST, deduplication, retry/backoff, quota enforcement, model fallback.
Contract: API JSON/SSE compatible with existing control plane, auth rejection, pagination, optimistic concurrency, error shapes and event schema.
Integration: sandbox tool run, downstream Pi delegated mission, InferWeave model routing, RustFS artifact ACL, OpenViking memory isolation, existing scheduler checkpoint and resume.
E2E: onboarding → chat → tool with approval → artifact → timeline; scheduled briefing → notification; user revokes connector during pending run → fail closed; device refresh → same thread; mobile at 375 px and desktop at 1280 px; host failure/replay → no duplicated externally observable action.
Security: tool-output prompt injection attempts, cross-user and cross-tenant tenancy, malicious MCP, credential redaction, filesystem/network isolation, delegated permission containment, public link denial by default.
Accessibility: keyboard-only navigation, accessible names, focus management on approval modal, WCAG 2.2 AA contrast, reduced motion, screen reader routine controls.
Operational: backups restored, fault injection, offline/reconnection UX, branch migration rollback, kill switch, host denylist and laptop manual placement.

### Concrete acceptance examples
A1 With assistant disabled, no background run is scheduled and no nonessential data is ingested.
A2 Default new assistant cannot send email or execute production shell commands without grant/approval.
A3 Thread reload after logout/login maintains thread and accurate run state only for authorized user.
A4 Memory from user A is never returned to user B; delete propagates to retrieval indexes.
A5 Failed/rerun trigger with same key produces no duplicate email or external command when the provider supports dedupe; uncertain outcome escalates rather than repeats blindly.
A6 Model route shows actual chosen model, reason category, latency/usage, and errors without secret leakage.
A7 Native settings/chat match Herdr tokens and remain usable at 375px, 768px, 1280px.
A8 On a prohibited Mac/laptop placement, scheduler never assigns autonomous task.
A9 A completed run reports verifiable outcome and links to canonical artifact/mission, not merely 'done'.
A10 Feature flag rollback does not delete user artifacts/memories and stops new runs safely.
