# Operations Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make HerdR an attention-first, configurable 4K operations workspace with stronger Projects and CI inspection.

**Architecture:** Extend existing pure view models and route controllers. Keep preferences local and bounded, derive attention from current Overview data, and enhance CI presentation without changing its retention contracts.

**Tech Stack:** Vanilla JavaScript, Web Components, CSS, Node test runner, Playwright, Python deployment scripts.

**Spec:** `docs/superpowers/specs/2026-10-22-operations-workspace-design.md`

## Global Constraints

- No new dependencies or route/API contract changes unless comparison evidence proves one is necessary.
- Preserve exact identity, freshness, partial coverage, focus, selection, and mobile containment.
- Use failing tests first and validate 3840/2560/1920/768/390.

## Review Focus

- Malformed or stale local preferences fall back safely without hiding Attention.
- Same-name repositories from multiple providers remain distinct without adding noise to unique names.
- Refresh and polling preserve retained data, focus, selections, and pane scroll positions.
- Pipeline comparison never joins different repositories or invents missing steps/timings.
- Partial source failures do not create reassuring zero attention counts.

---

### Task 1: CI identity and retained refresh evidence

**Files:** Modify `packages/ci-health.js`, `packages/herdr-web-components/ci-health.test.js`, `tests/playwright/ci-health-controller.test.mjs`.

- [ ] Add failing tests for collision-only labels and retained snapshots during refresh/failure.
- [ ] Implement deterministic collision suffixes while preserving full identity and existing project keys.
- [ ] Run focused CI model/controller tests and commit.

### Task 2: Sticky Projects workspace

**Files:** Modify `packages/github-view.js`, `tests/playwright/github.test.mjs`.

- [ ] Add failing wide-screen tests for independent pane scrolling, sticky placement, focus, and refresh preservation.
- [ ] Add viewport-bounded pane CSS above the existing desktop breakpoint and restore document flow below it.
- [ ] Verify all five responsive widths and commit.

### Task 3: Shared density preference

**Files:** Modify `packages/herdr-web-components/app-shell.js`, `packages/herdr-design-system/theme.css`, route CSS as needed; test in component and Playwright suites.

- [ ] Add failing tests for sanitized persistence, immediate application, keyboard access, and mobile touch sizing.
- [ ] Implement Comfortable/Compact/Dense using `data-density` and existing tokens; collapse the sidebar by 900px.
- [ ] Verify representative Overview, Projects, CI, Services, and mobile screens; commit.

### Task 4: Attention-first configurable Overview

**Files:** Modify `packages/overview-view.js`, `packages/herdr-web-components/overview.test.js`, `tests/playwright/ui.test.mjs`.

- [ ] Add failing pure-model tests for severity, deduplication, links, partial sources, and preference sanitization.
- [ ] Implement derived Attention plus persisted card order/visibility controls; keep Attention fixed and health in the footer.
- [ ] Add adaptive wide layout and keyboard/focus regression coverage; commit.

### Task 5: Pipeline inspection and comparison

**Files:** Modify `packages/ci-pipeline.js`, `packages/integrations-view.js`, related component and Playwright tests.

- [ ] Add failing tests for collapsed successful steps, bounded zoom, same-repository comparison, and unknown evidence.
- [ ] Restore the CI Health/Runs/Tests workspace switch expected by the existing browser contract, then implement graph controls, minimum heatmap cell widths/sticky headers, a wide run/inspector split, and a bounded comparison projection using already-observed runs.
- [ ] Verify logs/actions remain attached to the active run and responsive stacking works; commit.

### Task 6: Activity and Analytics wide compositions

**Files:** Modify `packages/app.js` and `tests/playwright/ui.test.mjs`.

- [ ] Add failing 3840/768 tests for an Activity summary/detail rail, Analytics auto-fitting cards, and zero overflow.
- [ ] Keep Activity chronological while using wide space for type filters, counts, coalesced repetition, and selected detail; make Analytics a responsive operational grid using existing data.
- [ ] Verify keyboard selection, chronological order, and all five widths; commit.

### Task 7: Whole-dashboard visual verification

**Files:** Update `DESIGN.md` and durable evidence documentation if present.

- [ ] Capture populated route screenshots at 3840/2560/1920/768/390 in every density.
- [ ] Run full tests, lint/static checks available in the repository, and independent code/visual review.
- [ ] Fix material findings and commit.

### Task 8: Collaboration certificate renewal repair and release

**Files:** Modify deployment documentation or scripts only if diagnosis proves a repository defect.

- [ ] Preserve the valid edge certificate, repair the missing Certbot source, and run a renewal dry-run.
- [ ] Merge reviewed changes, deploy the exact merge SHA with collaboration enabled, and confirm every container plus live dashboard commit.
- [ ] Re-run 4K live geometry/overflow checks and record remaining limitations.
