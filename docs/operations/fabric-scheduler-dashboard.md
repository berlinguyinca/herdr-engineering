# Scheduler dashboard

The existing Scheduling page now exposes the scheduler policy API, queued
requests, and recent placement records. Host status and decision records refresh
while the page is visible. Preview and policy drafts remain intact when focus
moves away and new telemetry arrives.

## Policy changes

Choose a host, review its captured policy revision, choose a placement policy,
and explicitly confirm before saving. An automatic-mode override on a Mac shows
a warning and requires renewed confirmation. Policy modes govern future scheduler
placements; other launch paths have independent controls. Existing agents continue.
The backend still requires operator authorization and same-origin browser writes.

A newer policy revision blocks submission of the stale draft. Reload policy
explicitly to discard the draft, fetch the latest revision, and confirm again.
Failures retain the draft; navigating away cancels outstanding UI requests and
prevents their late responses from replacing another view. A submitted server
mutation is not rolled back by navigating away.

Mode-only edits preserve omitted allow/deny tag restrictions inside the admission
transaction, including after another coordinator refreshes policy. An explicit
empty list still clears that restriction through the API.
The editor captures and resends the existing restrictions with its revision;
it stays read-only when an older backend omits the restriction lists. This also
protects a confirmed draft if the server is rolled back before submission.

## Queue and history

The queue shows current queued state, with a visible truncation count when the
result is bounded. Recent placement records show up to 200 requests, newest first,
using the immutable request timestamp so retry does not make old work appear new.
Expand an explanation for per-host rejection codes, scores, and recorded CPU/RAM
headroom. Decision time is separate from request time; those figures are snapshots,
not current utilization. Launch claims are not presented as running processes.

History uses a server-side allowlist. Lease tokens, raw request snapshots,
checkpoint content, and arbitrary explanation/measurement fields are excluded.
The mounted view and existing theme/density tokens are reused; no new dashboard
service or runtime launch path is introduced.

## Verification

2026-11-04 scoped validation: **77 Python tests**, **50 Node/client/design-token
checks**, and **16 browser regressions** passed. The PostgreSQL/HTTP/browser
canary is included in the Python selection and also passed separately for the
final desktop/mobile capture. Changed Python files pass Ruff; JavaScript syntax
and `git diff --check` pass. Independent read-only review found one request-time
ordering issue, reproduced and fixed, then approved the scoped implementation.

- New backend regressions fail before the history/tag fixes, then pass afterward.
- Browser regressions cover blurred drafts surviving refresh, late reads versus
  saves, conflicts, errors/retries, failed reload, disposal, Mac warning/confirmation,
  keyboard explanation controls, hostile text, and mobile containment.
- The isolated canary uses real PostgreSQL, HTTP, and Chromium without intercepted
  API responses. It changes a seeded Linux host to disabled and back, checks a
  seeded Mac remains manual-only, verifies viewer denial and persisted tag rules,
  and reads the final policy through a fresh coordinator.
- This is not a live-fleet canary or production deployment. No native jobs are
  launched and the canary keeps admission disabled. Native placement/migration
  remain deferred under the reconciled scope in #290.

Reproduce the browser canary after installing the repository's declared development
dependencies and PostgreSQL tools:

```sh
python -m pytest -q tests/control_plane/test_scheduler_dashboard_canary.py
node --test --test-concurrency=1 tests/playwright/scheduler-dashboard.test.mjs
```

Rollback the UI increment by reverting its commit. Policy changes are durable;
restore an earlier mode through the API/UI with the current expected revision
rather than deleting receipts. Reverting UI code does not revert saved policy.
