# CI command-center verification — 2026-10-25

Scope: Task 6 and its focused selection/readiness follow-up from `575d740`.
Local deterministic fixtures only; no live-provider, configuration, credential,
dependency, or polling-interval changes. Original Task 6 results are preserved
below as historical evidence; the follow-up results describe the current code.

## Method

`ci-insights-server.py --117-projects` extends the existing loopback control-plane
and TLS protocol fixture with 116 synthetic projects, each having seven retained
observations (five successes and two failures), plus the existing `org/app` fixture.
The added observations use the existing repository ingestion interface. Their
relative UTC dates stay inside the 14-day window. They cause no extra provider I/O.
A validated JUnit upload supplies the exact failed test for run 3.

The browser measures 20 sequential samples per operation. p95 uses nearest rank
(`ceil(0.95 * n)`). HTTP summary measurements include complete body consumption;
bytes are UTF-8 JSON body bytes, excluding headers. Render readiness measures
reload through all 117 project buttons and two animation frames. Project selection
awaits complete selected-project summary and test-history responses, populated KPI
cards/charts/test history, no pending command-center requests or refresh errors,
and two animation frames. Only then is the sample recorded and selection cleared.
Clear also settles before the next sample. Run selection includes Runs/Failed
navigation, the run click, structured diagnosis, and two animation frames. Neither
interaction measurement includes a requested step log.

Budgets: cached summary p95 < 2,000 ms; project/run selection p95 < 1,000 ms.
The specification provides no separate numeric render or response-byte budget;
the test additionally applies a local render-readiness target of 2,000 ms and
reports bytes without inventing a specification limit. These are local synthetic
measurements, not a production SLO or provider-performance claim.

Request accounting records CI API paths requested by the browser over the complete
performance scenario, separately from the 20 explicit HTTP summary measurements.
The initial summary must issue no pipeline/log reads, and the performance scenario
must issue no log reads. Provider navigation is intercepted at its exact loopback
fixture URL, proving keyboard anchor navigation without accessing a live provider.

## Behavioral coverage

- Project search and Enter selection, failed run, exact escaped test failure,
  recurrence labels, Enter-opened failed-step log, and provider-link navigation.
- 390, 1440, and 3840 px layouts: keyboard chart disclosure, named SVGs and
  descriptions, exact table headers, focusable project comparison, actual mobile
  internal horizontal scrolling, desktop sticky inspector, no document overflow.
- HTTP 503 summary/test refresh retains KPI cards, charts, disclosure, selected
  project/run/step, log and test evidence; status remains explicitly stale/partial.
  Both outage and recovery await completed fleet-summary, project-summary and
  test-history requests and all corresponding UI states; retry preserves selection
  and retained evidence. Separately held project/test responses prove a fast sibling
  cannot finish a selection measurement, and clearing a completed sample aborts no
  selected-evidence requests.
- Existing model/controller coverage verifies identity collisions, sparse samples,
  stale-response rejection, unavailable evidence, and shared context persistence.

## Follow-up results: completed-readiness measurements

The focused 20-sample performance run passed with the original thresholds:

| Measurement | p95 / size | Budget |
| --- | ---: | ---: |
| Summary HTTP | 218.32 ms | < 2,000 ms |
| 117-project render readiness | 1114.23 ms | < 2,000 ms |
| Completed project selection | 336.15 ms | < 1,000 ms |
| Run selection with diagnosis and paint | 480.01 ms | < 1,000 ms |
| Summary body | 74,876 bytes | Recorded only |

Production changes reuse the existing fleet snapshot on project selection/clear,
while manual refresh and polling still refresh fleet/project/test evidence.
Project and pipeline I/O now start before synchronous view synchronization/rendering.
Exact identity, generation/cancellation guards, lazy details, unavailable evidence,
and retained stale snapshots remain in place.

Verification commands and results:

```sh
# 347 passed, 0 failed
node --test --test-concurrency=1 packages/herdr-web-client/*.test.js packages/herdr-web-components/ci-*.test.js packages/herdr-web-components/integrations.test.js
# 3 passed: delayed summary, delayed tests, full outage/recovery
node --test --test-concurrency=1 --test-name-pattern='delayed selected|failed HTTP refresh' tests/playwright/ci-insights.test.mjs
# 1 passed; 20 samples per measurement, values above
node --test --test-name-pattern='117-project summary' tests/playwright/ci-insights.test.mjs
```

The fleet-request regression first failed (two fleet requests instead of one),
then passed. A temporary mutation restoring test-history-only readiness made the
delayed-summary regression fail; the mutation file was removed. Syntax checks for
all four modified JS files and `git diff --check` passed. LSP diagnostics returned
zero diagnostics but explicitly skipped tsc because no tsconfig exists; no
build/lint/typecheck script is defined. Backend files were unchanged and the
historical backend suite was not rerun for this follow-up.

Browser request counts in the completed performance run: history 27, summary 50,
performance 27, reports 27, tests 25, pipeline 23, fix 1, log 0. The separate
summary HTTP sample count was 20. Polls are included. Summary/project selection
still caused no eager pipeline or log request.

**Remaining verification caveat:** the first complete browser-suite run was
14 passed / 1 failed: the performance case timed out waiting for 117 project
buttons during reload, before selection measurements. This timeout was not
reproduced in the instrumented focused performance run above; its cause remains
unresolved. Failure diagnostics now include browser errors, failed requests,
HTTP error responses, project count and visible status. A subsequent full-suite
run was stopped at the user's request to finish (six scenarios had passed; the
performance scenario was still running). There is no claim of a fully green
final browser suite or a fixed reload flake. No timeout, budget, or retry policy
was relaxed, and no live service/configuration was touched.

Local evidence: `/tmp/task6-fix-components.log`, `/tmp/task6-fix-readiness.log`,
`/tmp/task6-fix-mutation.log`, `/tmp/task6-fix-browser.log`,
`/tmp/task6-fix-reload-diagnostic.log`, `/tmp/task6-fix-browser-final.log`.

## Original Task 6 results

Environment: Node v22.23.3, Python 3.12.3, pytest 9.0.3, local headless Chromium.

Backend command (325 passed, 98.56 seconds):

```sh
python3 -m pytest tests/test_ci.py tests/control_plane/test_ci_observability.py tests/control_plane/test_ci_history.py tests/control_plane/test_ci_monitor.py tests/control_plane/test_ci_reports.py tests/control_plane/test_ci_api.py tests/control_plane/test_ci_performance.py
```

Client/model/controller command (327 passed, 0 failed):

```sh
node --test packages/herdr-web-client/*.test.js packages/herdr-web-components/ci-*.test.js
```

Browser command (final run: **12 passed, 1 failed**; interaction budget failure):

```sh
node --test --test-concurrency=1 tests/playwright/ci-*.test.mjs
```

Static verification (all passed):

```sh
node --check tests/playwright/ci-insights.test.mjs
node --check tests/playwright/ci-health.test.mjs
python3 -m ruff check tests/playwright/ci-insights-server.py
git diff --check
```

The Python fixture also parsed with `ast.parse` and executed through the browser
suite. No build/typecheck script applies to these test-only JavaScript/Python edits.

Initial failures corrected during test authoring: the provider URL uses
`/repos/17/pipeline/3`, sparse duration history displays explicit insufficient data,
and the legacy health test required explicit default-branch selection after the
new 14-day/all-runs defaults plus a pipeline-specific step selector. Production
code and existing behavior assertions were preserved; no retries were added.

Limits: Chromium only; no live provider or production network measurement. The
117-project fixture has 816 observed runs; the existing backend suite also checks
120 projects/20,000 cached runs. Coverage percentages were not measured. Raw run
logs and screenshots remain untracked outside the evidence document.


## Original Task 6 performance evidence and unresolved gate

**Test health: NEEDS ATTENTION.** The final complete browser run failed the project
selection p95 assertion. The same sample set also measured run selection over its
budget (that assertion was not reached after the project assertion failed). No
threshold, retry, or production behavior was changed to force a green result.

| Measurement | Final p95 / size | Budget | Result |
| --- | ---: | ---: | --- |
| Summary HTTP | 515.46 ms | < 2,000 ms | Pass |
| 117-project render readiness | 1633.85 ms | < 2,000 ms (local target) | Pass |
| Project selection | 1305.59 ms | < 1,000 ms | Fail |
| Run selection | 1029.62 ms | < 1,000 ms | Over budget; assertion not reached |
| Summary body | 74,876 bytes | No specified byte limit | Recorded |

Final browser request counts over the entire performance scenario:

- `/api/v1/ci/history`: 28
- `/api/v1/ci/summary`: 88
- `/api/v1/ci/performance`: 29
- `/api/v1/ci/reports`: 29
- `/api/v1/ci/tests`: 23
- `/api/v1/ci/pipeline`: 21
- `/api/v1/ci/fix`: 1
- `/api/v1/ci/log`: 0
- Separately issued summary HTTP samples: 20

These counts include periodic refreshes and repeated reload/selection operations;
they are not a single-page-load request count. Initial summary plus all reload and
project-selection cycles passed the no-pipeline/no-log assertion before run selection.
The explicit failed-step scenario separately verifies exactly one requested log.

Two earlier performance passes on the same implementation measured respectively
summary/render/project/run p95 values of 201.58/1072.03/780.48/933.91 ms and
171.67/992.98/745.52/943.74 ms. The final sample therefore exposes a variable gate,
not a consistently satisfied budget. `uptime` immediately after the final sample
reported load averages 33.18, 26.85, 33.60; this is environmental context, not proof
of causation. Do not treat an earlier green run as superseding this final failure.

Recommended handoff: investigate interaction latency on a controlled runner and
profile the real summary/test/inspector paths before changing implementation. Keep
the one-second target intact. Production performance changes are outside Task 6.
All final functional browser scenarios passed, as did all 325 backend and 327
client/model/controller tests. No further rerun was used to seek a passing sample.
