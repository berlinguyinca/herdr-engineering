# Unified workspace: implementation status (2026-10-30)

Honest status per phase. "Merged" means on main with the feature flag off by default; nothing was deployed.
Deploys are the operator's step.

| Phase | State | Where | Not done |
|---|---|---|---|
| P00 Audit | Done, except baseline screenshots (no Python Playwright on the host) | `audit/*`, `evidence/P00-RESULT.md`, smoke against production (GET only) | screenshots |
| P01 Contracts, ids, broker | Merged (library only, nothing calls the broker yet) | herdr-engineering #364 | route wiring is P05 |
| P02 Persistent Work Graph | Merged, flag off | herdr-engineering #387 (migrations 0033, 0034); Pi ids in pi-engineering #474 | scheduled live reconcile in production; graph UI |
| P03 Artifacts | Foundation merged; scheduled reconcile in the worker (off by default) | #380 (provenance, ACL-filtered lookups, resumable uploads, archive/tombstone/purge, reconcile; 0031) and #376 (skill registry) | share-link delivery, WebUI gallery, CLI/Pi `/artifact`, room-derived read access, scheduled reconcile trigger, agent-role skill install receipt |
| P04 Room client (Pi) | Partial | pi-engineering #521: `/room log` on the existing client | live follow (streaming), keychain storage, interactive composer, `@agent` completion, user Bearer mode; not tested against a live HCN |
| P05-P09 | Not started | | broker routing, attention/approval consolidation across seven mechanisms, Rakazo as an HCN participant, email/calendar/GitHub connectors, E2E and chaos tests |

## Decisions
- One artifact registry. The registry owns ids, versions, ACLs and sharing; RustFS owns bytes. #366's separate storage, archive and previews are re-expressed as registry features and #366 is not merged. See `design/ARTIFACT_INTEGRATION.md`.
- `project`, `team`, `artifact_version`, `external_action` are owned graph kinds (P02). Only the account owner approves `send_email`.
- The scheduler native-launch stack is skipped by choice and stays archived on `salvage/*`.

## Main repairs made on the way (each its own PR)
- `ui-gates`: #389 (lineage filter coverage), #420 (collab baselines after #406), #439 (missions mobile baseline after #427), #457 (dashboard colour-contrast ratchet after #406). No floor or rule was relaxed; #457 tightened one.
- `unit-gates`: #390 (inspector_snapshots test stub). P02/P03 test clash (committed versions are immutable) fixed inside #380.
- Open item for the operator: the focus-control floor on main was lowered to 470 in `643d2b5f` by someone else.

## Rollback
Every migration is additive with a `-- DOWN:`. Flags: `control_plane.unified_workspace.enabled: false` (default) turns the graph routes back into 404s. Reverting a squash commit on main removes a phase; the archive branches `salvage/main-checkout-snapshot-20261028` and `salvage/arch-collab-stack-20261028` hold the original work.

## Known limits of the evidence
- The full unit suite had one infrastructure failure when last run end to end: `test_postgres_recovery.py` lost its Postgres mid-run; it passes alone (19 passed).
- GitGuardian reported fake-token test fixtures from other authors' files as findings on #387 and #380. Marked as test credentials by the operator on #387; the #380 findings are still open in the dashboard.
