# Scheduler admission increment — 2026-10-28

Tracking: [P00–P10 acceptance #290](https://github.com/berlinguyinca/herdr-engineering/issues/290).
This increment is based on `d8cfcebc94fc507c68082fb3504a97906e684a05` and
ports verified scheduler work onto current event, authentication, and telemetry
architecture. It does not constitute complete phase acceptance or fleet rollout.

## Behavior and decisions

- Reuse PostgreSQL event storage and one admission advisory lock for shared
  reservations, queue limits, policy checks, leases, and execution claims.
- Derive child scope/depth from persisted parent context. Owner/project/mission,
  resource, workspace, fan-out, and burst constraints apply across reloads.
- Host-token resource observations carry stable identity and capture time.
  Generic ingest cannot grant scheduler authority or forge policy receipts.
  Historical client policy envelopes are ignored; contaminated policy snapshots
  are disabled for reconciliation.
- Keep transaction receipts private until COMMIT succeeds. Restore local policy,
  reservation, and lease projections on abort. Snapshot refreshed peer state and
  retain event-only recovery when explicit snapshot maps are absent.
- Reuse the fleet host telemetry loop for the additional authenticated resource
  projection. Failures and telemetry opt-out preserve existing host reporting.
- Default new HTTP admission off. Preserve preview, cancellation, and lease
  maintenance during rollback. The operations guide lists effective central
  config keys; server flags require restart.
- Reuse the existing scheduling view's visibility-aware refresh/morph behavior.
  Exclude stale hosts from eligibility counts and label claims separately from
  running processes; no replacement dashboard or frontend service was introduced.
- No inference/model/GPU worker ownership changes. Native process supervision,
  all-family launch integration, migration certification, full dashboard, and
  rollout remain separate gates. Draft checkpoint/scope/redaction work is in
  [#305](https://github.com/berlinguyinca/herdr-engineering/pull/305),
  [#307](https://github.com/berlinguyinca/herdr-engineering/pull/307), and
  [#308](https://github.com/berlinguyinca/herdr-engineering/pull/308).

## Changed implementation files

`herdr_engineering/scheduler.py`, `host_policy.py`, `resource_probe.py`,
`fabric_agent.py`, and `cli.py`; `herdr_engineering/control_plane/repo.py`,
`sink.py`, `scheduler_service.py`, `ingest.py`, `bridge.py`, and `api.py`.
Configuration guidance is in `config/herdr-engineering.example.yaml` and
`docs/operations/fabric-scheduler.md`.
The existing `packages/scheduler-view.js` and its tests have two counter fixes.

Tests include the scheduler domain, service, persistence, queue limits,
ingest authority, HTTP admission, sink contracts, PostgreSQL contention,
lock ordering, commit rollback, resource probe, and fleet reporting suites.
The acknowledged sink fixture moved to `tests/conftest.py` so mixed-directory
importlib collection retains its scope. Existing ingest metric assertions are
preserved while authority-grant expectations are replaced with denial checks.

## Evidence

- Independent bounded review reproduced findings before correction and approved
  the final component. This approval excludes full-product/native certification.
- PostgreSQL tests use actual temporary clusters and separate processes/sessions:
  100-request capacity contention; 20-request queue cap contention; crash/rollback;
  cross-replica policy/resource/lease changes; historical envelope rejection;
  peer-release snapshot restore; and no projection/publication before commit.
- Targeted upstream sweep: 296 passed in 90.53s before the last snapshot and
  suppression deltas. Delta checks include 126 V2/ingest/persistence tests,
  96 focused scheduler checks, 36 PostgreSQL/rollback/sink checks, and the
  peer-release restore regression. Final frozen-tree results are recorded below.
- Final frozen Python selection: **297 passed in 61.28s** across scheduler,
  API, ingest, resource, sink, V2, events, and bridge suites. Scheduling view
  counter regressions: **three passed**.
- Fleet reporting/probe regression: 55 passed in 22.90s. Whole CI API file:
  96 passed in 48.64s while investigating a broader-run failure.
- All changed Python files pass Ruff; `git diff --check` passes.
- Broad control-plane/fleet/domain run: **4,945 passed, 12 skipped, two expected
  failures, one deselected, one failed** in 1,116.64s. The failure is
  `test_ci_api.py::test_history_is_viewer_readable_flat_and_memory_only`:
  `coverage['17']['exhausted']` was false during background head revalidation.
  The case and all 96 CI API tests passed separately. A deterministic Events
  barrier reproduced the race, then a bounded completion wait corrected the
  test while preserving every original assertion. The corrected CI API file
  passed **96 tests in 50.07s**. The fresh broad rerun passed **4,947 tests**,
  with **12 skipped, one deselected, and two expected failures**, in 1,110.72s.
  The final snapshot/suppression deltas are covered by the fresh targeted run,
  rather than claiming that the earlier-started broad run included them.
- Scheduling preview Playwright regression passed: edited inputs survive refresh
  and progress/errors remain visible. Repository-wide Ruff still has baseline
  findings outside the scoped component; all modified Python files pass.
- Scheduler POSTs also use the existing same-origin mutation guard. The new
  operator-credential cross-origin regression failed before the guard and
  passed afterward; the complete HTTP/API selection passed **46 tests**. This
  small final security delta has its own fresh check rather than being silently
  attributed to an earlier-started broad run.

## Remaining acceptance and rollback

Audit and re-attest historical host ownership before enabling admission.
Reconcile active legacy reservations lacking request context; do not fabricate
scope or delete receipts. Land the separately reviewed event-redaction fix
before production execution. Finish native adapters and process-backed lease
lifecycle evidence, live dashboard behavior, Linux/macOS canary, measured load
comparison, fault matrix, and rollback validation before closing #290.

For rollback, set central `control_plane.scheduler_admission=false` and restart
the control plane. Keep records and telemetry; existing agents are not killed.
No production flag was enabled and no automatic migration was attempted.
