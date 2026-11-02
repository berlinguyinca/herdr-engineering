# Phased implementation with stop/go gates

## P00 — Exact current audit; no source mutations
Inspect working trees, unmerged PRs, lock revisions, entire memory code paths in all five repos, real `herdr-memory` package ownership, operational Hindsight API, adapters and active sessions. Write evidence matrix with REUSE / EXTEND / CONSOLIDATE / NEW / REMOVE and per-feature tests. Confirm API semantics against running/versioned server. If critical unknown, mark blocked and defer affected feature, continue independent work.
Exit: source SHAs, deployment evidence, ownership, contract compatibility, migration strategy, security boundaries documented. Do not infer Hindsight availability from dated smoke report.

## P01 — Canonical memory client & config
Prefer extending herdr-memory (once found), with versioned config schema, typed retain/recall/get/forget, bank resolution, stable identity, retries and outbox. Add CLI memory plan/status/doctor. Tests for auth, scoped cross-bank queries, idempotency, corrections, queue overflow and unavailable server. No new storage engine.
Exit: unit/contract tests and read-only live smoke on canary.

## P02 — Harness integration and installer
Extend existing `scripts/install.sh`, existing lock/pinned install flow, `herdr-handoff`-like idempotent overlay and per-harness native integrations: Pi (reuse `interactiveMemory.ts` provider seam), Claude, Codex, OpenCode, capability-detected. Expose `herdr-eng memory plan/install --all/status/doctor/repair/uninstall-managed-only`. Do not promise identical hooks across harness versions. Maintain backwards-compatible `/memory` and `/remember` where practical. Automatic installation via declarative desired state, never mutate active sessions.
Exit: four locally supported harness smoke tests in isolated HOME fixtures, merge-preserving config tests, healthy/degraded statuses and one cross-harness canary.

## P03 — Lifecycle & handoffs
Add recall-before-task and project-plan lookup via existing lifecycle; candidate capture from permitted explicit evidence; evidence-gated promotion; handoff package optional memory references, versioned schema and durable ack. Hook into existing event metadata and mission IDs. Retain Blackhole short-term contexts; do not publish hidden reasoning.
Exit: Host A Pi retain => Host B Claude recall => Codex/OpenCode recall and correction; auth isolation and offline handoff tested.

## P04 — Dashboard MVP
One Memory Explorer route and detail drawer on existing Herdr control-plane authenticated API. Integrations status per host/harness; provenance links to existing session/handoff/PR views. Pagination, ACL, real empty/offline/error states, no placeholder data.
Exit: browser tests desktop/mobile; end-to-end query with real saved record.

## P05 — Quality/observability
Stale-memory detection from verified commit/reference changes; duplicate/conflict proposal queue, audit/correction UI; memory effectiveness benchmark with baseline and A/B repeated tasks; dashboards for measured retrieval success/latency/reuse, not invented confidence metrics. Graph/replay only where evidence supports it.
Exit: no regression on security, availability, latency, or task quality.

## P06 — Safe rollouts
Single-host canary with installed versions pinned and rollback of managed overlay. Only extend herdr-fleet when activity-aware staged rollouts and rollback verifiably exist. Explicit operator approval for production changes.
Exit: repair/uninstall preserves user configs and live sessions; no cross-tenant exposure.

## Delivery discipline
Independent PR per phase; tests + docs + screenshot evidence; do not combine repo-wide refactors. Record commits, commands and test results. Update `docs/specs/herdr-unified-hindsight-memory-20261102/STATUS.md` after each phase. Stop automatically on auth boundary failure or data-loss risk. Continue independent phases where safe.
