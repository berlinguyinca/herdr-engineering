# Final remaining selection-consistency fixes

Both requested findings are implemented. No live configuration, dependencies, backend production code, or security/data retention behavior changed. No subagents were dispatched.

## Changes

- `packages/ci-health.js`: hourly selections now carry stable UTC hour boundaries. Membership intersects those bounds with the current trailing 24-hour window, includes runs exactly at now, and excludes the selected hour's upper boundary. At an exact hour rollover, the new current-hour cell holds runs created at that instant. Coverage uses the intersected lower boundary.
- `packages/integrations-view.js`: render-cache invalidation includes time-window membership, so unchanged provider payloads cannot leave stale Runs rows or charts after clock advance. This tracks membership changes rather than forcing a redraw every clock tick.
- Performance-window changes now affect only cache/trend data. The main 7/14/30 period also leaves the performance window independent. The explanatory UI text reflects this separation.
- `packages/herdr-web-components/ci-health.test.js`: stable bounds and exact-hour membership regression.
- `packages/herdr-web-components/ci-observability-view.test.js`: mounted regressions cover both edge-hour selections through refresh, rollover, and expiry, plus performance 7/14 changes under every main 7/14/30 selection, with and without a selected bucket. They verify controls, Health, summary, Tests, Runs, saved preferences, and subsequent request bounds remain consistent.
- `tests/control_plane/test_ci_selection_contract.py`: clock-advance cases compare actual HTTP summary/test results with JavaScript Health, Tests, and Runs membership using the original clicked bounds.

## Test-first evidence

Before the fixes, current-hour selection excluded a run created exactly at selection time; subsequent refresh could not include later runs within that hour. Performance 7→14 changed the legacy period and pulled an older run into Runs/Tests while Health and summary stayed on 7 days. Mounted clock tests additionally exposed stale Runs rendering when unchanged payloads crossed a window boundary. These failures were reproduced before their respective fixes.

## Verification

- Targeted JavaScript/browser regression command: **6 passed, zero failures**, including both edge-hour refresh/expiry cases and all three main periods. Log: `/tmp/ci-last-selection-green-js.log`.
- Cross-language HTTP hourly regressions: **2 passed, 37 deselected** (`python3 -m pytest tests/control_plane/test_ci_selection_contract.py -k selected_hour_stays_shared -q --tb=short`).
- Node syntax checks passed for all five changed JavaScript/MJS files. Ruff and Python compilation passed for the changed Python test. `git diff --check` passed.
- `lsp_diagnostics` returned zero diagnostics for all six source/test files, but reported `tsc skipped: no tsconfig found`. This is not a TypeScript typecheck claim. The repository serves native JavaScript and has no frontend build/typecheck script.

- Broader frontend run: **144 passed, 1 failed** in 59.31 seconds. The failure was a stale fixture in `tests/playwright/ci-charts.test.mjs:72` expecting a 2020 run inside the default 14-day window. The fixture now uses nine days ago, preserving all existing assertions that it appears at 14 days and disappears at 7 days. Both tests in that file then passed: **2 passed, zero failures**, exit 0, in 2.74 seconds. Logs: `/tmp/ci-last-selection-frontend.log`, `/tmp/ci-last-selection-charts-final.log`. Thus all 145 distinct frontend tests passed across the broad run and corrected-file rerun; the full combined command was not repeated.
- Expanded backend run: **160 passed, no failures before interruption**, in 72.68 seconds. Stopped with SIGINT following the user's immediate-completion instruction. This is a partial run, not a full-suite pass. Log: `/tmp/ci-last-selection-backend.log`.

Commands:

```sh
node --test --test-name-pattern='hours use stable|performance window stays independent|selected hour .* stays shared' packages/herdr-web-components/ci-health.test.js packages/herdr-web-components/ci-observability-view.test.js
python3 -m pytest tests/control_plane/test_ci_selection_contract.py -k selected_hour_stays_shared -q --tb=short
node --test --test-concurrency=1 packages/herdr-web-client/client.test.js packages/herdr-web-components/ci-health.test.js packages/herdr-web-components/ci-health-view.test.js packages/herdr-web-components/ci-observability-view.test.js packages/herdr-web-components/ci-charts.test.js packages/herdr-web-components/ci-insights.test.js packages/herdr-web-components/integrations.test.js tests/playwright/ci-health-controller.test.mjs tests/playwright/ci-charts.test.mjs
node --test --test-concurrency=1 tests/playwright/ci-charts.test.mjs
python3 -m pytest tests/control_plane/test_ci_api.py tests/control_plane/test_ci_selection_contract.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_observability.py tests/control_plane/test_ci_data_performance.py tests/control_plane/test_ci_performance.py tests/control_plane/test_ci_provider_binding.py -q --tb=short
```

The last command was interrupted; the remaining commands completed. The six targeted JS tests and two chart rerun tests overlap the 145-test frontend suite and are not additive.

## Scope and limitations

Prior security, data, identity, and performance checks/assertions were not weakened. Production edits are confined to the two frontend modules above. Full-repository tests and the separate 117-project browser timing gate were not rerun. No implementation blocker remains. The commit includes this report; its ID and clean-worktree verification are reported in the final response.
