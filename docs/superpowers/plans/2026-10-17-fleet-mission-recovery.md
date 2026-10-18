# Fleet Mission Recovery Implementation Plan

> **For agentic workers:** Use test-first implementation with independent task reviews. This plan executes the design approved in the conversation on 2026-10-17.

**Goal:** Restore mission progress after recoverable failures and provide verifiable fleet-wide runtime update and existing-mission recovery.

**Architecture:** Pi owns transactional runtime handover, checkpoints, ownership fences, loaded build attestation and mission recovery. HerdR owns immutable deployment, exact live-session discovery, durable rollout operations and delivery reconciliation. Existing local Unix control sockets remain the workstation boundary; SSH transports structured requests without model prompt injection.

**Tech Stack:** Existing TypeScript/Node Pi runtime and Python HerdR Engineering; no new dependencies.

**Spec:** The accepted investigation and rollout design in this session; Pi architecture remains governed by docs/specs/pi-engineering-runtime-ledger-tournament-spec-v2.md.

## Global Constraints

- No changes in shared main checkouts; each implementation lane uses its own worktree.
- Never lose uncommitted work, bypass fencing, create replacement missions, or clear operator pauses during automatic maintenance.
- Full immutable release commit IDs are pinned for one rollout. Offline hosts remain pending.
- A message receipt is not successful activation. Verify actual loaded commit and subsequent mission dispatch/progress or a named future wait.
- Commands are targeted to exact session instance and expected generation. Replays are idempotent; mismatched payload reuse is refused.
- Legacy sessions without capabilities are explicitly reported for controlled bootstrap, never silently killed or declared upgraded.
- No live rollout or agent messages until code is tested and a concrete release is reviewable.

## Task 1: Mission recovery correctness

Files: src/orchestration/scheduler.ts, orchestrator.ts, supervisor.ts, missionStore.ts, src/runtime/EngineeringRuntime.ts; matching unit/integration tests.
- [ ] Add real-dispatch authority-loss regression that currently yields CANCELED; prove red.
- [ ] Use resumable cancellation for authority failures; old execution remains fenced and unfinished tasks recover before downstream phases.
- [ ] Add preflight blocker regression; preserve unresolved workspace/planner/provider causes before generic diagnosis.
- [ ] Add production-runtime parked-wait wake regression without a test-supplied watcher; implement bounded probe/recovery, preserving explicit operator pauses.
- [ ] Test idempotent recovery settlement and missing-candidate routing; fix evidenced failures.
- [ ] Run targeted tests, typecheck, formatting, review diff and commit.

## Task 2: Truthful, bounded runtime activation

Files: src/runtime/host/{extension,sourceWatch,host,operations}.ts, tests.
- [ ] Reproduce watcher accepting a failed/deferred reload; advance accepted SHA only after successful target activation.
- [ ] Bound default safe-point waits; per-session reload waits must not monopolize fleet-wide install locks.
- [ ] Publish a typed process-local Host control provider for describe/reload, preserving separate bootstrap and runtime identities.
- [ ] Test exact target mismatch, rollback, timeout, retry same SHA and sibling isolation; commit.

## Task 3: Structured Pi maintenance control

Files: src/sessionControl/*, extensions/index.ts, new maintenance state helper/tests.
- [ ] Add typed describe, reload and maintenance-resume operations with exact instance targeting and durable operation receipts.
- [ ] Expose actual runtime commit/generation/capabilities and owned missions; legacy absence remains explicit.
- [ ] Resume existing stopped missions only after loaded-commit verification, expected revision/resumption checks and latest operator intent checks.
- [ ] CLI accepts JSON request and returns accepted/running/succeeded/failed/skipped receipts; repeated operation IDs do not repeat mutation.
- [ ] Test same-user socket targeting, replay, busy session, cancellation race and loaded-commit mismatch.

## Task 4: Pinned fleet deployment and honest parity

Files: herdr_engineering/fleet.py, ansible fleet tasks/playbooks, tests.
- [ ] Reproduce false success for missing inventory hosts/components; require complete expected-host coverage and valid fingerprints.
- [ ] Pin controller and host deployment to one exact revision; stage Pi runtime at a selected exact revision, install package dependencies and register the managed package without overwriting unrelated settings.
- [ ] Report installed Pi provenance separately from loaded sessions, fail missing/unsupported hosts explicitly.
- [ ] Test upstream movement, missing hosts, dirty checkout preservation, offline and install failure; commit.

## Task 5: Native session identity and real delivery

Files: herdr_engineering/herdr_adapter.py, control_plane/{api,repo,worker}.py, deploy/herdr-cp-feed-plugin/emit.js, tests.
- [ ] Preserve native conversation references, terminal identity, revisions and machine/server routing.
- [ ] Fix native prompt argv against installed CLI/schema; never infer target from arbitrary identifier prefixes.
- [ ] Make control-plane message/action outcomes describe actual queued/delivered/failed execution, not fabricated active status.
- [ ] Namespace cross-host session identity and cover collisions; compatibility for existing records is explicit.
- [ ] Test exact argv, unavailable transport, no false receipt, replay and identity collision; commit.

## Task 6: Durable fleet rollout reconciliation

Files: new herdr_engineering fleet rollout/state/client modules, CLI, tests and operator documentation.
- [ ] Persist selected release and expected hosts plus per-session target identity, operation IDs, attempts and evidence atomically under private local state.
- [ ] Reconcile discovery, staging, activation, loaded commit, eligible mission resume and useful progress with bounded per-host work.
- [ ] Use Pi JSON control operations through local/SSH CLI; missing capabilities become bootstrap_required and unreachable hosts stay pending.
- [ ] Resume controller after interruption without duplicate missions/resumes; preserve original selected release.
- [ ] Test two-host/multi-session scenario, one offline host, duplicate replies, stale instance, operator pause and old loaded code.
- [ ] Document staged rollout, status, retry and bootstrap limitations; verify both repositories and open PRs with evidence.
