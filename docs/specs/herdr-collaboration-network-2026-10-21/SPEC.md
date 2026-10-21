# Specification: Herdr Cross-Harness Collaboration Network (HCN)

## Product goal
Enable human users to connect their locally or remotely running Claude Code, Pi and other coding-agent harnesses to permissioned collaboration rooms in Herdr. Agents ask peer agents questions, delegate bounded work, negotiate proposals, review and jointly author durable artifacts. Human participants can read, comment, invite, pause, approve and audit in a web dashboard. No participant automatically gains a shell, files, private context, secrets, model credentials, or access to other participants' machines.

## Non-goals
- Do not build a replacement for Pi Engineering's workflow/orchestrator or Herdr's terminal/session management.
- Do not make agent messages executable instructions by default.
- Do not synchronize entire workspaces, private prompts or memory stores.
- Do not deploy unauthenticated MCP endpoints or expose Herdr's local socket/PTY over the public internet.
- No federated cross-organization execution in v1; design contracts for eventual federation.

## Existing capability audit, mandatory before coding
Locate the actual owned Herdr + Pi Engineering repositories and default branch. Record component paths and tests for: agent sessions/panes, event store, durable message queue, project/mission model, CLI and socket APIs, task delegation, agent registry, auth/SSO, authorization middleware, artifact storage, versioning, commenting/approvals, Web UI, WebSockets/SSE, telemetry, OpenViking/memory, remote connectivity, current Pi plugins. Mark each REUSE / EXTEND / CONSOLIDATE / NEW with evidence. Public `herdrdev/herdr` already advertises pane control, agent-to-agent prompting, and multi-machine connections; validate actual API contracts in local source. Do not fork public upstream until ownership and integration strategy is determined.

## Architectural boundaries
```
Human browsers --OIDC + session--> Herdr web/API
                                    |
                                    v
                       Collaboration control plane
                       - identity + policy decisions
                       - room membership + approvals
                       - task broker + event log
                       - artifact/version store
                                    ^
                                    | outbound authenticated stream / long poll
                             Local agent bridge
                          (per human machine or host)
                           /        |       \
                      Claude MCP    Pi      generic harness
```

Central control plane is the only authority for room membership, capability tokens, participant visibility, and durable artifact version commits. Local bridge alone can access local harnesses and local files; accepts only explicitly scoped, authenticated work requests. It never accepts arbitrary shell snippets through peer chat. Remote bridge initiates outbound connections to server; no inbound workstation ports. Existing Herdr runtime may provide bridge capabilities if audited as suitable; do not create an unnecessary daemon.

## Transport design
- Use standard MCP tools on both Claude and Pi as initial compatibility path: `herdr.rooms.list`, `herdr.rooms.join`, `herdr.messages.send`, `herdr.tasks.delegate`, `herdr.tasks.respond`, `herdr.artifacts.propose`, `herdr.artifacts.review`, `herdr.approvals.request`, `herdr.agents.list`. Tool contracts use project-scoped auth.
- Map agent-to-agent interactions to A2A task/message/artifact semantics through a gateway adapter. A2A becomes useful for independently deployed agents, not a second truth store.
- Browser consumes a typed events stream via SSE or WebSocket (reuse existing implementation). AG-UI can be adapted for visual event rendering, but must not create a second event store or confer authorization.
- Every inbound event has schema_version, event_id, org_id, project_id, room_id, actor_user_id, actor_agent_id, correlation_id, causation_id, created_at, visibility_scope and optional artifact_ref. Server derives identity from auth, never trusts identity fields supplied by agents.
- Delivery is at-least-once with idempotency keys, cursors, replay, timeout/dead-letter behavior, retry limits, and explicit cancellation. No guarantee of exactly-once distributed agent execution.

## Enrollment and identity
- Invite authenticated humans to organizations/projects/rooms. Organization-scoped OIDC/SSO + MFA for humans; passwordless guest links alone cannot trigger execution. Short session lifetimes; CSRF protection.
- Each agent is enrolled by its owning human and tied to agent_id, harness_type, device_id, instance_id and allowed project ids. Pair with a one-time token via an authenticated browser; rotate and revoke agent credentials. Bridge authenticates with narrowly scoped short-lived credentials; store secrets in OS credential store when possible.
- Separate identities and permissions for user, agent, bridge/device and service. Agent authority is a subset of its owner and must be further attenuated by each task delegation. Reject confused-deputy/cross-tenant transitions.
- Projects, rooms and artifact releases have distinct membership policies; guests must be explicitly added. Artifact visibility always checked at read and reference resolution, including search, previews, caches and notifications.

## Authorization matrix (default deny)
- Viewer: read only room events/artifacts explicitly shared.
- Contributor: chat, comment, propose new artifact versions; cannot mutate protected branches.
- Reviewer: comment, accept/reject artifact proposals per scope; no implicit execution privileges.
- Project manager: invite within policy and manage project permissions; no credential or machine access by default.
- Agent: scoped delegated rights (read permitted room messages, send messages, propose artifacts and bounded tasks); cannot self-grant permissions.
- Executor: optional explicit capability to run a named allowlisted task in a sandbox under human-approved policy; never raw remote shell.
- External guest: view/comment only by default. Guest artifact export and download governed by separate toggles, watermark optional.
- Permissions are evaluated for user ∩ agent ∩ device ∩ task ∩ resource, including expiry. Deny on ambiguity. Revocation cancels outstanding capability grants and drops active streams.

## Conversation and tasks
- Room supports humans, agents, threads, @mentions, read receipts (if policy), presence, task links, artifact links, and chronological audit.
- Agents have discoverable capabilities, supported tasks, maximum resource class and availability. Publish only non-sensitive capability summaries, no hostnames or filesystem paths to other users.
- Agent-to-agent request includes task goal, allowed artifact refs, permitted operations, constraints, budget, TTL, depth, max_turns and response destination. Target agent accepts/declines; sends structured results and evidence.
- Prevent infinite loops: hop budget (default 3), max agent turns per delegation, explicit approval for cross-user chaining, per-user/org rate limits and spend budgets, cycle detection by correlation graph, queue pressure/backoff.
- Human pause/cancel, escalation and approval checkpoints. Agent must show plan + references; peer chat output is untrusted source content.

## Artifact collaboration
- Artifacts have stable IDs and immutable version IDs, with content-addressed blobs and provenance. Track author human/agent, parent revisions, task IDs, source citations, review decisions and signature/hash.
- All writes are *proposals*: create candidate version against base_version; compare-and-swap for publish. Divergent base -> conflict and merge review, never last-write-wins.
- Specs and markdown: patch/diff + comments and section-level annotations. Code artifacts: PRs/worktrees and existing git review. Data outputs: immutable file versions with MIME, origin and explicit export rules.
- Secret scanning, malware scanning, MIME validation, file size limits and allowed artifact types before publishing. Avoid injecting confidential contents into LLM context unless participant permissions and external-model policy allow.
- Use existing artifact pipeline and object storage after audit. Do not invent another S3 bucket topology, database, document editor or file preview service if already present.

## UI
- Multi-user auth, project chooser, guest invitations and membership page.
- Collaboration rooms with mixed human/agent messages, typing/working/blocked status, machine privacy-preserving online state, mentions, agent capabilities.
- Task card: owner, delegated agent, status, progress, estimated work only if grounded in measurements, trace, budget, cancel, review, artifact links.
- Artifact pane: diff, provenance, version graph, comments, requested changes, approvals, revert.
- Agent graph: who asked whom, task handoffs, loops, latency, errors, token/cost usage if available, scope-based redaction.
- Operations page: enrollment, revoked devices, access denials, audit search, anomaly alerts and throughput. Do not expose terminal screen contents to guests.

## Minimal domain schema
```
organization(id, name, policy_version)
project(id, org_id, name, data_classification)
project_membership(user_id, project_id, role, expires_at)
agent_identity(id, owner_user_id, device_id, harness_type, status, revoked_at)
agent_project_grant(agent_id, project_id, allowed_actions, expires_at)
room(id, project_id, visibility, created_by)
room_membership(room_id, principal_type, principal_id, role)
room_event(id, room_id, actor_id, type, payload_ref, correlation_id, causation_id, created_at)
delegated_task(id, room_id, requester_agent_id, target_agent_id, scope, budget, status, expires_at)
artifact(id, project_id, type, classification, created_by)
artifact_version(id, artifact_id, parent_version_id, blob_ref, digest, created_by, state)
artifact_review(id, version_id, reviewer_id, decision, created_at)
capability_grant(id, subject_id, resource_id, allowed_actions, expires_at, revoked_at)
audit_event(id, org_id, actor_id, action, resource_id, outcome, occurred_at)
```
All FK queries are tenant-scoped and protected with data access controls. Schema indicative only: adapt to existing DB and model.

## API contracts (names indicative; adapt to existing APIs)
```
POST /v1/agents/enrollment/start
POST /v1/agents/enrollment/complete
POST /v1/agents/{id}/revoke
GET  /v1/projects/{projectId}/agents
POST /v1/projects/{projectId}/rooms
GET  /v1/rooms/{roomId}/events?cursor=...
POST /v1/rooms/{roomId}/messages
POST /v1/rooms/{roomId}/tasks
POST /v1/tasks/{taskId}/accept
POST /v1/tasks/{taskId}/result
POST /v1/tasks/{taskId}/cancel
POST /v1/artifacts/{artifactId}/proposals
POST /v1/artifact-versions/{versionId}/reviews
POST /v1/artifact-versions/{versionId}/publish
GET  /v1/audit?project_id=...&cursor=...
```
For each endpoint specify auth scope, tenant constraints, idempotency, pagination, error shape, event emission and rejection tests. Sensitive actions need reauthentication or approval where policy requires.

## Observability
Prometheus: active authenticated bridges, delegated tasks by state, task latency p50/p95, message delivery lag, rejected policy decisions, agent loop prevention, retry/dead-letter counts, artifact conflicts, guest access, auth failures, event replay lag. Privacy-preserving trace ids for audit and dashboard. No full prompts, secrets or private filenames in metrics/log labels.

## Compatibility and rollout
Feature flag off by default. Preserve Herdr existing endpoint generation-1 handshake and codecs; add separately advertised collaboration methods. Existing single-user workflows must continue unchanged. Start one server with two users and three agents, no public federation. Add federation only after external security review.
