# Micro-spec 12: Optional long-lived compiler workers

## Objective
Evaluate native sccache-dist support and Linux worker constraints; reserve Slurm allocations for reusable workers, not one job per crate. Credentials scoped, TLS, worker lease and idle teardown.

## Scope
Limit edits to the minimum existing module(s) found in audit. Do not create a second orchestrator or dashboard. If an external dependency/repository is absent, document blocker and complete self-contained portions safely. For remote or destructive operations, require explicit operator authorization.

## Required workflow
1. Read `AUDIT.md`, architecture, contracts and prior `HANDOFF.md`. Re-check files touched for drift and local user changes.
2. Write a 5–12 line implementation plan with concrete file paths and tests.
3. Write failing focused tests; implement only this unit; run focused and relevant regression tests.
4. Produce evidence (commands, exit codes, code revision, test counts, before/after measurements if relevant).
5. Add `HANDOFF.md` with completed/not-done/known risks, exact files, next micro-spec 13-chaos.

## Exit criteria
N compilations reuse one worker allocation; no orphan jobs; latency benchmark.

## Stop conditions
Insufficient auth, unavailable toolchain/backend, unsupported upstream feature, dirty conflicting file, unverified remote host, failed correctness test, or unresolved safety hazard. Do not work around by deploying alternative infrastructure silently.
