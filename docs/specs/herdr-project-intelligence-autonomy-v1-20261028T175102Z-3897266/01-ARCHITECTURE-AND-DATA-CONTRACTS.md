# 01 — Architecture, event and entity contracts

## Service boundaries
- **External source connectors** fetch immutable source references, metadata and permitted contents and publish normalized events.
- **Project Intelligence** resolves project/investigation, extracts supported observations, links evidence, records decisions/commitments and maintains materialized read models.
- **Autonomous Operations** subscribes to actionable signals and delegates to existing Pi Engineering, CI and Herdr operations under approval policy.
- **Assistant interface** is Rakazo/Pi tool adapter for query/explain/navigate/approve; never runs privileged mutations directly.
- **Storage** is the existing RustFS/artifact registry for immutable blobs and derived versions; PostgreSQL for durable transactional data; OpenViking for context retrieval only, not the authoritative ledger.
- **UI** is the existing dashboard frontend, reusing auth, search, navigation, design tokens, notifications and mobile patterns.

## Canonical event envelope (logical schema)
```
Event {
  event_id: UUID, tenant_id: UUID, source_system: string,
  source_instance: string, source_id: string, source_version: string?,
  event_type: string, occurred_at: UTC timestamp, ingested_at: UTC timestamp,
  actor_ref: EntityRef?, project_ids: UUID[], investigation_ids: UUID[],
  object_ref: EntityRef, provenance_refs: EvidenceRef[],
  payload_schema_version: integer, payload: object,
  security_label: string, visibility_scope: Scope,
  correlation_id: UUID?, causation_id: UUID?, trace_id: string?,
  content_digest: SHA256?, supersedes_id: UUID?
}
```
Unique dedupe: `(tenant_id,source_system,source_instance,source_id,source_version,event_type)` or a source-specific stable semantic key. Event corrections append amendments; never silently alter history. At-least-once delivery, transactional inbox/outbox, cursor checkpoints, reprocess version. Sequence by occurrence and ingestion; show late arrivals transparently. Detect backdated source edits.

## Core entities
Project, Investigation, SourceObject, Person/IdentityAlias, Organization, EmailThread, EmailMessage, Attachment, Artifact, ArtifactVersion, Plot/Dataset, Repository, Commit, GitHubIssue, PullRequest, Review, CICheck, WorkflowRun, Agent, AgentRun, ModelInvocation, Task/Commitment, Decision, Hypothesis, Finding, TestEvidence, Incident, ActionProposal, Approval, AutomationRun, Notification, Relationship.

Minimal common columns: id, tenant_id, project scope, canonical external ID, source provenance, created/updated/observed timestamps, security label, ownership and lifecycle status. Never merge persons based solely on display name. Support aliases and reversible entity merges.

## Relationship model
```
Relationship { id, from_ref, to_ref, predicate, evidence_refs[],
  derivation: explicit|inferred|human_verified|system_verified,
  confidence: decimal|null, model_id?, prompt_version?, extractor_version?,
  verified_by?, verified_at?, valid_from?, valid_to?, superseded_by?,
  security_scope, review_state }
```
Examples: `reported_by`, `attached_to`, `mentions`, `addresses_issue`, `proposes_fix_for`, `tested_by`, `reviewed_by`, `implements`, `depends_on`, `assigned_to`, `requested_by`, `contradicts`, `supersedes`, `blocks`, `resolved_by`. Relationship cannot imply permission or ownership without verified source. Uncertain links are visibly provisional and editable; corrections are versioned and fully auditable.

## Evidence and claims
```
EvidenceRef { source_ref, version_id?, anchor_type, anchor_value,
  byte_range?, page?, cell_range?, email_message_id?, git_sha?,
  redaction_policy, content_hash? }
Finding { statement, evidence_refs[], uncertainty, contradicted_by[],
  extractor_version, model_identifier, human_state, recorded_at }
```
Exact anchor to email part, attachment bytes, page, chart series/point, code line+commit, CI log offset, agent execution event. Preserve immutable original when allowed; if source must be deleted, tombstone evidence and re-evaluate derived outputs. No fabricated quotes or unsupported citations.

## Project timeline read model
Unified ordered activity: `event_id, title, category, occurred_at, observed_at, actor, evidence_refs, involved_entities, confidence, consequence, action_state, visibility`. Aggregate by thread/incident with expandable subevents. Filters by person, repository, source, artifact, date, evidence type, status and model. Support as-of queries and historical replay of decisions and entity resolution.

## API interface proposals — adapt to existing conventions
Read: list projects, inspect project, list/search timeline, inspect event, inspect evidence, list investigations, traverse graph, list outstanding commitments, list recent artifacts, summarize activity with evidence.
Write: link/unlink entities, confirm/reject inference, assign owner with authority, propose issue, accept action, pause ingestion, request backfill. All writes require conditional versions/idempotency keys and audit identities.
Assistant tools: `herdr.projects.search`, `herdr.timeline.query`, `herdr.evidence.open`, `herdr.investigations.explain`, `herdr.tasks.propose`, `herdr.actions.request`, `herdr.actions.status`, `herdr.dashboard.navigate` (typed deep link). Tool gateway enforces scopes and rate limits.

## Persistence strategy
Prefer existing PostgreSQL migrations and indices (tenant_id + project_id + occurred_at, source uniqueness, entity refs, full-text metadata). Use a typed relationship table before introducing a separate graph database. Batch ingest and bounded worker queues; RustFS for blobs. Content-addressed storage with per-tenant ACL and de-duplication that does not cross security boundaries. OpenViking caches only permitted document fragments with expiry and deletion propagation.
