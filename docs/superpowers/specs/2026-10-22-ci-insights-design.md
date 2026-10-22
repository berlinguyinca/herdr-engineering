# CI stability, tests and deployment pipelines

User approved all three proposed views on 2026-10-22: stability overview, test history/explorer, and wide rendered run/deployment pipeline.

Use the existing Woodpecker integration, native web components and theme. Provider execution remains authoritative. Add structured JUnit report ingestion associated with provider repository identity, branch, revision and run; retain reports and observed pipeline history through the existing durable event repository. Never infer tests from log text, assume a missing report means zero tests, count cancelled runs as successes, or infer dependencies from list order. Show retained observation scope and stale/partial states.

Overview: success rate with explicit denominator, failed/current counts, duration trends, latest reported test count and delta within each repository/branch, repository/branch/time filters. Tests: count history, failure summaries, first failure, last pass, recurrence and slow tests. Flakiness requires evidence from retries on the same revision/test identity; otherwise describe inconsistent outcomes without claiming proven flakiness.

Pipeline: full-width inspector, state labels plus colors (red failures, green passed, distinct running/queued/skipped/blocked), real workflow grouping/dependencies when reported, timings, interactive step logs, revision/provider links. Unknown dependency graphs remain explicitly grouped steps. Preserve keyboard operation; mobile vertical flow; no page overflow. No new runtime dependencies.

Approaches: retain recent-window charts alone (insufficient history); replace CI provider (unnecessary); extend existing integration and event-backed reporting (selected by user approval of proposal). Report upload is authenticated operator-only; reads retain existing viewer rules. Bound XML size/case counts and failure text, reject DTD/entities, redact secrets before storage, and ensure replay survives restarts.

Validation: parser/history/API tests with actual XML and local servers; frontend model/render tests; browser verification of wide/mobile layout, failure colors, filters, step selection and refresh behavior. Existing suites run before completion. No production deployment is implied.
