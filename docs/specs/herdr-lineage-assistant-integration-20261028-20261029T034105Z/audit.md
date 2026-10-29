# P0 audit: lineage, continuity and assistant integration

Audited 2026-10-29. Evidence is read from `origin/main` of each repo, not from local working trees
(the local `herdr-engineering` checkout was 862 commits behind and dirty; `pi-engineering` was on a stale feature branch).
This is a code-reading audit. **No test suites were run in P0**; "verified" means tests exist and were read.

| Repo | SHA read |
|---|---|
| herdr-engineering | `694aba1` (main) |
| pi-engineering | `6082fb9` (main) |
| autospec | `d490d366` |
| autospec-constitution | `507356d` |
| autospec-baselines | `e269f56` |
| autospec-design | `39b8a23` |
| elie222/rakazo (Apache-2.0) | `3a0155f` (shallow clone) |

Classification: **V** implemented and verified, **I** implemented but incomplete, **U** implemented but untested,
**M** missing, **C** conflicting or duplicated.

## Requirements matrix

### Lineage graph (Herdr)
| Requirement | Class | Evidence |
|---|---|---|
| Envelope v2 ingest, per-principal idempotency, 409 on same id/different content | V | `control_plane/api.py:1721`; `tests/control_plane/test_envelope_v2.py:37` |
| Out-of-order/duplicate/shuffle determinism, rebuild from `fabric_events` after restart | V | `test_graph.py:93,133,461,749`; `tests/e2e/test_s6_chaos.py:90` (needs Postgres) |
| Fail-closed visibility, membership at read time | V | `test_graph_membership.py:64-134`; `test_s3_room_graph.py` |
| All 24 node kinds and 11 edge kinds from spec section 3 | V | `graph.py:56-77` |
| Contract doc lists node kinds | C | doc line 6 lists 24; code has ~50 (system-graph kinds) |
| Handoff projection | C | `handoffs.py:143` builds `handoff:<role>:<id>` pseudo-nodes, not lineage `handoff` kind; `graph_mission.py:10-14` emits mission-to-mission `handoff`, reversed spawn direction |
| Graph + timeline + replay + inspector + search/filter UI | I | `packages/collab-lineage.js` (625 lines) |
| Pan/zoom, focus agent/branch, virtualization | M | grep for zoom/wheel/drag finds nothing; 300-node table fallback only |
| Inspector: mission status, blockers, next action, PR/CI summary, artifact links, terminal click-through | M | inspector says "offers no terminal" (`collab-lineage.js:584`) |
| 60+ sessions bounded render | I | contract says "Not load-tested at that size" |

### Continuity (Pi)
| Requirement | Class | Evidence |
|---|---|---|
| `LineageEmitter`, nonblocking, deterministic ids | V | `src/runtime/herdr/lineage.ts:272,813`; `test/unit/lineage-emitter.test.ts` |
| Session rollover lineage | I | `sessionRolledOver` (`lineage.ts:533`) only inferred from a changed `session_id` (`missionStore.ts:654-678`); no trigger, no edge between sessions |
| Handoff edge in production | I | `transitionHandoffRecord` (`missionStore.ts:2424`) has no production caller; edge only at `acknowledged`/`committed` (`lineage.ts:623`) |
| Context-budget rollover trigger (70/80/90) | M | four unrelated constants: 0.75 `phaseBoundaryHandoff.ts:18`, 0.8 `context/usage.ts:30`, 0.7 `workers/contextBudget.ts:15` |
| Phase-boundary handoff | I | `orchestrator.ts:1785`, gated on `opts.phaseHandoff`, which nothing in src passes |
| Quota trigger | I | `runtime/host/quotaResume.ts` pure, no src importer |
| Checkpoint contents | I | `orchestration/types.ts:377-403`: no untracked snapshot, test pass/fail split, decisions, unresolved bugs, next action, memory refs |
| Verify checkpoint before revoking source lease | M | only authority is asserted (`missionStore.ts:2601`); `checkpointVerified` name is misleading |
| Logical-writer fencing (generation + token) | V | `ownership.ts:97-380`; `missionStore.ts:2082`; `orchestration-checkpoints.test.ts` |
| Per-worktree exclusive writer | I | lanes/repo leases exist; no explicit worktree lock found |
| Runaway OS process termination | M | worker abort is in-process `session.abort()` only |
| 10+ sequential handoffs, cross-host, crash at every stage, 60+ sessions | M | none found |
| Duplicate mechanisms | C | two checkpoints (`orchestration/checkpoints.ts` live, `resilience/checkpoint.ts` unused); three handoff paths (store, `plannerWorker/handoffCommand.ts`, `liveHandoff.ts`), only the store emits lineage |
| `room_id` on Pi lineage | I | Pi omits it, so Pi lineage is operator-only on Herdr side (contract "Known limitations") |
| e2e suites in `npm test` | C | `test/e2e/*.e2e.test.ts` not in `npm test`; no CI workflow files found in pi-engineering |

### Collaboration, artifacts, UX
| Requirement | Class | Evidence |
|---|---|---|
| Room API, roles, 404/403 model, message idempotency | V | `collab.py:84-90,1249,5083-5138`; `test_collab_message_artifacts.py` |
| Pi `/room` or `/mission` TUI attach | M | Pi `/mission` is local orchestrator; only headless `externalAgent.ts` joins rooms |
| Artifact versions, CAS, content addressing, ACL | V | `artifacts.py:74-260`; `artifact_acl.py`; `test_artifact_acl.py` |
| Artifact to graph link | M | only `_promote` (`collab.py:4916`) writes a `produced` edge, for delegation proposals |
| Email attachment to scoped artifact | I | `intel_attachments.ingest` registers; no graph link, no audience ACL, no retraction flow |
| Mobile/desktop UI gates and 88 baselines | V | `tests/playwright/ui-gates.yaml`; Missions and assistant have no baselines |
| OpenViking in Herdr repo | M | spec-only; Pi has `src/blackhole/OpenViking.ts` |

### Personal assistant
| Requirement | Class | Evidence |
|---|---|---|
| Herdr-native assistant, flag default off, grants, approvals, audit, per-user isolation | V | `assistant.py:84,1396,1623-1689`; `test_assistant_service.py`; `test_assistant_memory_security.py` |
| Assistant reads lineage graph/timeline/CI/artifacts (7A) | M | no graph tool; `read_fleet` is counts only and not principal-scoped (`assistant.py:551`) |
| `request_mission`, `request_review`, `propose_branch`, `comment_on_node` (7B,E) | M | `delegate` writes a `queued` record with no executor (`assistant.py:2114`) |
| CI watch and diagnose (7C) | I | `ActionBoard.request_ci_repair` (`intel_assistant.py:90`) not on assistant tool path |
| Gmail draft/send (7D) | M | `send_message` has an approval gate but no executor; `intel_gmail.py` is inbound only |
| Digests and notifications (7F) | C | `NotificationPlanner` plans, never sends, unwired |
| Explain routing and provenance (7G) | M | |

## Security findings in the existing assistant (fix before widening it)
1. `preauthorized=true` on a grant skips approval and any authenticated principal can create it over HTTP (`api.py:3028-3033`); requester and approver are the same principal.
2. Empty command allowlist allows any command once `allow_shell` and a `run_shell` grant exist (`assistant.py:2078`).
3. `POST /assistant-connectors/{id}/request` bypasses grants and approvals (`api.py:3093`).
4. Request hash is not bound to principal, room or mission; no action TTL; no dry-run (`assistant.py:1623-1659`).
5. `read_fleet` shows fleet data to any authenticated user.
6. `GET /{id}/routines/due` is unreachable (`api.py:2938-2942`).
7. Tenant is hardcoded `"default"`; "immutable" audit log is an in-memory list.

## Corrections to the specification package
- The spec says GitHub Actions disabled everywhere; `gh api` shows enabled on both repos, but CI of record is Woodpecker (`.woodpecker/`) and no recent GH runs exist.
- Spec section 6 says "extend Missions UI"; the lineage UI is the Collaboration "Lineage" tab (`collab-view.js`), and the Missions/Workbench pages have no screenshot baselines.
- AutoSpec already has `docs/specs/herdr-federated-engineering-20261022/` (T00-T17) that overlaps spec sections 2, 5, 9. Reconcile, do not redo.
- AutoSpec review quorum is already exact-SHA bound and can publish to Herdr (`review_quorum/herdr.rs`, off by default). The CI-failure-to-repair-to-re-review loop is the missing piece (**I**).
