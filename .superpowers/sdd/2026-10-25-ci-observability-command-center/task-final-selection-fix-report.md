# Final selection-consistency fixes

All three requested findings are resolved.

## Changes

- `packages/ci-health-view.js`, `packages/ci-health.js`, `packages/integrations-view.js`, `packages/ci-insights.js`, and `packages/ci-observability-view.js` propagate explicit window mode and exact selected project/run identity through Health, summary, Tests, and Runs. Last-24-runs selection has no calendar-day cutoff. Unselected run mode uses the latest 24 runs per project; an exact selection remains exact, including older runs exposed by search. Empty run slots do not create a misleading selection. Hour mode retains its trailing-24-hour meaning.
- `packages/herdr-web-client/client.js`, `herdr_engineering/control_plane/api.py`, `ci_observability.py`, and `repo.py` carry and validate the mode/run contract, require exact provider URL, provider repository ID, repository name and run number for exact run requests, and select matching report evidence. Non-calendar requests omit days, responses expose days=null, and visible scope labels describe the actual mode. Calendar behavior remains 7/14/30 days.
- Search/failures-only edits clear bucket bounds and selected run, invalidate affected summary/test requests, and refresh consumers consistently. Health no longer clears a bucket while Tests silently retains its bounds.
- The legacy Period selector offers only 7/14/30 days and synchronizes these windows with the command center and Health. The separate performance window remains available.
- Focused regressions cover exact old-run selection, provider/repository identity collisions, same-timestamp runs, old report evidence, last-24 limits, invalid selections, hourly boundaries, search/failures-only changes from both Health and command-center controls, and all legacy period options. A real-HTTP browser regression exercises an older-than-14-days run end to end.

## Regression evidence

Before implementation, the new run API case returned HTTP 400, the client serialized an unwanted days parameter, the mounted run-selection test never received exact run identity, command-center search/failures changes did not refresh Tests without bounds, and the period selector still offered 0/90. These regressions pass after the fixes. Existing Health-origin clearing tests also pass. One new period test initially attempted to operate a hidden Runs control; its setup now opens Runs before interacting, without weakening assertions.

## Verification

Passed, exit 0:

```sh
python3 -m pytest tests/control_plane/test_ci_api.py tests/control_plane/test_ci_selection_contract.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_observability.py tests/control_plane/test_ci_data_performance.py -q --tb=short
```

221 tests passed in 107.75 seconds. Log: `/tmp/ci-selection-backend.log`.

After extending the selection regressions with old report timestamps and hourly boundaries:

```sh
python3 -m pytest tests/control_plane/test_ci_selection_contract.py -q --tb=short
```

37 tests passed in 19.16 seconds. Log: `/tmp/ci-selection-contract-final.log`. This overlaps the broader backend suite; these counts are not additive.

```sh
node --test --test-concurrency=1 packages/herdr-web-client/client.test.js packages/herdr-web-components/ci-health.test.js packages/herdr-web-components/ci-health-view.test.js packages/herdr-web-components/ci-observability-view.test.js packages/herdr-web-components/ci-charts.test.js packages/herdr-web-components/ci-insights.test.js packages/herdr-web-components/integrations.test.js tests/playwright/ci-health-controller.test.mjs
```

138 tests passed, zero failures, in 40.03 seconds. Log: `/tmp/ci-selection-frontend-final.log`.

```sh
node --test --test-concurrency=1 --test-name-pattern='shared selection:' tests/playwright/ci-insights.test.mjs
```

Both real-HTTP browser tests passed, zero failures, in 31.10 seconds. Log: `/tmp/ci-selection-http-browser.log`.

Static validation passed: Node syntax checks on all nine changed JS/MJS files, Ruff and Python compilation on all five changed Python files, and `git diff --check`. `lsp_diagnostics` returned zero diagnostics for all changed source/test files, but reported `tsc skipped: no tsconfig found`; this is not a TypeScript typecheck claim. The repository serves JavaScript directly and defines no frontend build/typecheck script.

## Scope and delivery

Existing redaction, security, data and performance regression suites pass; no assertions or performance thresholds were relaxed. Retained evidence and bounded projection/cache behavior remain intact. No live configuration was changed, no dependencies were added, and no subagents were dispatched. No temporary/debug artifacts are included. Full-repository tests and unrelated environment-sensitive browser timing gates were not rerun.

This report is included in the fix commit. Its commit ID and final worktree status are supplied in the final response. No remaining blocker.
