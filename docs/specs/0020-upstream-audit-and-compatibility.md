# 0020 — Upstream Audit and Compatibility

## Goal

Replace assumptions with a tested compatibility matrix against the actual installed/current Herdr ecosystem before dependency lock-in.

## Required discovery

Capture and retain machine-readable output where supported for:

```text
herdr --version
herdr api schema --json
herdr machine list --json
herdr plugin list --json
herdr integration status
```

Inspect official Herdr documentation/source for machine connectivity, socket/plugin API, worktrees, events/config and Pi integration.

Audit candidate dependencies from `docs/upstream-baseline-2026-09-23.md`, including newer alternatives discovered during implementation.

## Classification

For every candidate record:

- canonical repository + immutable tested revision;
- license;
- maintenance/release state;
- Herdr version/API compatibility;
- Linux/macOS support;
- install/build scripts and supply-chain risk;
- listeners/network exposure;
- credential needs;
- whether native Herdr now supersedes it;
- decision: `ADOPT`, `ADAPT`, `OPTIONAL`, `HOLD`, `REJECT`.

## Mandatory architecture validation

Explicitly verify that native Herdr saved machines and machine-targeted operations are sufficient for the base fleet transport. A custom replacement requires a new ADR proving the gap.

## Deliverables

- `HERDR_COMPATIBILITY.md`
- populated `lock/upstreams.yaml`
- compatibility tests for adopted plugins
- ADR updates for changed assumptions

## Exit gate

No dependency enters the Powerpack without a tested revision and classification, and the implementation uses native Herdr primitives wherever they satisfy the requirement.
