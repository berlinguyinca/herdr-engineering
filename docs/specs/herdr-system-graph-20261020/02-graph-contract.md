# Typed graph contracts

Node types: Organization, Workspace, Repository, Branch, Commit, PullRequest, Package, Module, Symbol, Service, Endpoint, Deployment, Host, GPU, Model, Worker, AgentSession, Mission, Task, Plan, Review, Handoff, Request, Trace, Span, Queue, Artifact, Dataset, MemoryReference, ExternalDependency.

Edge types: CONTAINS, DEPENDS_ON, IMPORTS, CALLS, DEPLOYS_TO, RUNS_ON, SERVES, ASSIGNED_TO, SPAWNED_BY, BLOCKED_BY, PRECEDES, HANDOFF_TO, REVIEWS, IMPLEMENTS, CHANGES, PRODUCES, REFERENCES, ROUTES_TO, QUEUED_ON, OBSERVED_IN, BELONGS_TO, SUPERSEDES.

Every node/edge: stable globally namespaced ID, kind, source system, source object ID, tenant/workspace, observed_at, effective_from/to, revision, confidence, provenance pointer, permission scope, staleness status, last_verified_at. Edge direction and cardinality defined by schema registry. Distinguish observed (telemetry/code) vs inferred (heuristic/LLM) relationships; inference cannot silently overwrite observed facts.

Event envelope: event_id, schema_version, source_id, source_sequence, occurred_at, ingested_at, correlation_id, causation_id, trace_id, tenant_id, payload_hash, payload. Upsert by source+event_id; order by source sequence where available; tolerate out-of-order and duplicates. Tombstones and revisions preserve history.

Sample mission chain: Mission -> Task -> AgentSession -> Handoff -> Review -> PullRequest -> Commit -> Module -> Service -> RequestSpan -> Model -> GPU. Source links and evidence are required for each claimed connection.
