# CI observability command center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `#/ci` explain individual failures precisely, expose trustworthy per-project/test history and trends, and remain responsive while the CI-fix runner is enabled only for the existing sandbox allowlist.

**Architecture:** Extend the existing Woodpecker provider, `CIHistory`, durable CI report repository, `/api/v1/ci/*` routes, and native SVG/Web Component views. Add a compact server-side observation/summary layer, split fast head refresh from bounded backfill, lazy-load pipeline details/logs, and preserve explicit stale/partial/unknown coverage. No new provider abstraction, chart dependency, systemd timer, or secret storage path.

**Tech Stack:** Python 3.12, `python3 -m pytest`, existing control-plane repository/event journal, Woodpecker v3 API, vanilla ES modules, native Web Components, dependency-free SVG/CSS, existing Playwright/browser harness.

**Spec:** `docs/superpowers/specs/2026-10-25-ci-observability-command-center-design.md`

## Global Constraints

- Woodpecker remains authoritative for execution state, workflow/step metadata, logs, and provider links.
- Missing JUnit/report data is “not reported,” never zero tests or a passing result.
- Flakiness is claimed only with same-revision retry evidence; otherwise use neutral inconsistent-outcome language.
- Canceled, skipped, queued, blocked, running, and unknown states remain distinct from success/failure.
- Preserve provider URL + repository ID identity; never merge same-named repositories without an exact identity match.
- No new runtime dependencies.
- No environment variables for product settings; YAML remains the configuration source.
- CI-fix activation is limited to `berlinguyinca/herdr-ci-fix-sandbox`; the runner timer remains uninstalled.
- No token values enter Git, YAML, events, logs, browser storage, or test output.
- Use fresh worktrees from `origin/main`; never edit the dirty main checkout.

---

### Task 1: Lock current behavior and add measurement seams

**Files:**
- Modify: `herdr_engineering/control_plane/ci_history.py`
- Modify: `herdr_engineering/control_plane/ci_monitor.py`
- Create: `herdr_engineering/control_plane/ci_observability.py`
- Test: `tests/control_plane/test_ci_history.py`
- Test: `tests/control_plane/test_ci_api.py`
- Test: `tests/control_plane/test_ci_observability.py`

**Interfaces:**
- `CIHistory.snapshot()` continues to return the existing compatibility shape.
- New `CIObservationStore.record_pipeline(row)`, `record_report(report)`, and `summary(filters)` methods own compact observed-run/test aggregation.
- New summary records include `revision`, `provider_url`, `provider_repo_id`, `repository`, `branch`, `run_number`, `state`, timestamps, report availability, and coverage metadata.

- [ ] **Step 1: Write regression tests** for the current compatibility response, a failed pipeline with no report, an explicit partial snapshot, and the measurement counters exposed only through test hooks.
- [ ] **Step 2: Run the focused tests** with `python3 -m pytest tests/control_plane/test_ci_history.py tests/control_plane/test_ci_api.py tests/control_plane/test_ci_observability.py -q`; confirm the new module tests fail before implementation.
- [ ] **Step 3: Implement the observation store** as a bounded, redacted projection over existing repository/event primitives. Keep raw pipeline detail and log reads out of summary storage.
- [ ] **Step 4: Add counters/timestamps** for provider calls, summary rows, payload bytes, cache hits, stale state, and refresh duration; do not include credentials or raw logs.
- [ ] **Step 5: Re-run focused tests** and verify old `/api/v1/ci` consumers receive unchanged fields.
- [ ] **Step 6: Commit** with `feat(ci): add bounded observability summary store`.

### Task 2: Persist and aggregate structured test history

**Files:**
- Modify: `herdr_engineering/control_plane/ci_reports.py`
- Modify: `herdr_engineering/control_plane/repo.py:1643-1745`
- Modify: `herdr_engineering/control_plane/ci_observability.py`
- Test: `tests/control_plane/test_ci_reports.py`
- Test: `tests/control_plane/test_ci_observability.py`

**Interfaces:**
- `parse_junit()` remains the bounded XML parser and returns validated test-case projections.
- Add `ControlPlaneRepo.ci_test_history(repository=None, branch=None, test=None, days=30)` returning `{tests, scope}`.
- Add `CIObservationStore.test_summary(...)` returning stable test identity, pass/fail/inconsistent counts, first failure, last pass, duration quantiles with sample counts, representative run IDs, and evidence availability.

- [ ] **Step 1: Add failing tests** for first failure/last pass, same-revision retry evidence, missing reports, duplicate report attempts, ambiguous provider identity, and restart/replay preservation.
- [ ] **Step 2: Run `python3 -m pytest tests/control_plane/test_ci_reports.py tests/control_plane/test_ci_observability.py -q`** and capture the expected missing-summary failures.
- [ ] **Step 3: Implement compact test-case aggregation** from existing parsed report summaries; store only bounded message/stack excerpts and redacted evidence references.
- [ ] **Step 4: Implement neutral flakiness classification**: `proven_flaky` only when the same test identity has different outcomes on repeated attempts of the same revision; otherwise emit `inconsistent_outcomes` or `stable`.
- [ ] **Step 5: Verify durable restore** with the existing disk-event-journal fixture and ensure old report API output remains unchanged.
- [ ] **Step 6: Commit** with `feat(ci): persist test failure history and evidence summaries`.

### Task 3: Make provider refresh fast and non-blocking

**Files:**
- Modify: `herdr_engineering/control_plane/ci_history.py`
- Modify: `herdr_engineering/control_plane/ci_monitor.py`
- Modify: `herdr_engineering/ci.py`
- Test: `tests/control_plane/test_ci_history.py`
- Test: `tests/control_plane/test_ci_reports.py`
- Test: `tests/control_plane/test_ci_monitor.py`

**Interfaces:**
- `CIHistory.refresh()` retains the existing stale/partial contract.
- Add a bounded provider-read scheduler with explicit `max_in_flight`, per-refresh budget, and preserved repository cursor.
- Add a compact `summary_snapshot()` path that does not serialize every retained pipeline row.

- [ ] **Step 1: Add failing tests** proving unchanged heads do not reread every historical page, provider calls never exceed the concurrency/budget limits, and a slow provider leaves the last valid snapshot marked stale.
- [ ] **Step 2: Run the targeted history/monitor suite** and verify those tests fail against the sequential collector.
- [ ] **Step 3: Implement head-first refresh**: inventory and page 1 are prioritized; historical continuation is queued behind them and bounded by the existing safety budget.
- [ ] **Step 4: Implement compact summary snapshots** with a monotonic revision and stable `generated_at`, while retaining full history for compatibility only where the existing route requires it.
- [ ] **Step 5: Add request-count and payload-size assertions** to the local provider fixture; confirm unrelated control-plane requests do not wait for provider refresh.
- [ ] **Step 6: Run `python3 -m pytest tests/control_plane/test_ci_history.py tests/control_plane/test_ci_reports.py tests/control_plane/test_ci_monitor.py -q`**.
- [ ] **Step 7: Commit** with `perf(ci): prioritize cached summaries and bounded history refresh`.

### Task 4: Add compact summary and failure-diagnosis API routes

**Files:**
- Modify: `herdr_engineering/control_plane/api.py:1867-1903`
- Modify: `herdr_engineering/control_plane/ci_monitor.py`
- Modify: `herdr_engineering/control_plane/repo.py`
- Modify: `packages/herdr-web-client/client.js`
- Test: `tests/control_plane/test_ci_api.py`
- Test: `packages/herdr-web-client/client.test.js`

**Interfaces:**
- Add `GET /api/v1/ci/summary?days=7|14|30&repository=&branch=&scope=` for compact KPI/project/chart data.
- Add `GET /api/v1/ci/tests?days=7|14|30&repository=&branch=&test=` for project/test history.
- Extend `GET /api/v1/ci/pipeline` response with a bounded `diagnosis` projection; do not include full logs in this response.
- Keep `GET /api/v1/ci/log` as the lazy log route.
- Client methods: `getCiSummary(filters)`, `getCiTests(filters)`, and existing `getCiPipeline/getCiLog` remain abortable.

- [ ] **Step 1: Write failing API tests** for viewer access, invalid filters, compact response size, explicit partial/stale scope, exact project identity, failure diagnosis, and lazy detail behavior.
- [ ] **Step 2: Run `python3 -m pytest tests/control_plane/test_ci_api.py -q`** and confirm the new route assertions fail.
- [ ] **Step 3: Implement server-side filtering and aggregation** using the observation store; never perform a provider page scan inside an HTTP request.
- [ ] **Step 4: Add bounded diagnosis** that joins provider step failures with exact parsed report cases and returns evidence-unavailable reasons where joins are not proven.
- [ ] **Step 5: Add client methods and route-query serialization** with `AbortSignal` propagation and no token-bearing fields.
- [ ] **Step 6: Verify response-size and latency fixtures** against 117+ synthetic projects and 20,000 retained runs.
- [ ] **Step 7: Commit** with `feat(ci): expose compact summaries and failure diagnosis`.

### Task 5: Build the professional CI command-center UI

**Files:**
- Modify: `packages/integrations-view.js`
- Modify: `packages/ci-health-view.js`
- Modify: `packages/ci-charts.js`
- Create: `packages/ci-observability-view.js`
- Modify: `packages/ci-pipeline.js`
- Test: `packages/herdr-web-components/ci-health-view.test.js`
- Test: `packages/herdr-web-components/ci-charts.test.js`
- Create: `packages/herdr-web-components/ci-observability-view.test.js`

**Interfaces:**
- `mountCiObservability(host, client, options)` owns summary cards, project comparison, test-history selection, and chart refresh.
- Existing `mountCiHealth` owns heatmap/tree behavior and receives the selected project/window context.
- Existing `renderCiPipeline` receives diagnosis/test evidence but stays provider-step focused.

- [ ] **Step 1: Add model tests** for KPI denominators, missing-report language, inconsistent versus proven-flaky labels, p50/p95 sample thresholds, and project identity collisions.
- [ ] **Step 2: Add render tests** for summary cards, sparklines, outcome/failure/duration charts, exact-data tables, stale/partial banners, and no-data states.
- [ ] **Step 3: Implement the summary header and project rail** using existing design tokens, native SVG, accessible labels, and the current filter state.
- [ ] **Step 4: Implement failure-test history and Pareto/duration charts** with lazy data loading after project selection.
- [ ] **Step 5: Integrate the exact failure diagnosis panel** into the existing inspector without duplicating step/log fetching.
- [ ] **Step 6: Preserve selection, expansion, scroll, and refresh state** across summary updates and provider errors.
- [ ] **Step 7: Run the focused Node test files** and verify all new render assertions pass.
- [ ] **Step 8: Commit** with `feat(ui): add CI observability command center`.

### Task 6: Add browser and performance verification

**Files:**
- Modify: `tests/playwright/ci-insights.test.mjs`
- Modify: `packages/herdr-web-components/ci-observability-view.test.js` for any model gaps found
- Create: `docs/evidence/ci-observability/validation.md`

**Interfaces:**
- Browser fixtures use a local control-plane server and deterministic CI summary/report fixtures; no external token is embedded.
- Evidence records request count, response bytes, summary latency, detail latency, and stale/partial behavior.

- [ ] **Step 1: Add browser scenarios** for project filtering, selecting a failing run, opening a failed step, reading a failed test, and following the provider link.
- [ ] **Step 2: Add responsive checks** at 390 px, desktop, and 4K widths for keyboard navigation, chart tables, sticky inspector, internal scrolling, and no document overflow.
- [ ] **Step 3: Add refresh/error scenarios** proving the last valid snapshot stays visible and selected context survives a failed refresh.
- [ ] **Step 4: Add a synthetic 117-project performance fixture** and assert summary response/render budgets from the spec.
- [ ] **Step 5: Run `python3 -m pytest` for affected backend suites plus the browser suite**, then write the evidence file with exact commands/results.
- [ ] **Step 6: Commit** with `test(ci): verify command center evidence and budgets`.

### Task 7: Enable the scoped CI-fix runner and perform live smoke verification

**Files:**
- Modify live YAML only: `/home/wohlgemuth/.config/herdr-engineering/fleet/fleet-config.yaml`
- Use: `deploy/control-plane/deploy.sh` from a clean worktree at the approved implementation ref
- Verify: `/api/v1/health`, `/api/v1/ci/fix`, `systemctl --user list-timers`, runner process list
- Verify: `https://github.com/berlinguyinca/herdr-ci-fix-sandbox/pull/1` history and no new unscoped branches

**Interfaces:**
- `control_plane.ci_fix.enabled` becomes `true` only with `hosts: [bender]`, the existing sandbox allowlist, and the existing denylist intact.
- The manual invocation remains `ci-fix-runner.py once --api-key-file ... --forge-token-file ...`; no timer is installed.

- [ ] **Step 1: Add a config-diff check** that asserts only `ci_fix.enabled` changes from false to true and the repository restrictions remain unchanged.
- [ ] **Step 2: Apply the YAML change with `apply_patch`; never print token contents.**
- [ ] **Step 3: Restart/redeploy the control-plane services** using the documented extended health settings; if the image build stalls, preserve the old healthy image and use the narrow service restart fallback already proven in the handoff.
- [ ] **Step 4: Run the runner once** with the existing mode-0600 personal forge token and agent key; record “no queued request” or the exact bounded result without exposing credentials.
- [ ] **Step 5: Verify health, enabled route/config, no timer, allowlist/denylist, and host/process cleanup.**
- [ ] **Step 6: Commit only repository-side implementation/docs changes; live YAML remains outside Git.**

### Task 8: Final review, cleanup, and handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-10-25-ci-observability-command-center-design.md` only if implementation evidence changes a stated contract
- Create: `docs/evidence/ci-observability/validation.md` if not created in Task 6

- [ ] **Step 1: Run `git diff --check`, affected `python3 -m pytest`, JavaScript tests, browser tests, and static/syntax checks.
- [ ] **Step 2: Run a read-only security scan for token/log/report leakage and confirm no new dependency was added.
- [ ] **Step 3: Verify the live dashboard route, summary latency, failure diagnosis, chart data, stale behavior, and CI-fix route.
- [ ] **Step 4: Verify no runner timer, no stray CI-fix process, no leftover sandbox workspace/socket, and no unapproved deletion.
- [ ] **Step 5: Request code review, resolve findings, and merge the implementation PR without admin bypass or forged statuses.
- [ ] **Step 6: Report changed files, merged PR, measured performance, live config state, remaining risks, and sandbox/runner URLs.
