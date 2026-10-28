# Collaboration Data Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved collaboration-room data platform as four independently testable implementation tracks with one shared source of truth across public collaboration and the Herdr dashboard.

**Architecture:** Extend the existing collaboration service and web client rather than creating a parallel collaboration stack. Persist structured state in control-plane Postgres, store live files and immutable room archives through a configurable S3-compatible API, and keep the public edge stateless. Deliver the tracks in dependency order: core authorization, storage/uploads, public/dashboard parity and previews, then agent-generated skills.

**Tech Stack:** Python control-plane services, Postgres migrations, S3-compatible RustFS API, existing JavaScript web client/web components, MCP/A2A adapters, Node test runner, pytest, and Playwright.

**Spec:** `docs/superpowers/specs/2026-10-24-collaboration-rooms-data-platform-design.md`

## Global Constraints

- Use the existing control-plane Postgres for structured collaboration state and an S3-compatible API for file bytes and archives.
- Keep the public edge stateless and without database/object-store credentials.
- Transfer large files directly between the registered harness and S3; Herdr must not proxy terabytes of data.
- Treat room deletion as archive-then-delete and do not delete live data before archive verification.
- Preserve only minimal audit metadata for deleted users; remove user-visible identity, authored messages, credentials, and access.
- Every public collaboration capability must also be available in the Herdr dashboard against the same source of truth.
- All mutating operations must be idempotent and auditable.
- No bucket per room; use separate live/archive buckets and opaque room prefixes.

## Track order and handoffs

1. `2026-10-24-collaboration-core-permissions-plan.md` — state model, deletion, room/agent/file permissions, public waiting state.
2. `2026-10-24-collaboration-storage-uploads-plan.md` — Postgres/S3 storage, archive/restore, direct MCP uploads.
3. `2026-10-24-collaboration-surfaces-previews-plan.md` — public/dashboard parity, file browser, previews, Flasheic adapter.
4. `2026-10-24-collaboration-agent-skills-plan.md` — chat-driven analysis, skill registry, publishing, local agent installs.

Each track must pass its own targeted tests before the next track starts. Existing unrelated worktree changes must remain untouched.

## Cross-track verification

- Run `pytest -q tests/control_plane/test_collab*.py` after core changes.
- Run `npm test` after web-client or component changes.
- Run the focused Playwright suite for public/dashboard parity after surface changes.
- Run `git diff --check` and inspect `git status --short` before each handoff.

## Completion gate

- All four track plans are implemented or explicitly reported with a validation gap.
- Public and dashboard capabilities use identical room, message, file, upload, preview, skill, and authorization records.
- Archive deletion and user deletion have end-to-end tests proving the required retention boundaries.
- Bulk upload tests prove direct S3 transfer, resume, checksum verification, and hidden-until-finalized behavior.
- Generated skills are versioned, permission-scoped, verifiable, and installable by registered agents.
