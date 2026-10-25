# PI Harness Latency Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Herdr responsive during PI harness workloads by eliminating orphan helper CPU leaks, coalescing PI snapshot publication, and deduplicating Herdr snapshot reads.

**Architecture:** PI remains the owner of durable mission state and publishes a bounded latest-state snapshot for consumers. Herdr will cache parsed snapshots per unique working directory and reuse them across descriptors and polling cycles. Harness process containment will be implemented at the shared helper lifecycle boundary so cleanup and CPU limits apply to every helper.

**Tech Stack:** TypeScript/Node.js, Python 3, pytest, node:test, POSIX process groups/cgroups where available.

**Spec:** The approved remediation design from the investigation: P0 helper cleanup/containment; P1 PI snapshot rate limiting; P1 Herdr unique-cwd snapshot caching; P2 measurable performance budgets.

## Global Constraints

- Preserve the existing PI snapshot schema and Herdr mission-activity wire contract.
- Do not add runtime dependencies.
- Never let telemetry or cleanup failures break the parent PI session.
- Keep current repository/worktree isolation and preserve unrelated dirty work.
- Use bounded queues, timers, and cache sizes; no unbounded retry or memory growth.

### Task 1: Herdr snapshot cache and unique-cwd collection

**Files:**
- Modify: `herdr_engineering/mission_activity_feed.py`
- Test: `tests/test_mission_activity_feed.py`

**Interfaces:**
- Add a bounded snapshot cache keyed by absolute snapshot path with `(mtime_ns, size)` validation.
- `MissionActivityFeed._collect` receives an optional parsed snapshot so a polling cycle reads each unique cwd once.
- Existing payload and publication behavior remain unchanged.

- [x] Write failing tests proving one snapshot read per unique cwd in a cycle and cache invalidation after file metadata changes.
- [x] Run the focused tests and verify they fail for the missing cache behavior.
- [x] Implement cycle-local grouping plus bounded cross-cycle cache reuse.
- [x] Run focused tests and verify they pass.
- [x] Run the complete mission-feed test module.

### Task 2: PI snapshot publication rate limiter

**Files:**
- Modify: `/tmp/pi-harness-latency-fix/src/runtime/EngineeringRuntime.ts`
- Test: `/tmp/pi-harness-latency-fix/test/unit/orchestration-snapshot.test.ts`

**Interfaces:**
- Add an injectable snapshot publication clock/timer boundary or equivalent deterministic scheduling seam.
- Multiple dirty notifications within the configured window produce one latest-state publication.
- Explicit shutdown/forced publication remains immediate.

- [x] Write failing tests proving rapid observability updates coalesce and the latest state is published after the interval.
- [x] Run the focused tests and verify the new assertions fail.
- [x] Implement a default 750 ms publication window without changing the snapshot schema.
- [x] Run focused snapshot tests and verify they pass.
- [x] Run the broader PI runtime tests covering snapshot publication.

### Task 3: PI helper lifecycle cleanup and CPU containment

**Files:**
- Inspect and modify the existing helper/process launch boundary in `/tmp/pi-harness-latency-fix/src/` or `/tmp/pi-harness-latency-fix/test/support/` only after locating the shared implementation.
- Add regression coverage beside the owning helper lifecycle tests.

**Interfaces:**
- Helpers are placed in a process group or equivalent ownership scope.
- Parent shutdown, timeout, and child-exit paths terminate the group and reap descendants.
- CPU containment is best-effort and capability-gated; unsupported hosts retain cleanup behavior without failing launch.

- [x] Locate the common helper launch path and existing cleanup tests.
- [x] Confirm existing PI verification helpers already use detached process groups, abort cleanup, and leftover reaping.
- [x] Implement bounded CPU containment and transient-scope collection in the existing Herdr collaboration sandbox.
- [x] Run focused sandbox tests and verify they pass on the current host.
- [x] Run the relevant sandbox lifecycle checks.

### Task 4: Performance instrumentation and verification

**Files:**
- Modify: the PI snapshot publisher and Herdr mission feed only where existing observability hooks can record bounded counters/timings.
- Test: corresponding PI and Herdr unit tests.
- Docs: `docs/operations/` performance/runbook note if an existing operations document is the correct home.

**Interfaces:**
- Record snapshot publication count/duration/bytes and feed cycle duration/read count without logging prompts or secrets.
- Keep metrics bounded and optional.

- [x] Add tests for bounded snapshot read counters and preserve existing redaction tests.
- [x] Implement compact per-cycle snapshot read/cache-hit/byte counters.
- [x] Run unit, typecheck, lint, shell syntax, and targeted runtime checks in both worktrees.
- [x] Run controlled tests for repeated snapshot reads and publication coalescing.
- [x] Review diffs; full PI repository lint remains blocked by 18 pre-existing unrelated findings.
