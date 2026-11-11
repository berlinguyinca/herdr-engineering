# G7 handoff: pi-engineering audit

Branch `docs/wc001-g7-pi-engineering-audit` (worktree herdr-wc001), stacked on G5 / PR #649. Not pushed. Read-only audit: no code, no deploy, no GitHub write, no pi-engineering checkout modified. Runtime artifacts (`pi-eng-*-candidate-*`, `orch-*` worktrees) were not audited.

## Source used

- Canonical checkout: `/home/wohlgemuth/pi-engineering`, origin `ssh://git@github.com/berlinguyinca/pi-engineering.git`.
- Local `main` is c16a501 (2026-10-29) and 167 commits behind. I read `origin/main` ec107d8 (2026-11-04) with `git show` / `git grep` only (no checkout, no fetch). All Pi paths below are relative to that tree at ec107d8.
- The installed-copy path in HANDOFF-pi-engineering-2026-10-06.md was used for reference only.
- Herdr references are to this worktree, HEAD cb3536b8.

## Answers

### 1. Mission / session / task records and lifecycle timestamps
- `Mission` (`src/orchestration/types.ts:107-154`): `created_at`, `updated_at`, `completed_at`, `blocked_at?`, `operator_paused_at?`. There is no `started_at` on a mission. `OrchestrationTask` (`types.ts:216-260`) does have `started_at` / `completed_at`.
- Status set: `MissionStatus` (`types.ts:24-55`), projected to `CanonicalLifecycleState` in `src/orchestration/canonicalLifecycle.ts:37-66`.
- Storage is event-sourced: `MissionStore` over `EventStoreBackend` / JSONL (`src/orchestration/missionStore.ts:103-179` event types).
- Consequence: Herdr's `started_at` for a Pi mission is Herdr's own `recorded.created_at` (`packages/work-summary.js:47`), not a Pi start time. Pi has no honest mission start; `created_at` is the nearest.
- Session records leave Pi through `ControlPlaneIngest.session()` (`src/runtime/herdr/ControlPlaneIngest.ts:509-561`: pane_id, instance_id, worktree_id, commit_sha, repo_url); `missionStarted` is at 496-505.

### 2. Weekly / time-windowed or event-history store
- Pi has an append-only mission event log (above) and a `progressHistory` inside the observability snapshot (`src/orchestration/missionSnapshot.ts:74-135`). Neither is a weekly aggregate or an API Herdr reads.
- The snapshot (`.pi-eng/orchestration-snapshot.json`, contractVersion 3) carries no created/completed times (`missionSnapshot.ts:155-193`), so a time window cannot be built from what Herdr's feed receives.
- No weekly store exists in Pi. Herdr's `packages/work-week.js:85` windows over Herdr's own records.

### 3. Pause / quota / attention states
- Pause: `operator_paused_at` (`types.ts`), statuses WAITING_FOR_* and PAUSED_INFRASTRUCTURE map to canonical PAUSED (`canonicalLifecycle.ts:37-66`). `MissionStop` (`types.ts:705-753`) holds reason, resumeCondition, stoppedAt, queue.
- Quota: Pi has no quota status or failure category (`FailureCategory`, `types.ts:554-578`). "quota" appears only in `src/runtime/host/quotaResume.ts` (a pure resume decision) and gateway files (`src/gateway/backpressureSignal.ts`, `settleRecovery.ts`, `signals.ts`).
- Mismatch to know about: Herdr maps `/QUOTA/.test(entry.status)` to paused_quota (`packages/work-summary.js:18`). Pi never emits such a status, so that branch is dead for Pi missions. Do not claim a Pi mission is quota-paused.
- Attention: `src/orchestration/observability/attention.ts`, `src/orchestration/missionWhy.ts` (cooldown, nextActionAt). Contents of `MissionAttentionReporter` / `attentionIngest` not read (unverified).

### 4. Issue / PR association fields
- Schema exists: `GithubLinkRecord` (`src/github/types.ts:33-52`), recorded via `recordGithubLink` (`missionStore.ts` ~3728). Discovery with explicit-ref rules and `closingRefs` is in `src/github/discovery.ts` (1-11, 109); `discoverGithubLinks` (136) has no production caller; `confirmSuggestion` is at 331.
- The whole layer is off unless `engineering.yaml` has `github.enabled: true` (`src/github/settings.ts:52`). `~/.pi/agent/engineering.yaml` has no `github:` block, so on this host no mission-issue links are recorded.
- PR link: `linkPullRequest` (`src/github/prLinks.ts:29`) has one production caller, `src/orchestration/forks.ts:656-663`, reached from `src/runtime/EngineeringRuntime.ts:2147-2149` (`onPrLinked`). It links PRs created by forks, not arbitrary running missions.
- Ledger side is separate: `WorkItem` / `WorkItemPrLink` (`src/core/types.ts:135-200`), `Ledger.recordPrLink` / `listPrLinks` (`src/ledger/Ledger.ts:500-518`). Not the same record as a mission link.
- The snapshot has no GitHub links (`missionSnapshot.ts:155-193`), and Herdr's activity feed allowlist (`herdr_engineering/mission_activity_feed.py:41-53`) has no such field.
- Lineage envelopes can carry issue/pr/mission nodes and an `issue_link` edge (`src/runtime/herdr/lineage.ts:35-76`, emitted by `src/github/lineage.ts:83-113`), but only when the link layer is on.

### 5. Next action / planned step
- `MissionObservabilitySnapshot.nextAction` and `nextActionAt` (`missionSnapshot.ts:129-130`), derived in `src/orchestration/observability/MissionObservability.ts:1373-1402`. `nextActionAt` is `recovery?.nextActionAt` only.
- Caveat: the derivation has a literal fallback string "No further action is scheduled" (`MissionObservability.ts:1373-1402`). That is an absence marker, not a planned step; the Work Command Center must not display it as a next action.
- `LedgerPlan` (`src/core/types.ts`) is a plan object on the ledger side; whether it reaches Herdr is unverified.

### 6. Progress / evidence signals
- Snapshot observability: `progress`, `acceptanceCoverage`, `waitingReason`, `progressHistory` (`missionSnapshot.ts:74-135`). Herdr's feed allowlist is at `mission_activity_feed.py:41-53`; it checks contractVersion 3 and publishes `mission_activity.snapshot`.
- These are Pi-computed values. Herdr may show them labelled as Pi-reported; it must not recompute them.

### 7. Terminal / pane attach
- Pane identity travels in the session ingest (`ControlPlaneIngest.ts:509-561`). Attach mechanics live in `src/runtime/herdr/HerdrAgentRuntime.ts:436`, `attachInspector.ts`, `CollabLayout.ts`, `runtimeReport.ts`. These were identified, not read in depth. Herdr should reuse the pane_id it already receives and not invent an attach path.

### 8. How Pi state reaches the Herdr control plane
- Path A, push: `ControlPlaneIngest` posts to `POST /api/v1/ingest` (envelope v2, durable outbox, `source_sequence`; best effort, off by default; header `ControlPlaneIngest.ts:1-33`). Herdr dispatches by entity_type in `herdr_engineering/control_plane/ingest.py` (~461-600). Pi emits mission and session lifecycle; it does not emit the `pull_request` entity (`_ingest_pull_request`, ~677-690), so Herdr's PR table is not fed by Pi.
- Path B, read-only bridge: `herdr_engineering/mission_activity_feed.py` reads the Pi snapshot file and publishes `mission_activity.snapshot` into `MissionActivityStore`, which is in memory (not durable).
- Path C, lineage: Pi lineage envelopes (issue/pr/mission nodes) enter via the system-graph adapter (`ingest.py` `GraphIngestAdapter`, opt-in). Whether this populates `repo.list_pull_requests` is unverified.
- Durable Herdr outcome data (`control_plane/mission_outcome.py:100-210`, `pr_refs_by_mission` 249-275) is built from Herdr's own recorded PRs, not Pi's.

### 9. Duplication and double-count risks
- Same mission, two records: a Pi mission can appear via ingest (Herdr `upsert_mission`, `control_plane/repo.py`) and via `mission_activity.snapshot`. Count by mission id, once.
- Two lifecycle vocabularies: Pi canonical states vs Herdr statuses; the quota mapping above is Herdr-only.
- Ledger `WorkItem` vs `Mission` are different entities in Pi; do not sum them. Pi's own derived WorkGraph (`src/work/*`, `legacyMissionAdapter.ts`, `linkedPrs.ts`) is a read model over the same data, not a new source.
- PRs: Pi `linkPullRequest` data and Herdr's own PR records can describe the same PR; key by repository + number.
- Herdr already has its own work graph (`/api/v1/graph/work/*`); do not rebuild Pi's graph on top of it.

## Gap table

| Earlier gap | Honest Pi source Herdr could read? | Evidence | Status |
|---|---|---|---|
| Server weekly history | No. Pi has an event log and per-mission `progressHistory`, but no weekly store, and the snapshot drops created/completed times. | `types.ts:107-154`, `missionSnapshot.ts:74-193` | Not available from Pi as exposed |
| Issue-to-mission links | Partly, in schema only. `GithubLinkRecord` exists but the layer is off by default, discovery has no production caller, this host has no `github:` config, and the snapshot and feed do not carry it. | `github/types.ts:33-52`, `settings.ts:52`, `discovery.ts:136`, `mission_activity_feed.py:41-53` | No data today; would need a Pi config decision plus an exposure path |
| PR links for running missions | Narrow. Only fork-created PRs are linked (`forks.ts:656-663`). Pi does not emit the `pull_request` entity. Lineage `pr` nodes might carry some; unverified. | `forks.ts:656-663`, `ingest.py` ~677-690 | Not reliable for running missions |
| Next action | Yes, with limits. `nextAction` / `nextActionAt` is in the snapshot and its feed field, but may be the literal "No further action is scheduled", and `nextActionAt` exists only with a recovery plan. | `missionSnapshot.ts:129-130`, `MissionObservability.ts:1373-1402` | Usable if the fallback string is treated as "none" |

## Decisions needed from the user

1. Turn on Pi's `github.enabled` (and wire discovery) for the hosts that run missions? Without it issue links stay empty. This is a Pi change and out of scope here.
2. Add `created_at` / `completed_at` (and links) to the Pi snapshot and the Herdr feed allowlist, or leave weekly history as Herdr-only? Needs a Pi snapshot contract bump (currently 3).
3. Should Herdr treat "No further action is scheduled" as no next action (recommended), or show it?
4. Accept that Pi missions never show quota-paused, or have Pi add a quota category? (Pi only has the resume decision in `quotaResume.ts`.)
5. Should the in-memory `MissionActivityStore` become durable if the Work Command Center needs history after a restart?

No data is proposed to be invented. Where Pi lacks a source, the Work Command Center should keep showing the gap.

## Unverified

- Whether lineage ingestion fills `repo.list_pull_requests`.
- Whether Ledger / `LedgerPlan` data reaches Herdr.
- `MissionAttentionReporter` / `attentionIngest` behavior and the EVENT-OWNER-MAP spec.
- Attach mechanics beyond file identification.
- Production Pi configuration and state on hosts other than this workstation.
- Stale worktrees under `~/IdeaProjects` with broken git links were skipped.
