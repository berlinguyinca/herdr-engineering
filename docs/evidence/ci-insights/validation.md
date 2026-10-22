# CI insights validation — 2026-10-22

Implemented on `feat/ci-test-insights` in the isolated `herdr-ci-insights` worktree.

## Verification

- `python3 -m pytest -q`: **1127 passed, 6 skipped, 1 xfailed** (207.52 s), after the final backend changes.
- `npm test`: **225 passed, 0 failed**, including the existing UI suite and new CI browser scenario.
- Final targeted JavaScript rerun: **51 passed**; client real-HTTP/cancellation check **16 passed**; component suite **160 passed**.
- Final CI browser rerun after backend identity/retry changes: **1 passed**.
- Ruff on all changed/new Python files, JavaScript syntax checks and `git diff --check`: clean.
- Independent whole-change code review: approved; no unresolved findings.

The new browser scenario uploads actual XML over HTTP into the real control-plane API and reads actual run details and step logs through a local HTTPS Woodpecker protocol server. It verifies repository/branch/period filters, success-rate denominator, test counts/recurrence, optional provider identity, step selection, red failure styling, all-test pagination and search, focus preservation through polling, wide pipeline layout, mobile vertical layout, and no page overflow or browser errors.

Durability tests cover the existing durable sink and event store through real SQLite storage, replay, full retention eviction/re-observation, failed disk writes followed by retry, a real HTTPS provider through monitor persistence failure/recovery, and optional/explicit provider identities with late observations and backdated report timestamps. Parser tests cover unsafe XML, bounds, actual case counts, secret redaction and stable full identities despite long or redacted display labels.

## Visual evidence

Browser-generated local artifacts (ignored by git):

- `artifacts/ci-insights/overview.png`
- `artifacts/ci-insights/desktop.png` — 1440px viewport; pipeline inspector wider than 900px.
- `artifacts/ci-insights/mobile.png` — 390px viewport; vertical steps and contained table scrolling.

Screenshots were directly inspected. Measured failure border: `rgb(220, 53, 69)`; no document overflow at either viewport. The visual verdict is recorded under `.omx/state/ci-insights/ralph-progress.json`.

## Operating boundaries

This validation uses local servers and synthetic reports; production was not deployed or mutated. CI jobs must publish JUnit reports using `python3 -m herdr_engineering.ci_report_upload`; see `docs/ci-test-reports.md`. Keyed deployments require an operator key; an intentionally open trusted-network deployment can use the explicit uploader `--allow-open` option. Durable history requires the existing event sink; deployments without it explicitly display memory-only storage. Retained observations are bounded and do not claim complete provider history. Pipeline arrows require provider-reported dependencies.

## Deployment-candidate integration

Rebased onto deployed main `88c4d5e`, preserving fleet-controlled CI actions and the newer session/collaboration interfaces. Independent code and architecture lanes approved the integrated candidate plus the following compatibility fixes:

- Explicit trusted-open upload option matching the existing Tailnet deployment, without changing server authentication.
- Bounded acknowledged-observation fingerprints independent of the smaller retained-history window; unchanged inventories above 2,000 runs do not repeatedly persist evicted observations.

Fresh integrated verification: `python3 -m pytest -q --durations=8 -o faulthandler_timeout=90`: **2103 passed, 6 skipped, 1 xfailed** (607.68 s). `npm test`: **287 passed**. Uploader/monitor focused tests and Ruff pass. An earlier integrated run had one timing-sensitive pre-existing collaboration SSE quota test failure; five isolated reruns and the complete clean integrated run passed without changing that test or its assertions.

Read-only live checks: Woodpecker authentication succeeds, 117 repositories and 452 recent runs are connected; the dashboard uses PostgreSQL. Standard `deploy.sh --dry-run` on bender validates central configuration, Compose and the enabled collaboration overlay. No schema changes are required.
