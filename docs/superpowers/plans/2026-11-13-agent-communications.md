# Agent Communications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make native CLI and dashboard agent communications visible with honest, durable audit evidence.
**Architecture:** A transparent native CLI integration records redacted observations into a bounded SQLite outbox. Authenticated ingestion stores those observations without granting command authority; a shared read projection combines them with server receipts.
**Tech Stack:** Existing Python 3.12 integration and HTTP server, SQLite, existing dashboard modules, Playwright CLI.
**Spec:** docs/superpowers/specs/2026-11-13-agent-communications-design.md

## Global Constraints
- Preserve external HerdR binary and native behavior; no automatic message redelivery.
- Redact before persistence; 16 KiB messages; local 10,000 records/64 MiB/14 days.
- Preserve authentication, event retention, source/uncommitted work and other agents' checkouts.
- Implement inline (operator approved implementation; no further workflow-choice gate).

## Review Focus
- Native routed arguments and leading-hyphen text remain literal and route to the same target.
- Process crash/timeout and unrecognized receipts never become confirmed delivery.
- Concurrent writers, full disks and offline replay do not grow unbounded or duplicate messages.
- Direct observations never impersonate authoritative dashboard receipts or alter agent state.
- Message text cannot execute markup or leak credentials; missing session identities remain explicit.

### Task 1: Capture and bounded audit outbox
**Files:** new herdr_engineering/communications.py; tests/test_communications.py; CLI and install entry points.
**Interfaces:** typed audit record, SQLiteOutbox.enqueue/settle/pending/acknowledge, parse_prompt and native_main.
- [ ] Write and run failing pure-unit and real SQLite tests for parsing, redaction, bounds and delivery acknowledgement.
- [ ] Implement transparent native command capture and offline outbox with concurrent transaction safety.
- [ ] Verify native CLI invalid-target behavior, stdout/exit preservation and pass-through smoke.
- [ ] Commit code/tests.

### Task 2: Event ingestion and communication projection
**Files:** control_plane/communications.py; ingest.py; repo.py; api.py; herdr_adapter.py; tests/control_plane/test_communications.py.
**Interfaces:** validate_observation; list_communications; read-only GET /api/v1/communications; existing POST /api/v1/ingest.
- [ ] Write failing real ingest/HTTP tests: auth, input validation, duplicates, identity mapping, server precedence and no lifecycle mutation.
- [ ] Add strict observation validation and idempotent storage using existing event persistence; correlate dashboard dispatch operation IDs through child environment only.
- [ ] Add bounded retrieval with sender/recipient session links and unknown identity labels.
- [ ] Run affected regressions and commit.

### Task 3: Dashboard, packaging and release validation
**Files:** packages/app.js and existing web client; communications view; docs/operations/agent-communications.md; scoped GitHub Actions workflow.
- [ ] Capture current UI before edits when runnable; add real browser tests that fail for missing communications.
- [ ] Add communication history using existing dashboard conventions, truthful labels and escaped text.
- [ ] Add opt-in reversible shim installation and sync; document limitations, audit gaps and retention.
- [ ] Run lint, type checking, four test layers, 80%+ coverage and build.
- [ ] Commit, open PR, obtain independent review, fix important findings, watch CI to completion and merge when required checks pass.
- [ ] Install locally when safe; verify real audit visibility without duplicate delivery; report any remote deployment limits.
