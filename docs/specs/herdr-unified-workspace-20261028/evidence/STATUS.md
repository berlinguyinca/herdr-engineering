# Unified workspace: implementation status (2026-10-30)

Honest status per phase. "Merged" means on main with the feature flag off by default; nothing was deployed.
Deploys are the operator's step.

| Phase | State | Where | Not done |
|---|---|---|---|
| P00 Audit | Done, except baseline screenshots (no Python Playwright on the host) | `audit/*`, `evidence/P00-RESULT.md`, smoke against production (GET only) | screenshots |
| P01 Contracts, ids, broker | Merged (library only, nothing calls the broker yet) | herdr-engineering #364 | route wiring is P05 |
| P02 Persistent Work Graph | Merged, flag off | herdr-engineering #387 (migrations 0033, 0034); Pi ids in pi-engineering #474 | scheduled live reconcile in production; graph UI |
| P03 Artifacts | Foundation merged; scheduled reconcile in the worker (off by default) | #380 (provenance, ACL-filtered lookups, resumable uploads, archive/tombstone/purge, reconcile; 0031) and #376 (skill registry); share links and `artifacts share` landed from another author (migration 0041); `artifacts show|versions|for|links|link`; room-derived read access (off unless `unified_workspace.room_artifact_access`) | WebUI gallery beyond the "Linked to" filter and links section (no comments, approvals or share UI), Pi `/artifact`, CLI `artifacts diff` (no server route), agent-role skill install receipt (the spec does not define the receipt) |
| P04 Room client (Pi) | Partial | pi-engineering #521: `/room log` on the existing client | live follow (streaming), keychain storage, interactive composer, `@agent` completion, user Bearer mode; not tested against a live HCN |
| P05 Teams, delegation, skills | Partial | herdr-engineering #482 (templates, admission, lease contract; library), #485 (durable Postgres leases, 0040), #489 (independent review enforced at task creation), #491 (review lineage in the work graph), #495 (safe retry of a completion), #500/#501 (crash takeover, rule A, off unless `control_plane.collab.task_lease_seconds`); `evidence/P05-RESULT.md` | Pi mission admission (contract not defined), Pi `/assign`/`/tasks` beyond the existing `/room assign`, scheduler placement (skipped by choice), result linking to issue, skill execution with audited artifacts, attention/approval consolidation |
| P06 Team-first WebUI and visual map | Partial (Team Map screen for one project; "Your teams" list page; explicit-team producer; browser gate only in CI) | `team-map-model.js`, `team-map-view.js`/`.css`, `#/team-map`, `teams-view.js`, `#/teams`, `/api/v1/graph/work/teams`, `herdr-eng teams`, client methods, 32 node tests, 15 Postgres-backed server/CLI tests; `evidence/P06-RESULT.md` | team-card Home with Attention, Conversation view, deep links into the map, non-operator team creation; browser/ui-gates run |
| P07 Rakazo agent and safeguarded external actions | Not started | | Rakazo as an agent with approval-gated external actions (email, calendar, GitHub) |
| P08 Subscriptions, attention and automations | Not started | | activity subscriptions, one attention/approval surface over the existing mechanisms, automations |
| P09 Reliability, threat model, accessibility and rollout | Partial (docs only) | `security/THREAT_MODEL.md` (implementation section, read from code, nothing run), per-flag rollout/rollback table, accessibility checklist; `evidence/P09-RESULT.md` | E2E, chaos, load, tracing, security tests, per-user flags, repair command, accessibility audit |

Team, room and assistant collaboration (design only, approved 2026-10-31, nothing built): `design/TEAM_COLLABORATION.md` (attach a team to a room, guest levels, coordinators, assistant briefs; build order, flags and the operator's three decisions of 2026-10-31; no phase state changes).

## Decisions
- One artifact registry. The registry owns ids, versions, ACLs and sharing; RustFS owns bytes. #366's separate storage, archive and previews are re-expressed as registry features and #366 is not merged. See `design/ARTIFACT_INTEGRATION.md`.
- `project`, `team`, `artifact_version`, `external_action` are owned graph kinds (P02). Only the account owner approves `send_email`.
- The scheduler native-launch stack is skipped by choice and stays archived on `salvage/*`.

## Main repairs made on the way (each its own PR)
- `ui-gates`: #389 (lineage filter coverage), #420 (collab baselines after #406), #439 (missions mobile baseline after #427), #457 (dashboard colour-contrast ratchet after #406). No floor or rule was relaxed; #457 tightened one.
- `unit-gates`: #390 (inspector_snapshots test stub). P02/P03 test clash (committed versions are immutable) fixed inside #380.
- Open item for the operator: the focus-control floor on main was lowered to 470 in `643d2b5f` by someone else.

## Open risks from the P09 read (see `security/THREAT_MODEL.md`, none fixed)
Opt-in task-lease fence, asynchronous room revoke, no artifact unlink, the member graph door and room-artifact access not running in the compose.collab topology, single-principal public share approval.

## Rollback
Every migration is additive with a `-- DOWN:`. Flags: `control_plane.unified_workspace.enabled: false` (default) turns the graph routes back into 404s. Reverting a squash commit on main removes a phase; the archive branches `salvage/main-checkout-snapshot-20261028` and `salvage/arch-collab-stack-20261028` hold the original work.

## Known limits of the evidence
- The full unit suite had one infrastructure failure when last run end to end: `test_postgres_recovery.py` lost its Postgres mid-run; it passes alone (19 passed).
- GitGuardian reported fake-token test fixtures from other authors' files as findings on #387 and #380. Marked as test credentials by the operator on #387; the #380 findings are still open in the dashboard.
