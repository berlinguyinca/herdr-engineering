# Staged delivery plan

## P0: Inventory and architecture decision (required first)
- Identify canonical editable repositories; establish branch and access. Inspect internal Pi Engineering/Fabric/Herdr and upstream public Herdr components, current artifacts, auth and event stores.
- Produce `docs/specs/herdr-collaboration-network-2026-10-21/audit.md` with path/test evidence and REUSE/EXTEND/CONSOLIDATE/NEW classifications.
- Confirm deployment: single organization, data classification, SSO provider, bridge topology and whether cloud/local models receive restricted context. Stop implementation of security-sensitive capabilities until resolved.
- Create threat model and ADRs for gateway vs in-process, auth policy engine, transport, artifact storage and protocol interop.

## P1: Hardened collaboration spine
- Central authenticated organizations/projects/rooms, user invitations, scoped roles, auditable room messages and stream.
- Agent enrollment, device-specific credentials, outbound connection and reconnect; permission enforcement and revoke.
- Tests: tenant isolation, auth negative cases, message idempotency, stream permission replay. Feature flag disabled by default.

## P2: Harness connectivity
- MCP tools for Claude Code and Pi that map to one collaboration API; connect two owners' agents through their own bridges.
- A2A adapter transforms incoming/outgoing tasks and artifacts without duplicating task lifecycle state.
- Tests: Claude A asks Claude B a question; B declines or answers; Pi joins via scoped invitation; no terminal access transferred.

## P3: Shared artifact collaboration
- Versioned immutable proposal/merge/review flows against existing Artifact Studio storage.
- Multi-agent edits to specification with diff and conflict handling. Read-only guest comments.
- Tests: concurrent proposals, approvals, reverts, denied private artifact dereference and secret detection.

## P4: Herdr web interface
- Room, task/event timeline, artifact diff, approvals, permissions and collaboration graph.
- Accessibility, pagination, reconnect/replay, reduced noise and manageable visibility across dozens of active workspaces.

## P5: Pilot and operations
- Pilot with 2-5 coworkers and three harness types, explicit opt-in and non-sensitive test repository.
- Dashboards + audit, incident runbook, backup and disaster recovery, security signoff.
- After stable pilot: reconsider organization federation via A2A mTLS and trust policy.

## First vertical-slice demonstration
1. User A creates room in project and invites User B by authenticated identity.
2. Each enrolls own Claude Code agent via locally running bridge; A also enrolls Pi.
3. Claude A asks Claude B for a review of a sanitized spec artifact.
4. Claude B returns a review with references; Pi adds second review.
5. Artifact proposal is visible in dashboard and cannot be published without configured reviewer approval.
6. User B loses room access; all API, stream, artifact and bridge operations immediately fail closed.
7. Audit displays the full chain without prompts/secrets from private contexts.

## Definition of done
Each milestone includes code + tests + migration + operator documentation + backwards compatibility + UI observability + concrete security regression checks. No new duplicate queue, event store, mission engine or memory system.
