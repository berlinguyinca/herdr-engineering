# 10 — Deployment and operations

- Add assistant capability to existing Herdr deployment units and configuration. Prefer feature flag `personal_assistant.enabled=false` by default and per-user opt-in.
- Follow existing fleet deployment, service discovery, API key/OIDC and DB migration process. Never modify current production directly from local one-shot prompt.
- Verify runtime and memory compatibility first; pin Rakazo source commit. Use staged migrations and backfills that can be rolled forward/back safely.
- Server-side secrets and connector OAuth tokens stored through canonical secret storage. Per-user installations scoped to principal.
- Leverage existing metrics and Grafana/Prometheus if available: runs/min, outcome rate, queue depth, latency, token/s and tokens used, tool failure, approval wait, memory query accuracy signals, recovery count, notifications and security denies.
- Structured logs with correlation IDs, run_id, assistant_id, actor and placement; privacy-safe. Traces span scheduler → runner → tool → model → artifact.
- Alert on stuck runs, repeated failures, queue runaway, privilege-denial spike, model/provider failure, and excessive spend.
- Runbooks for disable/kill switch, secret compromise, sandbox incident, requeue/reconcile, stale approval, lost worker, bad migrations, rollback.
- Release: migration test → feature flag deploy → internal single-user canary → multi-user test → staged availability. Block default-enabled until all phase gates pass.
