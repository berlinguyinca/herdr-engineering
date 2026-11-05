# Micro-spec 02: Schema and feature flags

## Objective
Extend existing config loader and example. Default disabled. Validate quotas, mode enum, URLs-as-service-refs, allowed hosts/clusters, permissions, no embedded secrets.

## Scope
Limit edits to the minimum existing module(s) found in audit. Do not create a second orchestrator or dashboard. If an external dependency/repository is absent, document blocker and complete self-contained portions safely. For remote or destructive operations, require explicit operator authorization.

## Required workflow
1. Read `AUDIT.md`, architecture, contracts and prior `HANDOFF.md`. Re-check files touched for drift and local user changes.
2. Write a 5–12 line implementation plan with concrete file paths and tests.
3. Write failing focused tests; implement only this unit; run focused and relevant regression tests.
4. Produce evidence (commands, exit codes, code revision, test counts, before/after measurements if relevant).
5. Add `HANDOFF.md` with completed/not-done/known risks, exact files, next micro-spec 03-local.

## Exit criteria
Config tests prove invalid values fail closed and legacy configs still load.

## Stop conditions
Insufficient auth, unavailable toolchain/backend, unsupported upstream feature, dirty conflicting file, unverified remote host, failed correctness test, or unresolved safety hazard. Do not work around by deploying alternative infrastructure silently.
