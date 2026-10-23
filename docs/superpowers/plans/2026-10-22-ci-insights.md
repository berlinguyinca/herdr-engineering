# CI Insights Implementation Plan

> **For agentic workers:** Use the repository's native parallel execution authorization; execute each owned slice with failing tests first and perform a whole-branch review.

**Goal:** Deliver persistent CI stability/test reporting and a responsive rendered pipeline.
**Architecture:** Extend existing event repository and CI API; add focused frontend report and pipeline modules, integrated into existing CI page.
**Tech Stack:** Python stdlib XML, existing Python server/event sink, native JavaScript modules, Node test, pytest, Playwright.
**Spec:** docs/superpowers/specs/2026-10-22-ci-insights-design.md

## Global Constraints
No new runtime dependencies. Keep provider authority, actual dependency semantics, auth boundaries, secret redaction and missing-data distinctions. Work only in isolated worktree.

## Review Focus
- Missing/zero and malformed XML reports; bounded parser and API validation.
- Duplicate uploads/replay and out-of-order histories preserve identity.
- Mixed branches/repos/retries do not yield misleading counts/flakiness.
- Cancelled/skipped runs do not inflate success rates.
- Long labels, parallel steps and mobile layouts remain usable without page overflow.

### Task 1: Durable reporting backend
Own new control_plane/ci_reports.py, repo.py/api.py/ci_monitor.py integration and Python tests.
- [x] Write/run failing parser, history, replay and authenticated API tests.
- [x] Implement bounded JUnit ingestion and retained pipeline observations using existing event sink.
- [x] Expose GET ci/reports and POST ci/reports; verify real-server API and replay tests.

### Task 2: Pipeline rendering
Own packages/ci-pipeline.js, provider dependency preservation in ci.py, focused tests.
- [x] Write/run failing graph grouping/state/escaping tests.
- [x] Implement stateful, responsive pipeline renderer with data-step controls and genuine dependencies only.
- [x] Verify node/provider tests; report exact integration exports.

### Task 3: Overview and test explorer integration
Own packages/ci-insights.js, integrations-view.js, client.js and frontend tests.
- [x] Write/run failing summary/history/filter/render tests.
- [x] Implement truthful stability metrics, test histories/failures/slow tests and scoped filters.
- [x] Integrate reports fetch and pipeline renderer with full-width inspector; preserve refresh/focus behavior.

### Task 4: Verification and operating docs
- [x] Run Python/Node suites and targeted desktop/mobile browser tests; address regressions.
- [x] Document report publication, retention limits, unavailable states and validation evidence.
- [x] Review complete diff independently and resolve material findings.
