# Final data/performance fixes — findings 3, 5 and 6

Implemented and verified in the requested worktree on top of security commit `9ad220f`. This report is committed with the implementation. No subagents, dependencies, live configuration changes, or deployment were used.

## Changes

- **Finding 3:** `/ci/tests` ranks all aggregates by failure count, duration (p95 when available, otherwise latest), and exact identity before applying the existing 200-test and byte limits. Scope includes `omitted_test_count`, `omitted_failure_test_count`, and the ranking policy. A 250-test regression places the lone failure and slowest passing test beyond the former identity-ordered cutoff and verifies both lead the response.
- **Finding 5:** run summary membership and daily grouping use `created_at`. Summary and run-list windows now match the heatmap: UTC calendar days including today, inclusive start and current-time end. Unknown creation times and future runs are excluded. Cross-language regressions exercise summary, heatmap and run list together for 7/14/30 days, with runs immediately before/on/after boundaries and completions crossing those boundaries. Test-attempt history retains its separately documented report-time window.
- **Finding 6:** test-history reads reuse a bounded compact store and validated per-report projections. Retained event versions and exact resolved identity invalidate projections; ordinary pipeline changes reuse unchanged report projections. Imports, new reports, ambiguous legacy binding, eviction and clock changes are covered. Summary/test reads no longer copy compatibility reports; cache-performance reads no longer load reports just to obtain pipelines.
- `/ci/reports?compact=1` returns report counts and run metadata without case payloads. The dashboard requests this projection during polling. Full reports are requested lazily for an exact provider URL, repository ID, repository name and run number. The unfiltered compatibility route still returns full reports. Query validation, viewer authentication, credential redaction and unambiguous legacy binding are tested.
- Counts-only rendering explicitly directs users to compact project test history or selected-run details instead of presenting omitted cases as zero failures. The real HTTP/browser test verifies recurrence evidence through `/ci/tests`, full selected-run results, and absence of unscoped full-report polling.

Production changes are confined to `control_plane/{repo,api,ci_observability}.py`, the client report request method, and the necessary polling/count-rendering integration in `packages/{integrations-view,ci-insights}.js`. Parser security fixes, raw test identity, legacy receipts, missing-report semantics and distinct provider outcomes are preserved.

## Test-first evidence and resolved failures

Initial new regressions produced six backend failures and three frontend failures before implementation. A subsequent regression also failed when a pipeline refresh rebuilt report projections; it now passes while forbidding full report loading and revalidation of unchanged cases. Legacy compact provider binding was likewise reproduced before repair.

The two focused assertion issues are resolved:

1. The existing summary fixture represented an old run only through its finish timestamp. Its creation timestamp now represents the old run as required by the new membership contract; expected counts remain unchanged.
2. The telemetry size-limit test raced a megabyte upload against an intentional early server close, yielding `Broken pipe` before it could read the HTTP rejection. It now sends oversized `Content-Length` headers without a body and still asserts HTTP 400. This directly verifies rejection before body consumption without weakening the limit assertion.

## Final verification

- **378 backend tests passed:** 377 passed in the focused batch, with the telemetry transport test deselected there and then passing independently. Suites: `test_ci_api`, `test_ci_reports`, `test_ci_observability`, `test_ci_performance`, `test_ci_monitor`, `test_ci_history`, `test_ci_provider_binding`, `test_ci_data_performance`. Logs: `/tmp/ci-data-verified-python.log`, `/tmp/ci-data-telemetry-isolated.log`.
- **16 targeted tests passed** after the two assertion fixes: both formerly failing tests plus all 14 new data/performance regressions. Log: `/tmp/ci-data-final-targeted.log`.
- **154 JavaScript/browser tests passed, zero failures:** CI component suites, integration/client suites, and Playwright CI insights, provider binding, charts and health controller. Includes the 117-project interaction budget and responsive layouts. Log: `/tmp/ci-data-verified-js.log`. An earlier overlapping browser run missed its render-time budget; the final serial suite passed the unchanged budget.
- Ruff passed for all modified Python files. Python compileall, Node syntax checks and `git diff --check` passed.
- `lsp_diagnostics` returned zero diagnostics on all changed source/test files but reported `tsc skipped: no tsconfig found`. No TypeScript typecheck or application build is claimed; this project uses Python and native JavaScript modules and defines no corresponding build/typecheck script.

Focused commands:

```sh
PYTHONPATH=. pytest -q tests/control_plane/test_ci_api.py tests/control_plane/test_ci_reports.py tests/control_plane/test_ci_observability.py tests/control_plane/test_ci_performance.py tests/control_plane/test_ci_monitor.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_provider_binding.py tests/control_plane/test_ci_data_performance.py -k 'not test_telemetry_auth_limits_invalid_bundles_and_performance'
PYTHONPATH=. pytest -q tests/control_plane/test_ci_api.py::test_telemetry_auth_limits_invalid_bundles_and_performance
node --test --test-concurrency=1 packages/herdr-web-components/ci-*.test.js packages/herdr-web-components/integrations.test.js packages/herdr-web-client/client.test.js tests/playwright/ci-insights.test.mjs tests/playwright/ci-provider-binding.test.mjs tests/playwright/ci-charts.test.mjs tests/playwright/ci-health-controller.test.mjs
```

## Performance and limits

The regression retains 20 reports × 1,000 cases and more than 8 MiB of serialized source state. Measured warm repository reads were 0.100–0.130 seconds; an HTTP read after a pipeline state change took 0.194 seconds. Compact report counts are below 64 KiB, and bounded test-history responses below 256 KiB. The regression enforces a one-second warm/HTTP budget and forbids full compatibility-report reads and unchanged-case projection rebuilding.

Initial projection of this fixture still costs approximately three seconds once per cold store; newly changed report contents require validation. This is an in-memory derived cache, not a new persistence layer. Retention, omission counts and partial coverage remain explicit; no live-provider performance claim is made.

All requested implementation work is complete. No unresolved blocker remains. The final test processes completed before the stop request was serviced; unrelated workspace processes were left untouched.
