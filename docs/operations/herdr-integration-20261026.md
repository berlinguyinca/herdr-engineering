# HerdR integration recovery — 2026-10-26

## Required outcome

Consolidate the existing dashboard design and runtime work without losing functionality,
durable state, or UX. Verify the consolidated release locally before deployment.
Keep the original canonical checkout and all source branches recoverable.

## Source and integration state

- Integration checkout: `/home/wohlgemuth/IdeaProjects/.worktrees/herdr-complete-20261026`.
- Integration branch: `integration/herdr-complete-20261026`.
- Recovery checkpoint: `8bc7c7e` preserves the first integration attempt, including
  artifact, scheduler, handoff, and publication additions.
- Current upstream includes Pi snapshots, workspace mission namespaces, mission
  delivery, analytics, architecture, graph, collaboration, and host telemetry.
- UI polish adds themes, skeletons, freshness, reduced motion, and responsive views.
- CI polish adds the command center, diagnostics, and bulk controls.

## Root causes identified

The canonical `main` checkout lagged the remote by hundreds of commits. The first
deployment therefore used an old UI, even though newer work existed in Git.
Subsequent wholesale conflict resolutions selected old feature-file versions and
removed newer router, API, repository, graph, analytics, and collaboration behavior.
Those replacements must be reconciled against current upstream with additive ports.

The structured entity tables are not the complete durable state. Event replay
reconstructs missions, sessions, delivery, usage analytics, graph, host metrics,
and other projections. Disabling replay made startup faster by omitting product
state. Full-history replay is required until complete projection checkpoints and
durable history reads are implemented and verified.

The previous deployment also repurposed HOME and mounted configuration from `/tmp`.
This bypassed normal receipts and rollback. Future deployment must use the real
deployment state directory and a durable configuration path. Credential drift must
be verified through actual authentication, without exposing credentials.

## Acceptance gates

1. Preserve all upstream routes, identities, permissions, and replay projections.
2. Wire artifact, scheduler, handoff, publication, and client additions end to end.
3. Preserve themes, skeletons, freshness, keyboard focus, reduced motion, mobile
   layouts, and current CI/mission/agent inspection.
4. Bound request work, retain actionable error causes, and expose request IDs,
   accurate latency/error metrics, and retry behavior.
5. Verify restart replay against delivery, graph, usage, stage, namespace,
   artifact, and scheduler tests. Do not discard history to meet startup budgets.
6. Pass targeted and full Python/JavaScript/UI checks, document baseline lint
   issues separately, and inspect rendered desktop/mobile views.
7. Build from the committed integration revision, run a candidate smoke test,
   establish durable rollback/configuration, then verify live provenance and UX.

## Current validation evidence

- Repository reconciliation: 532 targeted tests passed.
- API/resource reconciliation: 58 tests passed before the nested ACL regression
  test was added; the nested ACL test then failed and passed after its fix.
- Request metrics and saturation behavior: three tests passed.
- The second broad Python run completed: 7,135 passed, two failed, 65 skipped,
  two xfailed. Both failures exposed the same obsolete download ACL guard,
  which passed a structured principal into the artifact ACL. The guard was
  removed because the enclosing resource guard already enforces the correct
  stable artifact principal. Subsequent artifact-family checks passed 103 tests,
  API checks passed 63, and real Postgres checks passed ten. The broad command
  itself is not reported as green.
- Integration has not been deployed.

## Full-history benchmark

The read-only replay against the actual durable database completed in 212.754
seconds with a 14,644.2 MiB peak RSS. It recovered 70 missions, 389 sessions,
3,976 services, four hosts, two artifacts, and all 1,788,611 visible events.
Service and mission projections therefore cannot be omitted merely because the
corresponding structured tables are empty. Cold-start cost remains a release
concern and the deployment readiness deadline must accommodate measured replay.

## Subsequent verification

- Initial broad Python run: 7,043 passed, 41 failed, 45 skipped, two xfailed.
  This ran while reconciliation was active; failures are being repaired and
  rechecked against the final source. It is not a green release gate.
- Current UI checks: 667 client/component tests, 13 visual checks and ten fabric
  browser checks passed. Desktop/mobile screenshots cover light/dark themes.
- Real isolated Postgres checks: migrations and replay passed; durable artifact
  uploads, ACLs and bytes survived an HTTP-server restart.
- Real Postgres testing found a heartbeat renaming bug and missing structured
  binding rows for artifact contexts; these are addressed through observed data,
  without fabricated mission/session identities.

The active goal remains incomplete until every acceptance gate is verified.

## Resumed on 2026-10-28

The user resumed work in Codex. The original handoff remains a historical
checkpoint. The paused dirty source was preserved in commit `8ac164f` after
fresh verification: 44 backend tests and all six event-history browser tests.
Current upstream/live release is `3f53537`; integration is merging its Personal
Assistant, GitHub Projects, and shared-patcher changes without replacing the
complete replay or responsive UI work.

The merge exposed an HTTP diagnostics gap: upstream PATCH/DELETE requests reused
the previous request identity on persistent connections. A regression test failed
before applying the shared timing wrapper, then the diagnostics/assistant API
suite passed 33 tests. The existing restriction of these verbs to assistant
resources remains unchanged. Merged backend checks passed 186 tests (42 optional
database cases skipped); subsequent isolated real Postgres checks passed 95 tests
and the assistant persistence round-trip passed separately.

Full final-source Python, browser, release, and deployment checks are pending.
The live service still filters startup event types, so health alone does not prove
recovery of event-only hosts/sessions/services. The complete replay remains a
required integration property; the existing measured cold-start limitation still
applies. Production has not been changed by this resumed work yet.

### Release sweep and final repairs

- All 268 UI gates passed, including unchanged visual baselines, keyboard,
  responsive layout, and performance budgets. Deployment/compose checks passed
  66 tests; canonical-config dry-run and the isolated image smoke passed.
- Broad Python: 7,318 passed, one failed, 58 skipped, two xfailed. The failure
  assumed request order for accounting from independent HTTP handlers, even
  though error responses can arrive before their final accounting call. The test
  now checks the exact rejection multiset and semantic model identities instead;
  the complete proxy suite passed 63 tests. No production proxy behavior changed.
- First broad JavaScript sweep: 1,017 passed and 16 failed. These identified
  CI select synchronization, an unstable offline-CI fixture, new-route loading
  and freshness gaps, new API fixture shapes, and event-history no-op writes.
  All affected suites passed after repairs. One architecture timeout during a
  host CPU-load spike passed unchanged in isolation. A clean broad rerun is
  running; its result must not be assumed.
- The Personal Assistant now uses common calm-aware loading/progress and a
  freshness chip with request diagnostics. Dark-mode fixtures include actual
  Projects/Assistant responses, retaining all original palette assertions.
  Ten dark-mode, nine freshness/loading, and two calm loading checks passed.
- History avoids redundant boolean attribute writes and keeps background polls
  visually quiet. Initial/page navigation still shows slow progress; newly
  appended events retain existing rows and shared enter motion. Eighteen
  history/analytics/motion tests passed. CI selection and adjacent checks passed
  46 tests; Attention comparison/error checks passed two.
- Before rollout, live release `3f53537a6c67` returned 72 missions, 46 sessions,
  264 services, and three hosts. Three-read median latency was 3.03 ms, 3.61 ms,
  24.59 ms, and 2.79 ms respectively. These are baseline measurements, not gains
  attributable to the pending integration. Retained event-only entities still
  need to be verified after complete replay.

### First rollout and safe recovery

The clean full JavaScript rerun passed all 1,033 tests. PR #306 was merged as
`6e7f135`; its tree exactly matches verified release `4e0368c`. The exact image
passed cursor traversal, legacy API, private-stream denial, and asset checks.

The first rollout did not become ready: complete replay exposed 23 duplicated
initial agent names across 46 identities. Startup failed closed with
`persisted agent name is already reserved`. Protected rollback successfully
restored the prior `3f53537` release, all app services, and previous configuration.
Do not describe this first rollout as successful or weaken replay to hide it.

The follow-up preserves all private identity events and normal strict validation.
Opt-in recovery blocks ambiguous names/handles for every claimant; sessions use
their stable IDs until explicitly renamed. Historical ambiguous aliases remain
blocked after renames. Malformed identities and version divergence still fail
validation. Workspace diagnostics explain the fallback, including on mobile.
Seventy-five backend recovery/API checks, 18 workspace browser checks, and the
actual 415-identity private reproduction passed without production writes.

Rollback pins containers by immutable image ID, so follow-up deployment discovery
also resolves that ID's buildstamp against the full receipt commit and actual
image identity. It refuses mismatches and preserves the mounted protected
configuration. Seventy-four deployment/compose checks passed, including repeated
rollback and mismatch tests. The live pinned rollback image resolves correctly.

A full read-only replay of the current production data (2,098,684 retained events
at measurement time) is running before a second rollout. It must prove both
startup and session/service API availability. Production is currently on the
healthy previous release; the follow-up is on `fix/herdr-identity-replay-20261028`.

## History and diagnostics verification

- Event-history reads now support bounded cursor pages without changing the
  legacy unpaginated API contract. Cursor boundaries survive appends and reject
  pruning or changed anchors explicitly; duplicate sequence numbers do not
  omit events. Mission and session histories load independently of detail data.
- Older history remains selected during live updates, with explicit navigation
  back to the newest events. Slow reads expose an indeterminate progress bar;
  errors retain the displayed history, request identity, and retry controls.
- History pagination, durable handoff transitions, and request diagnostics:
  44 tests passed (`/tmp/herdr-history-handoff-metrics-final.log`).
- History browser checks: three passed
  (`/tmp/herdr-history-ui-final.log`); existing mission/session detail integration
  checks also passed five tests.
- The full JavaScript run found nine failures among 983 tests; UI gates found
  four among 268. Repairs and focused rechecks are in progress. Analytics
  rendering was correct: Cache-write occupies column five and Failed column six.
  Tests must resolve semantic headers rather than rely on the old column index.
