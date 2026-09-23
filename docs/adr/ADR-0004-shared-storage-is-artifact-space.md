# ADR-0004 — Shared storage is artifact/handoff space, not source truth

**Status:** Accepted — 2026-09-23

## Decision

Provide a shared filesystem/workspace for generated artifacts, handoffs, screenshots, reports, test exports and similar evidence, but keep active source code in isolated local Git worktrees.

## Rationale

Multi-writer source trees on a network share create locking, latency, accidental overwrite and platform-compatibility risks. Git worktrees already provide correct isolation and provenance.

## Consequences

- Shared storage outage must not corrupt or block local source work.
- Files are copied/materialized with provenance and integrity metadata.
- Credential stores, `.git` internals and secrets are excluded by default.
