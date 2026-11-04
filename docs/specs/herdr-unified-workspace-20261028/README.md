# Herdr Unified Collaboration Workspace — Implementation Program

Package: `herdr-unified-workspace-20261028`  •  Specification revision: 1.0  •  2026-10-28

## Product outcome
A simple team-first Herdr dashboard and native Pi terminal interface to the SAME HCN rooms, work, graph, and artifact registry. Rakazo is the personal-assistant participant, NOT a competing orchestrator. Users see teams, plain-language progress, attention and results first; detailed agents/hosts/logs only on demand. Work Graph is durable for projects/issues/tasks long after sessions end. Every generated artifact is searchable from its project, task and issue.

## Ownership constraints
- Herdr HCN `CollabService` owns rooms, membership, permission checks, messages and room tasks.
- Pi Engineering owns agent execution, skills, missions, worker leases, worktrees, model roles and review handoffs.
- Existing Herdr Artifact Registry owns artifact IDs, immutable revisions, metadata and sharing; existing RustFS/S3 storage owns binary contents.
- Existing Herdr fabric events / PostgreSQL are event source + graph projections. No new Neo4j or parallel chat DB.
- Existing Rakazo integration is personal assistant; integrations are gated through user-scoped capabilities and approvals.
- InferWeave owns inference routing, model metrics and GPU resource decisions; no parallel scheduler.
- Preserve standalone Pi operation when HCN or InferWeave is unavailable. No Pi-Bolt. Do not introduce unsupported/competing UI or orchestrator projects.
- The WebUI is for conversation, inspection, annotations, approval and sharing; CLI/Pi commands and authorized agent skills author/modify artifacts.

## Source audit anchors (MUST revalidate against checkout and active PRs)
`herdr-engineering/herdr_engineering/control_plane/collab.py`, `collab_mcp.py`, `collab_a2a.py`, `packages/collab-view.js`, `collab-flow.js`, `session-workspace.js`, `artifacts-view.js`, `personal-assistant-view.js`, `attention-view.js`, `nav-capabilities.js`; Pi `src/runtime/herdr/CollabLayout.ts`, `src/runtime/herdr/envelope.ts`, `src/orchestration/externalEntry.ts`, `docs/integration/COLLAB_HERDR_LAYOUT.md`, `docs/specs/herdr-federated-engineering-20261022/T00-AUDIT.md`. Audit existing Rakazo source, integration and active phase work; do not assume UI code implies a working backend.

## Non-negotiable invariants
1. One authoritative entity per domain; adapters/projections may be rebuilt.
2. Existing grants intersect with user action policy; room membership NEVER grants shell or external account access.
3. Append-only evidence and versioned artifacts, explicit deletion policy and tombstones.
4. Stable typed identities and relationships; task/issue/project lookup always returns its linked artifacts authorized for the viewer.
5. All writes idempotent with audit trail; avoid claiming exactly-once delivery on unreliable networks.
6. One default view: conversation, task summary, and attention. Detail progressive only.
7. No overlapping implementations; audit REUSE / EXTEND / CONSOLIDATE / NEW and open PRs first.
8. Each gate requires passing tests + runnable evidence + rollback before advancing.

## Ordered dependency graph
P00 inventory -> P01 contracts -> P02 persistent work graph -> P03 artifact linkage -> P04 Pi terminal room client -> P05 Pi task/skills execution -> P06 team dashboard -> P07 Rakazo + external actions -> P08 event automations/attention -> P09 hardening + E2E rollout.
P03/P04 can be developed in separate worktrees AFTER P01/P02 contracts freeze; P05 requires P04 and task APIs; P06 requires P02/P03; P07 requires P01/P03 and approval contracts. Merge strictly in dependency order. Stop on failed gates, conflict, or schema incompatibility.

## Critical acceptance journey
A user opens Herdr, sees teams and one priority item, enters a room, asks in natural language to investigate a CI failure, sees a single task card and team map, reconnects through `pi room attach` from another host, inspects plan/PR/test artifacts, asks Rakazo to draft an email from the approved report, authorizes sending, and revisits the project months later to find the exact immutable report versions and causal history. No extra setup or command knowledge necessary for basic workflow.
