# 05 — Security/privacy threat model and mandatory controls

Threats: prompt injection via webpages/email/attachments, compromised MCP tools, malicious upstream packages, exfiltration, unauthorized shell commands, cross-user memory leakage, sandbox breakout, impersonation, stale grants, replayed triggers, unintended email sends/public publishing, poisoned memory, agent-to-agent privilege escalation.

Controls:
1. Deny-by-default capabilities and per-user tools; clear permission UX; tool grants with fine scopes, expiry and revocation.
2. Approval required for message sends, edits/deletes, purchases, external exposure, security changes and prod deployments unless separately preauthorized with limits. Never let prompts override policy.
3. Treat retrieved content/tool output as untrusted data. Separate instructions from external content. Sensitive operations policy-check at tool invocation, never only at initial task request.
4. Isolate private computers/containers by identity; shared sandboxes prohibited for sensitive personal workloads. Filesystem folders alone aren't isolation.
5. No implicit privilege inheritance between assistants/delegates. Delegates receive a limited subset of scopes; recursion, depth, concurrency and spend caps.
6. Credentials server-side encrypted using existing secrets mechanism; no tokens in prompt, model context, event logs or browser bundles. Secret rotation and redaction tests.
7. Memory isolation by tenant/user/assistant; explicit permission for shared memory, searchable provenance, freshness and deletion propagation. Support privacy export/delete semantics.
8. Durable task checkpoints; idempotency keys and reconciliation of externally executed side effects. Fail closed if approval status unknown.
9. Network egress allowlisting where appropriate; separate private/public services, no automatic exposure. Reuse Herdr/Tailscale/cloudflare exposure policies.
10. Audit every consequential step with actor, scope, policy result and evidence; protect logs from user-to-user access. Retention and legal/privacy review.
11. Supply-chain due diligence before vendoring Rakazo code; pin commit, scan dependencies/licenses, record copyright and upstream modifications.
12. Rate limits, prompt/tool budgets, host quotas, timeout and cancellation, bounded file size, malware inspection for attachments.

Gate criteria: cross-tenant read/write denied; injection suite prevents unauthorized action; revoked grant blocks queued and ongoing future actions; delegated child cannot escalate; secrets redacted; backups and deletion tested.
