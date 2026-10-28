# 03 — Autonomous operations, risk policy and CI repair

## State machine
`DETECTED -> TRIAGED -> INVESTIGATING -> PROPOSED -> AUTHORIZED -> EXECUTING -> REVIEWING -> VERIFYING -> COMPLETED` with terminal/side states `BLOCKED, AWAITING_APPROVAL, FAILED, CANCELLED, ROLLBACK_REQUESTED`. All transitions durable with timestamps, event causation, agent run IDs, evidence and policy decision. Idempotency per `(project,source incident,policy version)`; duplicate CI notifications attach to same incident, not new repair attempts.

## Pipeline for CI failures
1. Ingest signed webhook, dedupe, resolve repo/branch/commit/check and project policy.
2. Determine whether source defect, flaky test, infrastructure outage or external transient. Retrieve logs with bounded/redacted access; map recent commits and related issues.
3. Attempt reproduction in isolated unprivileged worktree/container with no production credentials and restricted egress; record test commands and artifacts.
4. Route work via existing Herdr/Pi Engineering scheduler and InferWeave model selection, respecting host exclusions and current fabric utilization; do not create a competing runner.
5. Implementer makes minimal targeted change and regression test, posts draft PR linked to incident and supporting artifacts.
6. Independent reviewer (different execution context; distinct model/provider where available) evaluates threat model, correctness, code diff, tests and scope. If review requests changes, send bounded feedback to implementer; preserve review history.
7. Apply deterministic policy gate: permitted repo/branch/path/action, patch size AND semantic risk, required review result, test matrix, signed provenance, CI green on exact head SHA, no outstanding changes, no protected path, no dependency/secret/security-sensitive change, no active incident conflict, acceptable blast radius and reversibility.
8. For approved low-risk classes, use GitHub native auto-merge/merge queue honoring branch protection. Re-evaluate gates if head SHA, base, approvals, policy, or CI state changes. Never force-push or bypass mandatory protections.
9. Verify merged mainline checks and applicable runtime health; if not good, incident stays open; request rollback only through policy. Send concise status email with root cause, diff, PR, review, tests, merge result and links.

## Risk classification
Low-risk candidates: docs, nonbehavioral formatting, isolated test fixture corrections only when externally verified and security/coverage unaffected. Moderate: contained bug fix with tests and known scope; default human gate unless project explicitly approves this class. High: scheduler, auth, secrets, database, package/dependency manifests, CI workflows, infra, deployments, permissions, data migration, model execution/benchmark scoring or broad source changes; human gate. Critical: production credentials, destructive infra/database, public sharing, billing, legal, access control; deny unattended action. Never classify from line count alone.

## Hard constraints
- No AI reviewer alone authorizes merge. Policy engine is deterministic.
- A bot-created PR approval does not satisfy GitHub human review requirements unless repository policies explicitly permit it; never impersonate a human reviewer.
- If required human approval cannot be satisfied, leave PR open and notify.
- Time, token, cost, attempt and concurrency budgets per incident/project/user. Circuit-breaker on repeated failures or cascading PRs; no self-amending autonomy policy by agents.
- No privileged SSH/production shell via model. Operators use narrowly scoped Herdr control actions with authorization, arguments validated server-side and logged.
- Every model selection records route rationale, benchmarks consulted (if actually available), runtime, tokens and outcome; benchmarking never substitutes for functional CI evidence.

## Other triggers
Stalled agents (loop detection), failing recurring jobs, unexpected GPU pressure, benchmark regressions, new scientific validation evidence, missing deliverables and artifact inconsistencies. Use same durable workflow primitives, but distinct action policies. Validation must not treat green software tests as proof of scientific correctness.

## Notifications
Email summaries and optional mobile notifications respect quiet hours, dedupe and digest preferences. Priority notification for permission-required, repeated failures and post-merge regressions. Every notification has status links and provenance. Sending email is a privileged separately-scoped action; service notifications go through an explicitly configured transactional channel.
