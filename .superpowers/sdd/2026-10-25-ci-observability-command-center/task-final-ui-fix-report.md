# Final UI review fixes

Implemented only the two requested findings on top of `ccc2ef3`. No subagents, dependencies, live configuration changes, or deployment were used.

## Changes

- `packages/integrations-view.js`: render an escaped, visible `ig-warning` status alongside retained selected-run test evidence after a refresh fails. The existing successful-response handler clears the error; the rendered warning participates in the inspector signature so failure and recovery both redraw correctly.
- `packages/ci-observability-view.js`: chart copy now describes grouping by run creation time (`created_at`) and UTC calendar days including today.
- `packages/herdr-web-components/ci-observability-view.test.js`: browser regression exercises successful selected-run report loading, a failed refresh retaining the same evidence with a visible stale warning, and recovery replacing the evidence and removing the warning. It also checks that evidence from the renamed repository does not enter the inspector.

The production diff changes only two rendering lines. Exact run identity, compact polling, lazy full-report reads, data projections, performance limits, and security handling from `ccc2ef3` remain unchanged.

## Verification

- Red/green regression: the new test failed before the fix because the warning never became visible, then passed after the fix (1/1).
- Directly affected suites rerun: **33/33 passed**, including the success → failure → recovery browser regression.
- Broader focused suite: **154/155 passed**. The sole failure was `local application modules load once when the browser asset transport is disconnected`: its Python fixture did not announce its port within 20 seconds (`Fixture timeout`), before page loading or UI assertions. An isolated retry reproduced the same startup timeout. This remains a validation gap; no timeout or unrelated implementation was changed.
- The 117-project performance regression passed: p95 summary 244 ms, render 1,014 ms, project selection 438 ms, run selection 550 ms.
- `node --check` passed for all three changed JavaScript files; `git diff --check` passed.
- `lsp_diagnostics` reported zero diagnostics for those files but explicitly skipped TypeScript checking because no `tsconfig` exists. No JavaScript build/typecheck/lint script is configured in `package.json`; syntax checks and actual browser execution provide the applicable verification.
- Reviewed the complete diff for unrelated changes and temporary/debug code; none were added.

Commands:

```bash
node --test --test-name-pattern='selected run report refresh' packages/herdr-web-components/ci-observability-view.test.js
node --test packages/herdr-web-components/ci-observability-view.test.js packages/herdr-web-components/integrations.test.js
node --test --test-concurrency=1 packages/herdr-web-components/ci-*.test.js packages/herdr-web-components/integrations.test.js packages/herdr-web-client/client.test.js tests/playwright/ci-insights.test.mjs tests/playwright/ci-provider-binding.test.mjs tests/playwright/ci-charts.test.mjs tests/playwright/ci-health-controller.test.mjs
node --test --test-name-pattern='local application modules load once' tests/playwright/ci-insights.test.mjs
node --check packages/integrations-view.js
node --check packages/ci-observability-view.js
node --check packages/herdr-web-components/ci-observability-view.test.js
git diff --check
```

Test output was captured outside the worktree in `/tmp/ci-final-ui-focused-20261025.log`, `/tmp/ci-final-ui-tests-20261025.log`, and `/tmp/ci-final-ui-retry-20261025.log`. The report is committed with the fixes.
