# 05 — Security, privacy, reliability and operability

## Tenancy and authorization
Classify sources by owner, organization, project and data sensitivity. Source ACL and project ACL both restrict derived claims and search. Personal Gmail and employer/scientific data must remain separate unless explicitly authorized per connector and project. No automatic publishing or external LLM forwarding of private evidence. Support per-model data-egress policies: local-only, approved cloud, prohibited. Tool consent is not data consent.

## Access controls
Separate permissions for ingest, read, link, propose, execute, approve, merge, publish and administer. Privilege service identities by operation. High-risk approvals bind actor, exact action payload, expected resource version and expiry. Audit admin overrides. CSRF/OAuth state/PKCE as applicable; validate webhook signatures and protect against replay. Remote MCP/OpenAPI connectors use allowlisted destinations and SSRF controls.

## Untrusted input
Email, attachments, plots, CI logs, repository text, issue content and agent reports are data, not executable instructions. Extraction runs isolated with content-type and archive limits, antivirus where supported, attachment preview sanitization, no active HTML/script rendering. Model output cannot directly execute shell or alter policy. Prevent artifact/public share link leakage. Subject each suggested action to deterministic validator and explicit principal.

## Retention and data lifecycle
Per-source retention and revocation, deletion propagation through derivatives, search and embeddings. Legal/compliance records may require hold rather than deletion; reflect source policy. Purge temporary copies, make encrypted backups and test restores. Preserve audit records consistent with retention policy without retaining forbidden content. Credential rotation and reauthorization must not invalidate data ownership scoping.

## Observability
Metrics: ingest lag, webhook/drop rate, duplicate suppression, parser failure, extraction cost/latency, link precision/recall, review queue, agent recovery success, CI false positives, auto-merge rate, post-merge regression, notification volume, UI query latency. Structured logs trace event/incident/artifact IDs without private contents. Dashboard health pages and alerts. SLO proposals set after baselines, not invented measurements.

## Rollout controls
Flags per tenant/project/source and capability: ingest-only, derive, suggest, execute, auto-merge. Defaults read-only. Kill-switch immediately blocks new autonomous actions, not merely UI buttons. Backpressure to protect existing fabric and older GPU hosts; per-connector rate limits. No autonomous deployment to laptop/Mac by default. Explicit migration rollback, staged rollout and replay drills.
