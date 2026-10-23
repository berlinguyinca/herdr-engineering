# CI Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cache-operation telemetry, rolling p50/p95 trends, and visible CI regression indicators to the existing Woodpecker/HerdR pipeline.

**Architecture:** The runner writes bounded, sanitized cache-operation records beside existing timing records. A retryable host relay uploads validated bundles to HerdR's existing CI report ingestion path, where durable event history supplies filtered aggregates and alert calculations. The current CI page renders cache health, trends, and alerts without adding outbound notification side effects.

**Tech Stack:** Python standard library runner and relay, HerdR Python API/event store, plain JavaScript/SVG UI, existing pytest/Node/Playwright tests, Quobyte storage.

**Spec:** `docs/superpowers/specs/2026-10-23-ci-observability-design.md`

## Global Constraints

- Never send commands, arguments, environment values, filesystem paths, credentials, cache keys, archive members, stdout, or cache contents to HerdR.
- Cache compatibility keys remain hashed and opaque.
- Invalid or oversized bundles are rejected as a whole; ingestion failure never changes the wrapped build exit status.
- Aggregates require five valid samples for percentiles and ten operations for hit-rate alerts.
- Regression rules are visible-only: no email, chat, automatic retry, cancellation, or deployment.
- Existing repository, branch, period, mission, session, stale, partial, retention, and memory-only semantics remain intact.
- No new third-party dependencies or global cache-limit increases.

### Task 1: Runner cache-operation telemetry

**Files:**
- Modify: `/home/wohlgemuth/IdeaProjects/woodpecker-slurm/bin/wpci-cache`
- Test: `/home/wohlgemuth/IdeaProjects/woodpecker-slurm/tests/test_cache.py`
- Modify: `/home/wohlgemuth/IdeaProjects/woodpecker-slurm/docs/persistent-cache.md`

**Interfaces:**
- Produce `cache-operation-v1` JSON records under the configured timing/proof directory with `operation_id`, `schema_version`, `repo`, `pipeline`, `phase`, `kind`, `operation`, `outcome`, `wall_seconds`, `compressed_bytes`, `expanded_bytes`, and `entry_count`.
- Preserve existing command status and cache restore/save behavior.

- [ ] Write failing tests for hit, miss, failed save, bounded numeric fields, redaction, and stable idempotent operation IDs.
- [ ] Run the focused cache tests and confirm the new assertions fail against the current helper.
- [ ] Add a single sanitization/record-writing function and invoke it after restore and save maintenance completes.
- [ ] Keep record writes atomic and bounded; omit optional fields when unavailable and never serialize command/environment/path data.
- [ ] Run the focused cache tests, the existing 135-test cache gate, `py_compile`, and `git diff --check`.
- [ ] Commit the runner change with `feat(cache): record bounded cache operation telemetry`.

### Task 2: Quobyte summary relay

**Files:**
- Create: `/home/wohlgemuth/IdeaProjects/woodpecker-slurm/bin/wpci-telemetry-relay`
- Test: `/home/wohlgemuth/IdeaProjects/woodpecker-slurm/tests/test_telemetry_relay.py`
- Modify: `/home/wohlgemuth/IdeaProjects/woodpecker-slurm/docs/persistent-cache.md`

**Interfaces:**
- `wpci-telemetry-relay scan --root ROOT --state STATE --endpoint URL [--dry-run]` reads only completed `cache-operation-v1` and existing timing records.
- Upload payload is `{schema_version: 1, records: [...]}` with at most 200 records or 1 MiB per request; operation IDs are the deduplication key.
- State is an atomic local JSON checkpoint; source records remain untouched.

- [ ] Write tests for valid scan, malformed record rejection, duplicate replay, partial source directories, request-size bounds, and bounded upload timeout/failure.
- [ ] Run relay tests and confirm they fail before implementation.
- [ ] Implement validation, deterministic ordering, checkpointing, retry-safe POST, and dry-run output without credentials.
- [ ] Run relay tests, shell syntax, `py_compile`, and `git diff --check`.
- [ ] Commit the relay change with `feat(cache): add retryable telemetry relay`.

### Task 3: HerdR ingestion and aggregate API

**Files:**
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/herdr_engineering/control_plane/repo.py`
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/herdr_engineering/control_plane/api.py`
- Create: `/home/wohlgemuth/IdeaProjects/herdr-engineering/herdr_engineering/control_plane/ci_performance.py`
- Test: `/home/wohlgemuth/IdeaProjects/herdr-engineering/tests/control_plane/test_ci_performance.py`
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/tests/control_plane/test_ci_api.py`

**Interfaces:**
- `validate_ci_telemetry(payload) -> list[dict]` accepts only schema-v1 records and returns normalized records.
- `ControlPlaneRepo.record_ci_telemetry(records) -> dict` stores idempotent `ci_telemetry` events.
- `ControlPlaneRepo.ci_performance(repository=None, branch=None, days=14) -> dict` returns cache groups, execution trend samples, coverage scope, and alert records.
- `POST /api/v1/ci/telemetry` accepts the bounded relay payload; `GET /api/v1/ci/performance` returns aggregates.

- [ ] Write tests for ingestion authentication/limits, duplicate operation IDs, invalid records, and retained scope.
- [ ] Write aggregate tests for 7/14-day windows, p50/p95 nearest-rank calculations, hit rate, minimum sample behavior, and all three alert rules.
- [ ] Run the focused API tests and confirm the new assertions fail.
- [ ] Implement normalized event persistence and read-time aggregation using existing CI identity/project annotation.
- [ ] Preserve memory-only/durable-sink and partial/stale disclosures in every response.
- [ ] Run the focused suite, existing CI API/project-link suites, Ruff, compileall, and `git diff --check`.
- [ ] Commit with `feat(ci): ingest and aggregate cache telemetry`.

### Task 4: HerdR dashboard

**Files:**
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/packages/ci-performance.js`
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/packages/ci-charts.js`
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/packages/integrations-view.js`
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/packages/herdr-web-components/ci-charts.test.js`
- Modify: `/home/wohlgemuth/IdeaProjects/herdr-engineering/tests/playwright/ci-charts.test.mjs`

**Interfaces:**
- `ciPerformanceModel` consumes the API's normalized performance payload and returns cache groups, trend points, and alerts.
- `renderCiPerformance` adds cache hit-rate cards, restore/save p50/p95 trend SVGs, and accessible alert tables while retaining existing project/language charts.

- [ ] Add failing unit/browser assertions for cache cards, trend windows, insufficient data, alert copy, filters, escaping, refresh focus, and mobile width.
- [ ] Implement the API fetch and render path with explicit loading/error/partial/stale/memory-only text.
- [ ] Keep exact values in Chart data and cap plotted groups while disclosing omitted groups.
- [ ] Run all existing client/component/browser tests plus new focused tests and `git diff --check`.
- [ ] Commit with `feat(ci): show cache health and regression trends`.

### Task 5: Pilot and delivery

**Files:**
- Modify: `/home/wohlgemuth/IdeaProjects/inferweave-rust-cache-pilot/.woodpecker/rust.yaml`
- Modify: `/home/wohlgemuth/IdeaProjects/go-modules/.woodpecker.yml` only, preserving its existing commands
- Create: `docs/performance/ci-observability-pilot-20261023/README.md`

- [ ] Add relay invocation after successful Rust registry publication and the Go modules cache save; keep failure status independent of telemetry upload.
- [ ] Validate YAML command preservation and run one cold/warm allocation for each pilot.
- [ ] Upload only sanitized summaries to the live HerdR endpoint; record request IDs, counts, and bounded timings without credentials.
- [ ] Verify live API aggregates, alerts, filter propagation, and screenshots at `http://100.104.39.6/#/ci`.
- [ ] Run the full relevant Python/Node gates and request independent review before deployment.
- [ ] Deploy through `deploy/control-plane/deploy.sh`, verify health, and document baseline versus warm results.
- [ ] Commit pilot evidence and open reviewable PRs; do not broaden to compiled target snapshots or notifications.

## Self-review

- Runner, relay, API, UI, and pilot requirements from the approved spec each have an explicit task.
- No placeholder language or unspecified function names remain; interfaces are fixed above.
- The relay and API use the same `operation_id` key, and the UI consumes the API payload rather than raw Quobyte files.
- Existing CI execution metrics remain separate from cache-operation metrics, preserving queue exclusion and successful-run semantics.
