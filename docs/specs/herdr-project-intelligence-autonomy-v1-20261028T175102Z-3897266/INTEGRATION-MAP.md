# INTEGRATION MAP

Legend: **R** reuse as-is, **E** extend, **N** new (only where nothing exists).

| Spec concept | Integrates with | Interface | Mode |
|---|---|---|---|
| Normalized event | `fabric_events` envelope v2 | `repo.record_event(..., envelope=, require_durable=True)` ; `project_id`, `evidence_refs`, `visibility_scope`, `source`, `source_sequence`, `correlation/causation` | R |
| Spec fields absent from v2 (`tenant_id`, `security_label`, `supersedes_id`, `content_digest`) | v2 `org_id`, `visibility_scope`; payload `_pi` block | carried in payload + intelligence tables | E |
| Connector inbox / outbox | Postgres | `pi_inbox`, `pi_outbox` | N |
| Project, Investigation, Commitment, Decision, Relationship, Evidence | Postgres `pi_*` | repository class modelled on `github_store.py` (revision, tombstone, tolerates missing tables) | N |
| Repo→project mapping | `project_links.py` | `project_for_repository` | R |
| Explicit links | lineage events → `graph.py` | existing `/graph/edge` | R |
| Inferred links | `pi_relationship` (derivation, review_state) | — | N (separate by design, DECISIONS D1) |
| Timeline API | `api.py` | `GET /api/v1/projects/:id/timeline` etc., flag-gated | E |
| Artifact originals | `ArtifactStore` | `register_logical` → `put_stream` → `commit_version(idempotency_key)`; ACL grant by caller | R |
| Artifact events | `ArtifactRegistered` (legacy path) | adapter emits a registered-version event when `commit_version` is used | E |
| CI failure source | `ci_monitor`, `ci_fix` events | fold `CIFix*` events into incidents | R |
| Webhook intake | new `POST /api/v1/webhooks/github` | HMAC-SHA256 verify, delivery-id dedupe, reconcile via `github_app.py` | N (gap) |
| Repair execution | `CIFixService`, `ci-fix-runner.py` | request/claim; no new runner | R |
| Independent review + merge | `review_gate` | `decide_merge` preceded by `policy.evaluate()` | E |
| Placement | `host_policy`, `scheduler_service` | plan/reserve | R |
| Assistant tools | `assistant.py` | new catalog entries behind `request_tool_action` | E |
| Notifications | `attention_inbox` + `pi_outbox` | channel interface; transactional email = blocker | E |
| Deep links | `entity-links.js` | new kinds + allowlisted route schema | E |
| UI | `app.js` | 1 NAV entry (capability `project_intelligence_v1`) + 1 ROUTES entry; logic in `project-intelligence-view.js` | E |
| Retrieval cache | herdr-memory (`retain/recall/forget`) | permitted fragments only; forget on tombstone | E (D5) |
