# CI observability command center

Status: design approved in conversation on 2026-10-25; written-spec review pending.

## Goal

Make `#/ci` answer two operator questions quickly and precisely:

1. Why did this CI run fail, down to the failed workflow step, test case, log evidence, revision, and provider link?
2. Which projects and tests are becoming unreliable or slow over time?

The existing Woodpecker provider remains authoritative for execution state, steps, logs, and provider URLs. HerdR adds bounded, durable observation and aggregation so the dashboard can show history without repeatedly rescanning the provider or shipping a large raw snapshot to the browser.

## Current evidence and root causes to address

- `herdr_engineering/ci.py` already reads repositories, pipeline pages, pipeline details, step logs, and report data from Woodpecker.
- `herdr_engineering/control_plane/ci_history.py` performs bounded background history collection, but its provider reads are sequential and the retained history is memory-only.
- `GET /api/v1/ci/history` returns the history snapshot used by the CI health heatmap and run tree. The browser polls this route every five seconds while the server refreshes history on its own interval.
- The current frontend already has project health, charts, pipeline workflow/step rendering, and log selection. The work should extend those boundaries rather than introduce a second CI model or chart framework.
- The current history limits are safety controls, not a complete historical database: repository/page/run/byte caps and incomplete/stale coverage are surfaced. The new design must preserve those honesty rules.

## Product design

### 1. CI command-center header

Keep the existing `#/ci` route and add a stable summary strip above the current views:

- observed projects and coverage state;
- completed pass rate with explicit denominator;
- failed runs in the selected window;
- median and p95 completed-run duration, with sample counts;
- active runs and data freshness.

Every metric shows its time window, branch/scope filters, and whether coverage is complete, partial, stale, memory-only, or unavailable. No metric treats canceled, skipped, queued, or unknown runs as successful or failed unless the provider reports that outcome explicitly.

### 2. Project comparison and trends

Extend the existing heatmap rather than replacing it. Add a selectable project summary rail/table with:

- pass/fail counts and rate;
- failure-rate sparkline;
- duration p50/p95 trend;
- latest status versus selected-window failure count;
- top failing tests and slowest tests when structured reports exist.

The comparison view supports project search, branch/scope filters, 7/14/30-day windows, failures-only, sorting by failure rate or duration, and selection that scopes every chart and the run tree. Project identity remains provider URL plus repository ID plus repository name so collisions are impossible.

Charts use the existing dependency-free SVG/Web Component patterns:

- stacked outcomes by UTC day;
- failure-rate line chart with completed-run denominator;
- p50/p95 duration lines;
- top failing tests Pareto/bar chart;
- per-project sparklines.

Each chart has accessible titles/descriptions and an exact-data table. Charts never imply precision below the available sample count.

### 3. Exact failure explanation

Selecting a run opens the existing inspector and adds a concise failure diagnosis panel:

- provider state, event, branch/PR metadata, revision, timestamps, duration, and provider URL;
- failed workflow(s) and failed step(s), with explicit distinction between reported dependency edges and unknown ordering;
- structured test failures from JUnit/report ingestion: test identity, class/file, message, stack trace bounded and redacted, first observed failure, last observed pass, recurrence count, and duration;
- a bounded log excerpt for the failed step with a link/action to open the full provider log;
- a clear “evidence unavailable” reason when no report or log exists.

The diagnosis must never infer a test failure from log text when a structured report is absent, and must never claim a test is flaky without same-revision retry evidence. “Inconsistent outcomes observed” is the fallback language.

### 4. Project test-history view

For a selected project and branch, show test history grouped by stable reported test identity:

- failure count and failure rate over the selected completed-run window;
- first failure and last pass;
- last observed duration and p50/p95 duration when sample count supports it;
- recurrence and same-revision retry evidence;
- links back to representative runs and failed-step logs.

Only parsed, validated JUnit/report records contribute. A missing report is “not reported,” not zero tests and not a passing run.

## Data flow and architecture

1. Woodpecker remains the execution source. The provider adapter continues to fetch inventory, compact pipeline metadata, details, and logs with bearer credentials held only server-side.
2. A background history collector separates fast head/status refresh from bounded historical backfill. Provider requests are concurrency-limited, deduplicated, and budgeted; an unchanged head does not trigger a full page reread.
3. Compact CI observation events/records persist pipeline outcomes, run timing, repository identity, test-report summaries, and coverage metadata through the existing durable repository/event patterns. Raw logs remain bounded and are fetched on demand; credentials and unredacted report payloads are never stored.
4. Server-side aggregation returns a compact summary model for the command center and project/test views. Raw pipelines are returned only for the selected bounded tree/inspector context.
5. The client uses snapshot revisions/conditional refresh behavior, preserves selections and expanded state, and renders only changed summary/inspector regions.

No new runtime dependency or parallel CI provider abstraction is introduced. Existing `ci_history`, `ci_performance`, report ingestion, provider, project-link, and Web Component boundaries are extended in place.

## Performance and reliability budgets

- Initial `#/ci` summary response: target p95 under 2 seconds when cached data exists.
- Interactive project/run selection: target p95 under 1 second excluding provider log latency.
- Background provider work is bounded by the existing configured time, repository, page, run, total-byte, and concurrency limits.
- A slow or unavailable provider must not block the control-plane request thread or replace retained data with an empty state.
- The browser must not request full pipeline details for every project during summary refresh.
- The UI must remain usable at 390 px and 4K widths, with internal chart/table scrolling and no document-wide horizontal overflow.

## Security and data handling

- The runner continues to use the existing mode-0600 forge-token file; no token is written to YAML, GitHub, events, logs, chart data, or browser storage.
- Report XML is bounded, rejects DTD/entity expansion, validates case/test counts, redacts credentials and sensitive strings, and stores only the bounded structured projection needed for trends and diagnosis.
- Viewer reads retain existing authorization. Report upload and any CI action remain operator-authorized.
- Failure text and log excerpts are escaped in HTML and capped server-side and client-side.

## Acceptance criteria

1. A failed run shows the exact provider-reported failed step(s), a bounded log excerpt, revision/event metadata, and a provider URL.
2. A run with a valid JUnit report shows the failing test identities, messages, first failure, last pass, recurrence, and duration history.
3. A run without a report explicitly says test evidence is unavailable and does not show zero tests.
4. The selected project view shows outcome, failure-rate, and duration trends with exact accessible tables and explicit denominators.
5. A test is labeled flaky only when repeated outcomes on the same revision support that claim; otherwise the UI uses neutral inconsistent-outcome language.
6. Changing project, branch, scope, time window, or heatmap selection updates every summary, chart, test table, and run-tree count consistently.
7. Provider slowness leaves the last valid snapshot visible, marks it stale/partial, and does not block unrelated dashboard routes.
8. Summary refresh does not fetch all pipeline details or logs; detail requests are lazy and bounded.
9. Backend, frontend, and browser tests cover failure explanations, JUnit aggregation, time boundaries, stale/partial states, collisions, sparse history, keyboard interaction, mobile layout, and performance budgets.
10. CI-fix activation remains limited to the existing sandbox allowlist and manual runner path; no systemd timer is installed.

## Verification plan

- Python provider/history/report/API tests with local HTTP fixtures and durable replay/restart coverage.
- JavaScript model/render tests for summary metrics, charts, test histories, explicit unknown states, filters, and selection preservation.
- Browser tests at 390 px, desktop, and 4K widths for no overflow, keyboard operation, lazy loading, chart accessibility, and refresh behavior.
- A live read-only measurement against Woodpecker before and after the collector/summary changes, recording request counts, response sizes, refresh latency, and stale/partial behavior.
- Final checks: `python3 -m pytest` for affected suites, JavaScript tests, syntax/lint/static checks, and a live dashboard smoke check.
