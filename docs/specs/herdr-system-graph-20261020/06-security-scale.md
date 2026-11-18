# Security, reliability and scale

Auth: reuse Herdr identity/RBAC; enforce workspace, repository, mission, and trace ACLs at ingestion, graph query, subscriptions, export and shared artifact links. A graph edge must never leak existence of a hidden node. External collaborators see only explicitly shared projections. Audit access and sharing changes.

Secrets and private prompt contents excluded by default; redact logs, environment variables and sensitive source snippets. Per-source provenance and permission filtering. Prevent cross-tenant joins and injection through repository metadata or generated HTML.

Targets to validate: 100 repositories, 10k visible nodes with clustering, 1M persisted edges, 50 concurrent viewers, reconnect/replay after 24h offline, p95 initial scoped graph < 2s and incremental updates < 2s. These are acceptance targets, not measured current capacity.

Observability: ingestion lag, queue depth, dropped/duplicate events, projection age, websocket reconnects, query latency, render latency, tracing sampling coverage, auth-denied counts, disk growth. Provide dashboards and alerts. Versioned migrations, backups, canary rollout and feature flags with one-click disable of graph UI.
