# Pi handoff: HerdR dashboard recovery

## Stop state and how to resume

The user explicitly requested stopping Codex and transferring this work to Pi.
Codex goal is **paused**, all six child agents were closed, and this session's
remaining deployment-test and client-test process trees were stopped. No active
OMX mode existed in this session. Other sessions' processes were left alone.
No release was deployed during this final work interval. Work is incomplete.

Start Pi in this exact integration checkout:

```bash
cd /home/wohlgemuth/IdeaProjects/.worktrees/herdr-complete-20261026
pi "Read docs/operations/HANDOFF-PI-20261026.md and continue the remaining work in order. Preserve the current uncommitted work and all existing functionality and UX. Verify the final source before committing and redeploying."
```

Pi instructions: read this document and local AGENTS.md first. Inspect the actual
working tree before editing; do not recreate the implementation from this prose.
The earlier user authorization covers autonomous fixes, verification, integration,
and redeployment as needed. The latest stop applies to Codex; the command above
is the user's explicit way to start the successor. Do not launch another Codex
loop. Do not ask the user to repeat the context or approve ordinary reversible
steps. Never claim complete or green based on the partial suite results below.

The standalone `herdr-handoff` CLI is absent from PATH. This is a local private
handoff, not a server-queued/claimed handoff. A local JSON package, source patch,
untracked-source archive, status, and available evidence are saved under:
`/home/wohlgemuth/.local/state/herdr-engineering/handoffs/20261026-dashboard/`.
No target Pi process was launched. Do not claim a remote transfer occurred.

## Goal and required outcome

Recover all the dashboard design and runtime work, fix slowness/timeouts with
actionable causes and request IDs, provide standard responsive loading/progress
behavior, and redeploy the consolidated release without losing functionality,
UX, permissions, or durable history. The user asked to do the work in order.

## Source of truth

- Working directory: `/home/wohlgemuth/IdeaProjects/.worktrees/herdr-complete-20261026`.
- Branch: `integration/herdr-complete-20261026`.
- HEAD at stop: `c75f10bc7717baf8dfbe2fbbae82a476ae08ad16`.
- **Important:** final UI/history/diagnostic fixes remain uncommitted. Deploying
  HEAD now would omit them. Use `git status` and `git diff`, including untracked
  source/tests, then commit the verified final candidate before deployment.
- Original `/home/wohlgemuth/IdeaProjects/herdr-engineering` is a different,
  dirty, old checkout with user work. Preserve it. Never reset it or deploy it.
- Other sessions actively work in sibling checkouts and sometimes deploy the
  same services. Never kill their tests or overwrite their changes. Recheck
  deployment concurrency immediately before rollout; do not chase every new
  upstream commit indefinitely or resolve conflicts wholesale.
- Recovery checkpoints: `8bc7c7e` preserves an early faulty integration;
  `eb91435` reconciles current upstream additively; `a415f4c`, `5f6065c`,
  `62d4282`, and `c75f10b` retain later upstream, security, and deployment work.

## Confirmed root causes and decisions

1. The original checkout lagged upstream by hundreds of commits. Deploying it
   replaced newer UI work. Early wholesale conflict resolution also deleted
   newer routes/API behavior. Reconciliation must remain additive and reviewed.
2. Production entity tables are not a complete projection. The event log is
   required to reconstruct missions, sessions, services, hosts, analytics, graph,
   and other product state. **Do not restore upstream replay stream filters.**
   They made startup faster by discarding visible state.
3. A read-only production replay recovered all 1,788,611 events, 70 missions,
   389 sessions, 3,976 services, four hosts, and two artifacts in 212.754 seconds,
   with peak RSS 14,644.2 MiB. Cold start is still expensive. Preserve data and
   budget readiness accordingly; do not present this as a solved cold start.
4. Credential drift was an earlier incorrect hypothesis. Canonical database
   authentication was tested successfully. Never print credentials.
5. Analytics rendering was correct: upstream inserted Cache-write at cell five;
   Failed is cell six. Source unchanged; tests now resolve semantic headers.

## Completed implementation

Committed integration includes restored upstream routes and rich UI, artifact
registry/ACL/publication/job/review integration, scheduler and cross-harness
handoffs, probe caching, feed batching, identity preservation, bounded request
workers/diagnostics, complete replay with batched read-only cursor hydration,
legacy migration aliases, and protected immutable rollback snapshots.

Current uncommitted work includes:

- `repo.py` and API events branch: opt-in cursor pagination, limits 1–200,
  default 50; legacy unpaginated response unchanged. Entity-bound opaque cursors
  retain duplicate/gapped sequences and stable appends; changed/pruned anchors
  return 409 `staleHistoryCursor`. Private stream denials remain intact.
- `handoffs.py`: restores full typed streams rather than only recent 100k
  events; persists before acknowledgment and serializes concurrent changes.
- `request_metrics.py`/API: bounded recent slow/error diagnostics with sanitized
  route, request ID, status, elapsed time, and cause class. A merge-introduced
  obsolete download ACL check using the wrong principal type was removed;
  the enclosing correct resource ACL guard remains.
- New `packages/event-history.js`, integrated in mission/session detail pages:
  independent background loading, bounded scroll region, Older/Newer/Latest,
  retry, timeout, cancellation, request identity, and slow indeterminate progress.
  History enrichment never blocks detail rendering.
- Review found three history UX issues and fixes were applied: Newer restores
  a visited snapshot and pins it until Latest; Retry repeats failed navigation;
  delayed completion preserves focus moved into the event list. Reviewer added
  three regressions and reported **all six history browser tests passed**.
- Overview now limits concurrent resource reads to four, uses single-flight
  reads, cancellation on hidden/dispose, deadlines, jittered exponential backoff,
  and Retry-After. Failures retain previous data and actionable diagnostics.
- Accessible delayed native indeterminate progress, calm/reduced motion,
  Attention cancellation generations, CI inspector width, on-scale toolbar
  spacing, immediate workbench control release after writes, and deterministic
  chat readiness. No ratchets or baseline PNGs were loosened.
- Analytics regression tests check all seven headers, independent Cache-write
  and Failed flashes, exact single-cell changes, first-paint/calm suppression,
  and unchanged refreshes with no DOM writes.

See `git status` for the complete inventory; do not omit new untracked modules
or tests. Generated mission screenshot was restored and is not a release change.

## Verification: distinguish evidence from gaps

Verified focused results:

- Backend reconciliation 532 tests; backend history/handoff combined 117 tests.
- History/handoff/diagnostics focused command: 44 passed.
- Artifact family 103; API 63; real isolated Postgres 10 passed.
- Most recent real Postgres plus artifact-access command: 15 passed.
- Legacy migration receipt upgrade retained artifact data and ACLs.
- Analytics: four focused browser tests passed; source unchanged.
- History: three main tests passed, then reviewer reported all six after the
  final three fixes; existing mission/session integration five passed.
- UI polling/progress 44 checks, core UI 29, workbench eight, state audits three,
  four isolated room audits, and **88 unchanged visual baselines** passed.
- Isolated CI performance budget passed: run selection p95 465.7 ms.
- Scoped Ruff, syntax/compilation, and diff checks passed. Whole-repo Ruff has
  existing findings; do not describe it as clean.
- Earlier deployment-script 57 and deployment/compose 124 checks passed against
  the pinned `a415f4c` application. Final-candidate checks still needed.

Broad runs are NOT green:

- Second broad Python: 7,135 passed, two failed, 65 skipped, two xfailed.
  Both failures were the same obsolete download ACL guard, then repaired and
  covered by the passing targeted artifact/API/Postgres results above.
- Broad JS: 974 of 983 passed, nine failures. Each assigned regression was
  repaired and focused checks passed. Later broad runs were stopped after
  intermittent room startup stalls and a CI timing miss under host contention.
- Initial UI gates: 264 of 268 passed. Actual spacing and readiness issues were
  repaired; isolated audits and unchanged visual baselines passed afterward.
- Final client/component and final deployment/compose commands were **stopped
  at user request**, so their partial output is not a pass. Their logs are
  `/tmp/herdr-final-client-components.log` and `/tmp/herdr-deploy-final-source.log`.
- No clean broad final-source run, image candidate smoke, or final rollout has
  completed. Do not weaken assertions or increase visual/performance ratchets.

Useful logs/reports (available files copied into private evidence directory):

- `/tmp/herdr-final-python-release.log`
- `/tmp/herdr-final-javascript-release.log`
- `/tmp/herdr-final-ui-gates-release.log`
- `/tmp/herdr-history-handoff-metrics-final.log`
- `/tmp/herdr-history-artifacts-release-final.log`
- `/tmp/herdr-analytics-focused-final.log`
- `/tmp/herdr-history-ui-final.log` and `/tmp/herdr-history-review-fixes.log`
- `/tmp/herdr-ui-release-followup.txt`
- `/tmp/herdr-ci-budget-isolated.log`
- `/tmp/herdr-production-replay-benchmark.log`
- `/tmp/herdr-ui-slow-progress/manifest.json` and captures
- `/tmp/herdr-ui-screenshots/manifest.json` and captures

## Remaining work, in order

1. Inspect current diff and all six final history browser tests. Confirm final
   snapshot/retry/focus changes and source integration are present. Recheck
   pending coverage with controlled concurrency; focus first on unresolved
   failures, then obtain an honest final release result. Existing test commands:
   `python3 -m pytest -q`, `npm test`, `npm run test:ui-gates`.
   Use `node --test --test-concurrency=1` for client-only runs if necessary to
   avoid launching all test files simultaneously. Do not start redundant broad
   runs while external sessions already saturate the host.
2. Review final functionality/security and rendered desktop/mobile states.
   Preserve all routes, themes/density/calm, CI/debug controls, keyboard focus,
   artifact jobs/ACLs, and full durable history. Confirm genuine timeout causes
   and request IDs remain visible, not just generic errors.
3. Record accurate evidence in `docs/operations/herdr-integration-20261026.md`.
   Commit all reviewed source/tests into the integration branch, then pin the
   exact final revision. Preserve original user checkout and recovery commits.
   Integrate/publish through the appropriate normal Git workflow; no force push.
4. Run deployment/compose checks against that committed candidate, build and
   smoke-test its image, and check no competing control-plane deploy is active.
5. Deploy only the final verified commit using real HOME and canonical config,
   durable receipts, immutable rollback images, and readiness longer than the
   measured full replay. Do not deploy `c75f10b` without the current dirty fixes.
6. Verify live exact commit/config mounts, healthy web/worker/proxy/collab/edge,
   recovered entity counts and history, normal UI routes, no browser errors,
   and measured warm response times. State the remaining cold-start limitation.
7. Clean only owned disposable test resources after they are no longer needed;
   keep unpushed source and user work. Mark the goal complete only after genuine
   completion; Codex's goal is currently paused at the user's request.

## Deployment state and command template

Last read-only audit: web/worker/proxy use `bce35fb967bf`, collaboration uses
`d58294d5e399`. Web health OK, zero restarts observed. Recheck these facts.
Canonical configuration directory is mounted read-only on web/worker:
`/home/wohlgemuth/.config/herdr-engineering/fleet`.

Use canonical file:
`/home/wohlgemuth/.config/herdr-engineering/fleet/fleet-config.yaml`.
It must be alone in its directory. Never repurpose HOME/CODEX_HOME or use
temporary rollback fleet directories. Do not print or copy its credentials.
Real durable deployment state:
`/home/wohlgemuth/.local/state/herdr-engineering/deploys/`.
Legacy receipt `bce35fb967bf.json` lacks protected previous snapshots; the new
script creates them before changing services and pins each actual image ID.

After verification, replace FINAL_COMMIT with the new committed full revision:

```bash
test "$HOME" = /home/wohlgemuth &&
HERDR_CP_HEALTH_TRIES=180 HERDR_CP_HEALTH_INTERVAL=2 \
flock -n /home/wohlgemuth/.local/state/herdr-engineering/deploys/rollout.lock \
bash deploy/control-plane/deploy.sh \
  --ref FINAL_COMMIT --no-fetch \
  --fleet-config /home/wohlgemuth/.config/herdr-engineering/fleet/fleet-config.yaml
```

Do not wrap rollout in an external timeout: termination traps do not initiate
rollback. The script owns its readiness and rollback flow. Recheck concurrent
deployment processes even though this lock protects cooperating sessions only.

Old/new artifact migration names must coexist with aliases: existing production
receipts cover `0003`–`0007`; upstream renamed additions to `0008`–`0013`.
The alias upgrade avoids duplicate CREATEs and validates matching legacy SQL.
The last audit saw both receipt names already applied; do not remove definitions.

## Owned disposable resources

Isolated test DB container `herdr-integration-pg-20261026-1218` (Postgres 16,
tmpfs, loopback port 32770, database `herdr_e2e`) was left available for Pi.
It contains test data only. Obtain test connection settings locally without
printing secrets. When finished, stop this exact container; it auto-removes.
Never stop production Postgres/RustFS or other sessions' containers.

Production replay benchmark is complete; do not repeat its 14 GiB replay merely
for reassurance. Check candidate/live recovery as part of the actual release.
No new agent/team remains assigned. Pi owns further orchestration and validation.
