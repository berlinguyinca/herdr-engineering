# Dashboard Space Utilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use desktop and mobile space more effectively across HerdR while preserving exact operational evidence, accessibility, routes, and refresh state.

**Architecture:** Add a route-aware fluid shell and shared density rules, then reshape individual views around compact toolbars, rows, and bounded master/detail regions. Each task changes one visual system and is proven with real-browser assertions before production CSS or markup changes.

**Tech Stack:** Vanilla JavaScript, Web Components, CSS custom properties, Node test runner, Playwright.

**Spec:** `DESIGN.md` and `docs/evidence/dashboard-space/audit.md`

## Global Constraints

- No new runtime dependencies.
- Preserve API contracts, route deep links, filters, selection, expansion, focus, freshness and coverage semantics.
- Operational views use available width; prose remains readable and mobile has no document-level horizontal overflow.
- Preserve semantic status text, keyboard focus, tables/disclosures, reduced motion and adequate mobile touch targets.
- Use TDD: every changed interaction or layout contract gets a failing browser test before production changes.

## Review Focus

- A 1920px viewport uses substantially more than the old 1080px inner ceiling while a 390px viewport never overflows.
- Refresh and route navigation retain filter, focus, selection and expansion state after layout changes.
- Long repository, mission, model and event values wrap or truncate accessibly without forcing page overflow.
- Sparse/empty pages omit inapplicable controls without implying unavailable data is healthy or complete.
- Split panes collapse to one column at usable container widths and keep logs/composer/actions reachable.

---

### Task 1: Fluid shell and Overview composition

**Files:**
- Modify: `packages/herdr-web-components/app-shell.js`
- Modify: `packages/overview-view.js`
- Modify: `tests/playwright/ui.test.mjs`

**Interfaces:**
- Produces: shell content region that is fluid up to 1600px and an Overview grid with mission, fleet and secondary panels assigned explicit responsive areas.
- Consumes: existing `herdr-app-shell`, `data-overview` panel keys and route behavior.

- [x] Add browser assertions for a content inner width above 1350px at 1920px, no overflow at 390px, mission/fleet/secondary panel placement, and unchanged Overview links.
- [x] Run the focused test and confirm it fails on the 1080px ceiling/current grid.
- [x] Implement the fluid shell and three-column Overview composition, retaining readable intro width and mobile single-column behavior.
- [x] Run focused Overview/UI tests and confirm they pass.
- [x] Commit with a Lore-compatible `feat(ui)` message.

### Task 2: Compact catalog controls and rows

**Files:**
- Modify: `packages/sessions-view.js`
- Modify: `packages/mission-catalog-view.js`
- Modify: `packages/services-view.js`
- Modify: `packages/topology-view.js`
- Modify: `tests/playwright/ui.test.mjs`
- Modify: `tests/playwright/topology.test.mjs`

**Interfaces:**
- Produces: consistent compact heading/filter rows and desktop record rows with secondary metadata available in disclosures/details.
- Consumes: Task 1 fluid shell; existing filter, grouping, focus and refresh contracts.

- [x] Add browser assertions for first-record vertical position, visible record density, filter wrapping, long-content containment and preserved keyboard focus.
- [x] Run focused catalog tests and confirm density assertions fail.
- [x] Compact controls and catalog rows without changing state classification or link targets.
- [x] Run focused catalog tests and confirm they pass.
- [x] Commit.

### Task 3: Activity timeline and workspace height

**Files:**
- Modify: `packages/app.js`
- Modify: `packages/session-workspace.css`
- Modify: `tests/playwright/ui.test.mjs`
- Modify: `tests/playwright/session-workspace.test.mjs`

**Interfaces:**
- Produces: compact expandable activity rows and agent panes sized from remaining viewport height.
- Consumes: existing event descriptions, activity ordering, activity JSON disclosures and workspace mobile tabs.

- [x] Add browser assertions for collapsed event density, expandable full detail, remaining-height workspace, composer reachability and mobile pane behavior.
- [x] Run focused tests and confirm failures against cards/75vh sizing.
- [x] Implement timeline semantics and `calc(100dvh - …)` workspace sizing with safe minimums.
- [x] Run focused tests and confirm they pass.
- [x] Commit.

### Task 4: CI workspace composition and Projects assessment

**Files:**
- Modify: `packages/integrations-view.js`
- Modify: `packages/ci-health-view.js`
- Modify: `tests/playwright/ci-health.test.mjs`
- Modify: `tests/playwright/ci-insights.test.mjs`
- Modify: `tests/playwright/ui.test.mjs`

**Interfaces:**
- Produces: CI Health & pipeline/Runs/Tests workspace navigation; records whether current Projects evidence supports a layout change.
- Consumes: existing CI filters, five heatmap windows, compound run identity, report coverage and Projects repository/issue selection.

- [x] Add browser assertions for workspace navigation, retained filter/selection state, wide pipeline layout, narrow stacked layout and no duplicated provider-origin clutter.
- [x] Run focused CI tests and confirm failures.
- [x] Implement workspace navigation without combining independent history/report filters; retain the existing Projects tree because synchronized issue data was unavailable for a grounded redesign.
- [x] Run focused CI tests and confirm they pass.
- [x] Commit.

### Task 5: Analytics, responsive navigation, Collaboration assessment and full verification

**Files:**
- Modify: `packages/app.js`
- Modify: `packages/herdr-web-components/app-shell.js`
- Modify: `tests/playwright/ui.test.mjs`
- Modify: `DESIGN.md`
- Modify: `docs/evidence/dashboard-space/audit.md`

**Interfaces:**
- Produces: composed analytics panels, compact accessible mobile navigation, a documented Collaboration layout decision and final measured evidence.
- Consumes: shared shell and density patterns from Tasks 1–4.

- [x] Add browser assertions for analytics composition and mobile navigation access/focus; verify existing Collaboration workspace coverage before deciding whether a layout change is warranted.
- [x] Run focused tests and confirm failures.
- [x] Implement the remaining compositions and update the design/evidence documents with final measurements; retain Collaboration's established sparse-room layout.
- [x] Run all JavaScript/browser tests, JavaScript syntax checks and diff checks.
- [x] Capture and inspect screenshots at 1920×1080, 1440×900, 1280×800, 768×1024 and 390×844.
- [x] Request whole-branch code and visual review, resolve material findings through RED/GREEN, and commit.