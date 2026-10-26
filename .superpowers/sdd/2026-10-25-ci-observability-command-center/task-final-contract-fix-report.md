# Final selection and credential contract fixes

Worktree: `/home/wohlgemuth/IdeaProjects/.worktrees/ci-observability-design-20261025`.
Base: `eb1340a` (`feat/ci-observability-command-center`). Implementation and this report are committed together.

## Changes

1. **Detached summary credential redaction.** `ci_monitor.py` applies the existing exact configured-token redactor to the compact summary response. It then reconstructs `project_id` from the scrubbed identity fields, preventing JSON escaping from concealing credentials inside derived identities. Tests cover provider URL, repository name and provider repository ID, including quoted/backslash-containing tokens. Both exported retained evidence and cached history remain unchanged. No provider requests are added to the summary handler.
2. **Shared selection.** Summary and Tests accept `scope`, `start` and `end`; both client methods forward the same filters. Bounds require ordered, timezone-aware timestamps. Temporal buckets are half-open (`start <= created_at < end`), intersected with the selected UTC calendar-day window and the current time. Health callbacks propagate the selected cell bounds, and Runs and Tests receive scope and bounds through the command-center context. Clearing a bucket or switching projects clears its bounds. Backend aggregation reads published history and retained runs without fetching full reports or provider details. Scoped test attempts require an exact provider URL, repository ID/name, run, branch and revision match to selected run evidence; late report uploads therefore remain attached to the selected run's creation bucket.
3. **Calendar-day run metrics.** Tests workspace run totals, success/failure rates, durations and missing-report counts now use creation time and the same UTC calendar days as summary, Runs and heatmap. Unknown creation times and future runs are excluded. Test-attempt history retains its rolling `reported_at` window. The compact Tests response explicitly describes its separate run-time basis/window.

Production files: `control_plane/{api,ci_monitor,ci_observability,repo}.py`; `packages/{ci-health,ci-health-view,ci-insights,ci-observability-view,integrations-view}.js`; `packages/herdr-web-client/client.js`.

No dependencies, subagents, live configuration, deployment or external production writes were used. Existing retention limits, response ranking/caps, compact-report polling, projection reuse, lazy details/logs, idempotent ingestion and raw test identities are preserved. Inventory matching uses a map rather than scanning all repositories for each run.

## Regression evidence

- Before implementation, the new focused tests produced **nine backend failures and three JavaScript failures**: credential leakage, rejected bucket filters, calendar-window count drift and dropped selection fields. The real HTTP/browser case also demonstrated that Tests retained a push run under PR scope.
- A follow-up credential test failed in all three identity positions when the token contained JSON-escaped characters. Rebuilding derived IDs from redacted fields fixed all six credential cases.
- A follow-up mounted-page test failed when changing the exact project through Runs retained the previous bucket. It now verifies cleared bounds and cleared health selection.
- Route tests exercise all/PR/default scope; exact inclusive-start/exclusive-end boundaries; late reports; malformed, missing and reversed bounds; and 7/14/30-day missing-report counts.
- The real HTTP/browser regression uses a local TLS provider fixture, the actual client and server, and no API response mocks. PR scope excludes push runs; selecting a day narrows summary, exact test history, the health tree, Tests workspace and Runs; clearing it restores the PR selection.
- Two existing fixtures were aligned with the requested contract: Tests accepts `scope=all` while still rejecting invalid scope; the legacy provider-binding fixture now supplies a creation timestamp while retaining its original two-unmatched-runs assertion.

## Verification

**400 backend tests passed** in 148.26 seconds. Log: `/tmp/ci-contract-verified-python.log`.

The completed broad JavaScript/browser run had **158 passes and one failure**, solely the existing run-selection p95 timing gate (`/tmp/ci-contract-focused-js.log`). The subsequent functional rerun completed at least 146 tests successfully before being stopped at the user's explicit finish-now instruction. The final focused contract result is recorded below. No performance assertion was relaxed.

**All five final focused JavaScript/browser contract tests passed**, including the real HTTP PR/bucket selection regression and clearing bounds on project change. Log: `/tmp/ci-contract-final-focused.log`. The focused backend selection/credential batch also passed all 23 tests (`/tmp/ci-contract-final-targeted.log`), and is covered by the 400-test passing backend suite.

Static verification: Ruff, Python compilation, Node syntax checks and `git diff --check` passed on modified files. `lsp_diagnostics` returned zero diagnostics, but its implementation reported `tsc skipped: no tsconfig found`; this is not a TypeScript typecheck. The repository uses native JavaScript modules/Python and defines no application build/typecheck script.

Commands:

```sh
PYTHONPATH=. pytest -q tests/control_plane/test_ci_api.py tests/control_plane/test_ci_observability.py tests/control_plane/test_ci_reports.py tests/control_plane/test_ci_monitor.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_provider_binding.py tests/control_plane/test_ci_data_performance.py tests/control_plane/test_ci_selection_contract.py tests/control_plane/test_ci_performance.py
node --test --test-concurrency=1 packages/herdr-web-components/ci-*.test.js packages/herdr-web-components/integrations.test.js packages/herdr-web-client/client.test.js tests/playwright/ci-insights.test.mjs tests/playwright/ci-provider-binding.test.mjs tests/playwright/ci-charts.test.mjs tests/playwright/ci-health-controller.test.mjs
node --test --test-concurrency=1 --test-name-pattern='117-project summary|shared selection' tests/playwright/ci-insights.test.mjs packages/herdr-web-components/ci-observability-view.test.js
```

**One environment-sensitive performance gate remains failed:** run selection p95 must be below 1,000 ms; the isolated measurement was **1,160.16 ms** (the initial concurrent sample was 2,028.40 ms). In the same isolated run, summary p95 was 428.14 ms, render p95 1,471.72 ms and project selection p95 555.81 ms, all within their unchanged limits. Host load averages were approximately 56/61/63. This supports an environment-sensitive interpretation, but causality was not established: a baseline comparison was prepared but deliberately not run after the user instructed completion. The temporary baseline checkout was removed. Log: `/tmp/ci-contract-verified-browser.log`.

An added health-tree assertion initially counted lazy descendants before opening their disclosures. The final focused regression opens both levels and waits for their asynchronous rendering before asserting the selected run; no production behavior or assertion threshold was weakened. The user explicitly authorized committing despite the host-load performance sample.

## Limits

Selection is based on retained/published evidence, not a claim of complete provider history. A scoped/bucket-filtered attempt without matching run evidence is excluded rather than guessed. Unfiltered test-attempt history retains its prior report-time semantics. No live-provider latency or production validation is claimed.

Completion: committed with the explicitly accepted timing exception. The temporary baseline checkout is removed; no live configuration was changed.
