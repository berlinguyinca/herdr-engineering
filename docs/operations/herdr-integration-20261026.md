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
- Full Python suite is running; completion is not yet established.
- Integration has not been deployed.

The active goal remains incomplete until every acceptance gate is verified.
