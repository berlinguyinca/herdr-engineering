# P00 audit matrix (REUSE / EXTEND / CONSOLIDATE / NEW / BLOCKED)

Sources: herdr-engineering origin/main `fb964ed` at first audit; production now runs `676b442` (36 commits later, incl. merged #348/#349/#351, #317, #353/#357); pi-engineering origin/main `c270af3` (re-checked `6082fb9`); herdr-artifacts `1b651f0`; InferWeave boundary only.
Deployed column verified by read-only smoke tests and docker/systemd inspection: audit/SMOKE.md.

| Domain | Need | Tag | Evidence (paths in herdr-engineering unless noted) | Deployed (smoke 2026-10-28) |
|---|---|---|---|---|
| Rooms, membership, ACL, messages, room tasks | HCN owner | REUSE | control_plane/collab.py (+collab_*.py), ROLE_ACTIONS/AGENT_ACTIONS, ~40 tests/control_plane/test_collab*.py | DEPLOYED (2 rooms, 7 tasks; collab on `herdr-collab-public` + operator proxy; `collab_v1` flag absent on web health) |
| Cursor/subscribe | pi room client, WebUI | REUSE | collab.py read_events, room_state (long-poll), open_stream (SSE) | DEPLOYED (events, state 200) |
| Idempotent mutations | all writes | REUSE | collab.py `_idem` fingerprint on messages/tasks/delegations/proposals | source-only verified (tests); not exercised (GET only) |
| Audit | tamper evidence | REUSE | collab.py audit(), deploy/control-plane/collab-audit-verify.py | source-only (not probed) |
| Event log | graph source of truth | REUSE | fabric_events (0001, 0003 envelope v2, 0004/0005 seq/id per actor); events.py, sink.py | DEPLOYED (1.8M events ingested, source=postgres) |
| Work Graph substrate | projects/issues/tasks | EXTEND | control_plane/graph.py (node/edge types, scopes, can_see), graph_history.py, graph_artifact.py; NO `project`/`team` node, issue/task not owned | DEPLOYED BUT EMPTY (`/api/v1/graph/nodes` 200, 0 nodes for every type) |
| Durable tasks | persist beyond 24h TTL | EXTEND | collab.py TASK_TTL; graph task nodes | DEPLOYED (7 room tasks); TTL not verified |
| PI relationships | avoid second store | CONSOLIDATE | pi_relationship (0020) vs graph.py edges; decision D1 keeps inferred edges out of graph | source-only |
| Artifact registry/versions/ACL/publication | canonical | REUSE | control_plane/artifacts.py, artifact_acl.py, publication.py, rustfs.py, migrations 0003/0004/0006/0008/0010 | DEPLOYED (74 artifacts, `/api/v1/artifacts`, migration 0021 applied; 0 publications) |
| Artifact->task/issue/project linkage | P03 | EXTEND | artifact_bindings has workspace_id/room_id/issue_ref/commit_sha (0010); NO task_id, NO "artifacts for X" route | source-only gap confirmed (no route) |
| Two artifacts modules | hygiene | CONSOLIDATE | herdr_engineering/artifacts.py vs control_plane/artifacts.py | DOWNGRADED: unrelated modules (see SMOKE (d)); document, do not merge |
| Share links w/ TTL, revoke | P03 | NEW (on publication.py) | publication has audience+expires_at; no per-link token | source-only |
| Report renderers | P03 | BLOCKED on PRs #348-351 | rebase, do not reimplement | UNBLOCKED: #348/#349/#351 MERGED and DEPLOYED (676b442); rebase on main, reuse |
| Artifact client for agents | CLI/Pi adapter | REUSE | ~/IdeaProjects/herdr-artifacts (CONTRACT.md, skill/) | source-only (separate repo) |
| Binary storage | RustFS | REUSE | rustfs.py; compose `rustfs` service currently runs minio stand-in (config-only swap) | DEPLOYED as MinIO RELEASE.2025-09-07 (health rustfs=s3); rustfs.py exists |
| Attention | P08 | EXTEND | attention_inbox.py, collab_attention.py, attention-view.js; derive-on-read | DEPLOYED (`/api/v1/attention`, 38 items; room attention route 404) |
| Approvals | one actionable card | CONSOLIDATE | 7 mechanisms: proposals, steering, agent requests, user, egress, artifact review/publication, pa_approvals | partial: assistant approvals 200 (empty); others not probed |
| Rakazo backend | P07 | EXTEND | control_plane/assistant.py, intel_assistant.py, migration 0014 pa_*, /api/v1/assistant-* (flag personal_assistant.enabled, default off) | DEPLOYED (assistant_v1 on, Postgres JSON snapshot in `herdr_pa`; 10 smoke assistants; connectors/approvals empty) |
| Rakazo as HCN participant | P07 | NEW (adapter only) | no link assistant.py <-> collab.py today | source-only gap confirmed |
| Rakazo memory scopes | room/project | EXTEND | pa_memory is per (tenant,user); PR #317 (draft) binds ownership -> BLOCKED/coordinate | #317 MERGED and deployed (memory-hindsight, `herdr-memory-*` containers); ownership gap re-check in P07 |
| email/calendar/GitHub | P07 | NEW as Connector entries | intel_gmail*.py, intel_webhook.py are pure cores; no calendar anywhere | source-only (connectors list empty) |
| Team home / Team Map UI | P06 | NEW UI over EXTENDed API | collab-view.js (1580 lines) has no teams home/map; collab-flow.js is SVG base; nav-capabilities.js gate | source-only gap (UI served by cp-web; no teams route) |
| Layout persistence | P02/P06 | NEW | none exists | source-only gap |
| Pi: room client | P04 | NEW | no `pi room`, no `/room`; extend src/runtime/herdr/externalCli.ts (`herdr join|status|heartbeat|report|leave`) | source-only gap |
| Pi: credentials | P04 | EXTEND | herdr-external.json 0600, herdrConnection.ts api_key_file 0600; no keychain (0600 satisfies spec "keychain/0600") | source-only |
| Pi: task submit | P05 | REUSE+EXTEND | src/orchestration/externalEntry.ts (gated by PI_AUTOSPEC_ENABLED -> design decision), standaloneEntry.ts | source-only |
| Pi: leases/worktrees/roles/review independence | P05 | REUSE | ownership.ts, runtime/isolation/*, capability/roles.ts, orchestration/reviewGuard.ts | source-only |
| Pi: room/workspace/agent-instance ids on session events | P02 | EXTEND | ControlPlaneIngest.session (payload lacks them); envelope EnvelopeIds exist | CONFIRMED GAP: emit() does not merge YAML room_id (claim (c) FALSE) |
| Pi: artifact registration | P03 | NEW | only local ArtifactStore | source-only |
| Pi: skills registry | P05 | BLOCKED | no skills system in Pi source; need owner decision | source-only |
| Pi standalone | P04/P09 | EXTEND | everything off by default; add proving test (INV-013) | source-only |
| InferWeave | inference only | REUSE (no change) | llm_proxy.py identity headers, graph_trace.py; no scheduler work in any phase | deployed elsewhere (llm-proxy container `herdr-llm-proxy` present); not probed |
| Fabric scheduler | host placement | REUSE | scheduler.py; PRs #322/#327 in flight | DEPLOYED (fabric gateway runs older 17d2b21b3149) |

Ownership confirmed: HCN CollabService=rooms; Pi=execution/missions/leases; Artifact Registry+RustFS=artifacts; fabric_events+Postgres=graph; assistant.py=Rakazo; InferWeave=inference. Zero new services proposed.
