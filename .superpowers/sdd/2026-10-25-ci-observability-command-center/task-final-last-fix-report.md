# Final remaining review findings — completed

## Changes

- `herdr_engineering/control_plane/ci_observability.py`: shared `pipeline_outcome` maps provider state/status aliases to the supported safe outcome keys: success, failure, running, queued, skipped, blocked, canceled and unknown. Unsupported states accumulate under unknown rather than becoming dynamic JSON keys. Raw evidence is not rewritten.
- `herdr_engineering/control_plane/{ci_history,repo}.py`: provider summary publication and HTTP summary aggregation use that normalization. KPI, project and daily chart outcome maps preserve every run count, existing completed/active denominators and duration samples. Blocked remains distinct from queued.
- `packages/ci-health.js`: calendar-day cells carry stable half-open UTC midnight-to-midnight bounds. Existing run filtering intersects them with the current time. The inclusive current-time exception is limited to hourly cells, so a run at exactly midnight cannot enter the previous day's selection.
- `tests/control_plane/test_ci_selection_contract.py`: real HTTP regressions cover configured synthetic credentials in both state and status, for both cached history and retained observations. They assert no token anywhere in serialized responses, exact KPI/project/chart outcome maps and totals, and unchanged retained evidence. A separate HTTP check advances the clock with fixed day bounds and verifies matching summary/test membership without admitting future or next-day runs.
- `tests/control_plane/test_ci_history.py`: real collector regression verifies safe provider summary outcome keys, preserved counts and unchanged provider evidence.
- `packages/herdr-web-components/{ci-health,ci-observability-view}.test.js`: regressions cover 7/14/30-day windows, refresh, future-run exclusion, exact midnight and next-day rollover. The mounted-page test advances the clock from 12:30 to 14:00, refreshes, and verifies Health, Tests and Runs all include the newly eligible run while summary/test request bounds stay unchanged.

## Regression evidence

Before production edits, four HTTP cases and the provider-summary case failed on synthetic credential leakage. The calendar-model regression failed on the moving end bound. After correcting the browser fixture's report timestamps to match its controlled clock, the mounted-page regression reproduced the actual drift: Health displayed two selected runs while Tests retained one. An additional exact-midnight assertion failed when the next day's run entered the previous day's cell; restricting the inclusive exception to hourly cells repaired it.

All these cases pass in the final focused suites below. No existing assertion or performance threshold was relaxed.

## Final verification

**212 backend tests passed**, exit 0 (97.27 seconds):

```sh
python3 -m pytest tests/control_plane/test_ci_api.py tests/control_plane/test_ci_selection_contract.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_observability.py tests/control_plane/test_ci_data_performance.py -q --tb=short
```

Log: `/tmp/ci-final-last-backend.log`.

**99 JavaScript/component/browser tests passed**, exit 0 (10.52 seconds):

```sh
node --test --test-concurrency=1 packages/herdr-web-components/ci-health.test.js packages/herdr-web-components/ci-health-view.test.js packages/herdr-web-components/ci-observability-view.test.js packages/herdr-web-components/ci-charts.test.js packages/herdr-web-components/ci-insights.test.js packages/herdr-web-components/integrations.test.js
```

Log: `/tmp/ci-final-last-frontend.log`.

**1 real-HTTP browser selection regression passed**, exit 0 (11.16 seconds):

```sh
node --test --test-concurrency=1 --test-name-pattern='shared selection:' tests/playwright/ci-insights.test.mjs
```

Log: `/tmp/ci-final-last-http-browser.log`.

Static checks passed:

```sh
python3 -m ruff check herdr_engineering/control_plane/ci_observability.py herdr_engineering/control_plane/ci_history.py herdr_engineering/control_plane/repo.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_selection_contract.py
python3 -m compileall -q herdr_engineering/control_plane/ci_observability.py herdr_engineering/control_plane/ci_history.py herdr_engineering/control_plane/repo.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_selection_contract.py
node --check packages/ci-health.js
node --check packages/herdr-web-components/ci-health.test.js
node --check packages/herdr-web-components/ci-observability-view.test.js
git diff --check
```

`lsp_diagnostics` was called on all eight changed source/test files. It returned zero diagnostics but explicitly skipped TypeScript checking because no tsconfig exists; this is not a typecheck claim. The project serves JavaScript directly and defines no frontend build/typecheck script. Ruff, Python compilation, Node syntax checks and runtime tests provide the applicable validation.

## Scope and delivery

Both requested findings are resolved. Existing redaction, exact project identity, scope filtering, retained-evidence and selection-clearing behavior remain covered by the focused suites. No live configuration was read or changed, no dependencies were added, and no subagents were dispatched. No unrelated source changes or temporary/debug files are included. Full-repository tests and the previously documented environment-sensitive timing gate were not rerun; no claim is made about those unrelated checks.

This report is included with the fix commit. The final response supplies its commit ID.
