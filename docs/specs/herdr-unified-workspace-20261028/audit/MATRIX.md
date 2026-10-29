# P00 audit matrix (REUSE / EXTEND / CONSOLIDATE / NEW / BLOCKED)

Sources: herdr-engineering origin/main `fb964ed`; pi-engineering origin/main `c270af3`; herdr-artifacts `1b651f0`; InferWeave `inferweave` (boundary only).
All "deployed" status is UNVERIFIED (source audit only; see evidence/P00-RESULT.md).

| Domain | Need | Tag | Evidence (paths in herdr-engineering unless noted) |
|---|---|---|---|
| Rooms, membership, ACL, messages, room tasks | HCN owner | REUSE | control_plane/collab.py (+collab_*.py), ROLE_ACTIONS/AGENT_ACTIONS, ~40 tests/control_plane/test_collab*.py |
| Cursor/subscribe | pi room client, WebUI | REUSE | collab.py read_events, room_state (long-poll), open_stream (SSE) |
| Idempotent mutations | all writes | REUSE | collab.py `_idem` fingerprint on messages/tasks/delegations/proposals |
| Audit | tamper evidence | REUSE | collab.py audit(), deploy/control-plane/collab-audit-verify.py |
| Event log | graph source of truth | REUSE | fabric_events (0001, 0003 envelope v2, 0004/0005 seq/id per actor); events.py, sink.py |
| Work Graph substrate | projects/issues/tasks | EXTEND | control_plane/graph.py (node/edge types, scopes, can_see), graph_history.py, graph_artifact.py; NO `project`/`team` node, issue/task not owned |
| Durable tasks | persist beyond 24h TTL | EXTEND | collab.py TASK_TTL; graph task nodes |
| PI relationships | avoid second store | CONSOLIDATE | pi_relationship (0020) vs graph.py edges; decision D1 keeps inferred edges out of graph |
| Artifact registry/versions/ACL/publication | canonical | REUSE | control_plane/artifacts.py, artifact_acl.py, publication.py, rustfs.py, migrations 0003/0004/0006/0008/0010 |
| Artifact->task/issue/project linkage | P03 | EXTEND | artifact_bindings has workspace_id/room_id/issue_ref/commit_sha (0010); NO task_id, NO "artifacts for X" route |
| Two artifacts modules | hygiene | CONSOLIDATE | herdr_engineering/artifacts.py vs control_plane/artifacts.py |
| Share links w/ TTL, revoke | P03 | NEW (on publication.py) | publication has audience+expires_at; no per-link token |
| Report renderers | P03 | BLOCKED on PRs #348-351 | rebase, do not reimplement |
| Artifact client for agents | CLI/Pi adapter | REUSE | ~/IdeaProjects/herdr-artifacts (CONTRACT.md, skill/) |
| Binary storage | RustFS | REUSE | rustfs.py; compose `rustfs` service currently runs minio stand-in (config-only swap) |
| Attention | P08 | EXTEND | attention_inbox.py, collab_attention.py, attention-view.js; derive-on-read |
| Approvals | one actionable card | CONSOLIDATE | 7 mechanisms: proposals, steering, agent requests, user, egress, artifact review/publication, pa_approvals |
| Rakazo backend | P07 | EXTEND | control_plane/assistant.py, intel_assistant.py, migration 0014 pa_*, /api/v1/assistant-* (flag personal_assistant.enabled, default off) |
| Rakazo as HCN participant | P07 | NEW (adapter only) | no link assistant.py <-> collab.py today |
| Rakazo memory scopes | room/project | EXTEND | pa_memory is per (tenant,user); PR #317 (draft) binds ownership -> BLOCKED/coordinate |
| email/calendar/GitHub | P07 | NEW as Connector entries | intel_gmail*.py, intel_webhook.py are pure cores; no calendar anywhere |
| Team home / Team Map UI | P06 | NEW UI over EXTENDed API | collab-view.js (1580 lines) has no teams home/map; collab-flow.js is SVG base; nav-capabilities.js gate |
| Layout persistence | P02/P06 | NEW | none exists |
| Pi: room client | P04 | NEW | no `pi room`, no `/room`; extend src/runtime/herdr/externalCli.ts (`herdr join|status|heartbeat|report|leave`) |
| Pi: credentials | P04 | EXTEND | herdr-external.json 0600, herdrConnection.ts api_key_file 0600; no keychain (0600 satisfies spec "keychain/0600") |
| Pi: task submit | P05 | REUSE+EXTEND | src/orchestration/externalEntry.ts (gated by PI_AUTOSPEC_ENABLED -> design decision), standaloneEntry.ts |
| Pi: leases/worktrees/roles/review independence | P05 | REUSE | ownership.ts, runtime/isolation/*, capability/roles.ts, orchestration/reviewGuard.ts |
| Pi: room/workspace/agent-instance ids on session events | P02 | EXTEND | ControlPlaneIngest.session (payload lacks them); envelope EnvelopeIds exist |
| Pi: artifact registration | P03 | NEW | only local ArtifactStore |
| Pi: skills registry | P05 | BLOCKED | no skills system in Pi source; need owner decision |
| Pi standalone | P04/P09 | EXTEND | everything off by default; add proving test (INV-013) |
| InferWeave | inference only | REUSE (no change) | llm_proxy.py identity headers, graph_trace.py; no scheduler work in any phase |
| Fabric scheduler | host placement | REUSE | scheduler.py; PRs #322/#327 in flight |

Ownership confirmed: HCN CollabService=rooms; Pi=execution/missions/leases; Artifact Registry+RustFS=artifacts; fabric_events+Postgres=graph; assistant.py=Rakazo; InferWeave=inference. Zero new services proposed.
