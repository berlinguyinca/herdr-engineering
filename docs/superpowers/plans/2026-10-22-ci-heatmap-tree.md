# CI Heatmap and PR Tree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement the approved project health heatmap with five windows and an identity-safe PR/branch/run tree linked to pipeline details.

**Architecture:** Add a separate bounded asynchronous provider history cache, preserving the existing recent-run monitor and durable report projection. A pure frontend model/render module consumes raw history and explicit per-project coverage. Integrate a dedicated health controller into the CI page and reuse existing pipeline/detail/log interactions; never eagerly fetch tree details.

**Tech Stack:** Python stdlib, native JavaScript modules/components, existing Node tests and Playwright. No new runtime dependencies.

**Spec:** docs/superpowers/specs/2026-10-22-ci-heatmap-tree-design.md (approved 2026-10-22).

## Global Constraints

- No new runtime dependencies.
- Provider execution remains authoritative; missing history is not zero activity.
- Default-branch filtering applies across all five heatmap modes; PR scope is explicit.
- Compound provider-instance/repository/run identity; never guess PR association from branch names.
- UTC trailing24h split at clock-hour boundaries; calendar7/14/30d including today; last24 filtered runs has no date cutoff.
- Preserve keyboard operation, focus, expansion, selection and filter state through refresh; no page overflow.
- Existing durable report retention and live monitor behavior must remain intact.

## Review Focus

1. Filtered last24 runs may be older than30d: pagination/coverage must not treat30d stop as complete for that mode.
2. Provider pagination can overlap, repeat, reorder or fail: deduplicate and report incomplete coverage rather than advancing false watermarks.
3. Same PR number across repositories must not merge; untrusted refs/URLs cannot manufacture associations.
4. Canceled/skipped-only buckets must not be shown as successful or failed; mixed failures survive latest passing run.
5. Background refresh must not steal keyboard focus, collapse the tree or overwrite a newer filter request.

## Files and interfaces

Backend: new `herdr_engineering/control_plane/ci_history.py`, new `tests/control_plane/test_ci_history.py`; modify `ci_monitor.py`, `api.py`, corresponding API tests. `CIMonitor.history()` returns `{available,status,refreshing,stale,repos,pipelines,coverage,scope}`. `pipelines` is flat. Coverage is keyed by string provider repository ID, containing `complete_since`, `exhausted`, `truncated`, `error`, `checked_at`; complete_since is only evidence for a contiguous validated prefix retained in the response. Scopes include provider instance URL in rows. History cache is memory-only/refetched on restart and says so; do not persist backfill through the existing2000-row report projection. Budgeted resumable background reads cap3000runs/repo,20000total,16MiB and60pages/repo. Full-repository exhaustion or enough24matching scopes plus30days allows early completion; caps/errors stay visible.

Frontend model/render: new `packages/ci-health.js`, tests `packages/herdr-web-components/ci-health.test.js`. Exports `ciHealthModel(history,options={},now=Date.now())`, `renderCiHeatmap(model)`, `renderCiTree(model,{selectedRunKey='',expanded=new Set()}={})`, `ciHealthStyles`. Options mode=`hours|runs|7|14|30`,scope=`default|pr|all`, search, repository, branch, failuresOnly, sort, selection=`{projectKey,bucketKey}|null`. Model projects include stable key, repository metadata, filtered runs, cells with stable key, label, runs/counts/state/complete and per-project completeness; selectedRows feeds PR/branch tree. Run buttons use data-health-run with model runKey; cells data-health-project +data-health-bucket. Tree disclosure keys stable; no branch-name PR inference. Defaultbranchunknown shows explicit incomplete/unavailable scope. Exact-count accessible alternative is included. Agent reports final concrete model shape before root integration.

Controller/integration: new `packages/ci-health-view.js`; modify `packages/integrations-view.js`, existing web client and static serving modules/tests. `mountCiHealth(host,client,{onSelectRun,onSelectStep,onLayout,repository,mission,session})` handles polling/preferences and returns `{dispose,selectedRun(record)}`. It loads history only while visible, preserves model/selection through refresh and invokes parent run inspector. Parent opens history-only records directly and reuses current provider detail endpoints; tree expansion can load only selected run workflow/steps through inspector. Existing run tabs/reports remain functional.

## Task1 — Bounded history and coverage

- [x] Write failing Python tests for paginated>20runs,30d cutoff, older filtered24, exhausted empty repos, repeat pages, timestamp ordering/missing timestamps, budget continuation, partial errors, caps and restartreset.
- [x] Run new tests and record RED.
- [x] Implement background cache, monitor-owned lifecycle and GETci/history viewer endpoint with truthful coverage.
- [x] Run history/monitor/API/provider tests, Ruff and diff checks; record GREEN.

## Task2 — Heatmap and PR tree model/render

- [x] Write failing Node tests for exact trailing24h edge buckets,7/14/30dates, last24filteredolderrows, dedup/multi-provider identity, mixed/canceled/unknown states and coverage gray-vs-incomplete.
- [x] Write tests for default-branch PR exclusion, explicit PR ref/URL identity, unknown association, latest-within-selection vs window failure counts, safe escaping and complete repository inventory.
- [x] Run RED, implement pure exports, run GREEN.
- [x] Provide bounded keyboard-accessible renderer, stable data attributes and internal scrolling styles without dependencies.

## Task3 — CI integration and browser acceptance

- [x] Add client history route test and failing browser scenario using real local ControlPlaneServer + synthetic in-memory provider data; no request interception.
- [x] Implement controller, persisted five-mode/scope preferences, cell-to-tree selection and selected-run-to-existing-inspector bridge.
- [x] Preserve tree disclosures/focus/grid scrolling and avoid stale-response races; lazy selected workflow/step expansion only.
- [x] Verify all five modes, PR scope, historical-only run selection, clearselection, responsive30dgrid, keyboard flow, refresh, error/partial coverage and existingCI/reviews regression.

## Task4 — Review and delivery

- [x] Run focused Python/Node/browser checks, then full Python and npm suites once integrated.
- [x] Independent code and architecture review; fix confirmed issues with RED/GREEN evidence.
- [x] Update docs/evidence and approved design status; commit using conventional intent plus Lore trailers.
- [ ] Prepare reviewable PR and deploy through existing authorized deployment workflow only after checks pass; live smoke tests use genuine data and explicit coverage labels.

## Execution ruling

The user approved the design and the standing autonomy directive requests completion without routine confirmation. Execute this plan now using the existing isolated worktree, bounded native implementation lanes, and root-owned integration/verification. This supplies execution authority without another plan-approval handoff. No new production behavior is claimed until verified.
