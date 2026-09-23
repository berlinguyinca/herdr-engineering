# 0080 — Shared Artifact and Handoff Workspace

## Goal

Give agents and humans a convenient common place for outputs while preserving local Git worktree isolation.

## Scope

Support generated artifacts such as:

- screenshots/video captures;
- plans/spec extracts;
- reports/plots;
- test/coverage exports;
- browser captures;
- review handoffs;
- packaged logs;
- candidate comparison bundles.

## Architecture

Implement an abstract artifact workspace provider first. The initial concrete provider may be a shared filesystem available to the fleet, but the product contract must not depend on one mount technology.

Recommended logical hierarchy:

```text
<root>/<project>/<mission-or-session>/<artifact-id>/...
```

Use metadata/provenance, content hashes and `ArtifactRef`; do not make human-readable path layout the only index.

## Safety

- Never sync `.git`, SSH keys, cloud credentials, auth tokens, browser profiles or arbitrary home directories by default.
- Use atomic publish/rename patterns for completed files.
- Handle concurrent writers without last-writer corruption.
- Local work continues if shared storage is down; publishing becomes queued/degraded with visible status.
- Retention/quota policy is configurable.

## Exit gate

Linux/macOS hosts can publish/read artifacts concurrently, provenance resolves back to owner session/worktree, simulated storage loss cannot corrupt active repositories, and reconnection safely resumes or reports failed publication.
