# Session Workspace Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement each owned slice with tests and review.

**Goal:** Deliver inspectable session conversations and a shared composer with separate activity and linked helpers.

**Architecture:** Extend existing inspector and feed contracts; share composer/message components between Collaboration and session views. Preserve authenticated transport semantics and explicit provenance.

**Tech Stack:** Python control plane, JavaScript ES modules, node:test, pytest, Playwright.

**Spec:** docs/superpowers/specs/2026-10-22-session-workspace.md

## Global Constraints

- No new dependencies.
- No hidden reasoning or system/developer messages in transcript capture.
- No live service restarts or trial prompts to running sessions.
- Native attachment limitations remain explicit; never claim unsupported delivery.

## Task 1: Recording foundation

Files: `herdr_engineering/control_plane/inspector.py`, inspector tests and contracts.

- [x] Add failing regression for `herdr:bender:default:w1Z%3ApF` ingest.
- [x] Accept only validated canonical pane escapes and retain invalid-ID rejection.
- [x] Persist redacted messages using existing sink; restore ordered bodies and replay identities, within limits. Exclude reasoning/tool bodies from persistence.
- [x] Run inspector, sink, namespace and ingest tests.

## Task 2: Native transcript adapter

Files: `deploy/herdr-cp-feed-plugin/session-transcript.js`, its tests, `snapshot.js` and snapshot tests.

- [x] Normalize Codex, Claude and Pi user/assistant messages and tool calls from fixture records.
- [x] Resolve files from explicit agent session evidence only; bound filesystem reads and replay; skip incomplete lines and hidden reasoning.
- [x] Integrate inspector envelopes into snapshot publication without changing session lifecycle or losing idempotency.
- [x] Verify session snapshots retain existing behavior and transcript output is bounded.

## Task 3: Shared composer and safe message renderer

Files: new `packages/conversation-*` modules, Collaboration view and client tests.

- [x] Test Markdown escaping and safe links, completion selection/IME, drafts and failed sends.
- [x] Extract keyboard/draft/completion behavior and reusable rendering; reuse in Collaboration without altering leading-agent task permissions.
- [x] Support upload selection/preview/removal with capability-aware submission and preserve failures.
- [x] Verify Node client suite and relevant browser coverage.

## Task 4: Session split workspace

Files: `packages/session-workspace.js`, `packages/app.js`, workspace CSS and tests.

- [x] Add chronological conversation and independent activity panes with resizer and narrow-screen tabs.
- [x] Reuse shared composer and Markdown renderer; keep the composer mounted during stream updates.
- [x] Display explicit helper relationships and GitHub associations, and filter by agent/type/search.
- [x] Preserve scroll/details state; stop streams and requests on navigation.
- [x] Verify renderer/DOM behavior, then run combined regression checks.

## Task 5: Integration verification

- [x] Review cross-slice interfaces and unsupported runtime capabilities.
- [x] Run Python control-plane tests, Node client tests, static checks and available UI checks.
- [x] Report changed files, validation and deployment limits with no live-session side effects.

## Verification and remaining runtime work

- Python control-plane suite: 1,192 passed, 3 skipped, 1 expected failure.
- Full client/browser suite: 267 passed.
- Native feed suite: 108 passed.
- Ruff, JavaScript syntax and diff whitespace checks passed.
- Independent code review findings were fixed with regressions for persistence acknowledgement, HTTP messaging, retry identity, history gaps, full-message expansion and full tool-output expansion.
- Native attachment delivery and retroactive identity for the existing running example remain unavailable; see `../specs/2026-10-22-session-workspace-implementation.md`. Dashboard deployment remains separate.
