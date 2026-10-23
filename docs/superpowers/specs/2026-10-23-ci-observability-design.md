# CI observability design

## Goal

Measure whether persistent CI caching improves real pipeline execution and make regressions visible in HerdR. The feature covers cache hit/miss behavior, restore/save cost, rolling p50/p95 execution trends, and bounded regression indicators.

## Boundaries

Pipeline execution time remains the existing start-to-finish measurement and excludes queue time. Cache measurements are separate phase records and never replace the provider's run timestamps. Successful and failed cache operations are recorded, but only validated, finite measurements participate in aggregate statistics. Missing records remain missing; they are not treated as zero.

No commands, arguments, environment values, filesystem paths, credentials, cache keys, archive members, stdout, or cache contents enter HerdR. Cache compatibility keys remain hashed and opaque. Every record is bounded by size, field length, timestamp range, and numeric limits before persistence.

## Data flow

1. `wpci-timing` continues to emit one private timing record per wrapped phase.
2. `wpci-cache` emits one sanitized cache-operation summary per restore/save operation. It includes schema version, repository and pipeline identifiers when supplied, phase, cache kind, operation, outcome (`hit`, `miss`, `cold`, `saved`, or `failed`), wall seconds, compressed bytes, expanded bytes, and entry count. A cache operation never blocks the wrapped command beyond its existing bounded maintenance timeout.
3. A host-side relay reads completed records from the Quobyte CI timing area, validates and deduplicates them, and uploads a bounded summary bundle through an existing HerdR CI ingest route. The relay is retryable and idempotent; it does not delete source evidence after upload.
4. HerdR stores summaries in the existing durable CI event stream. Memory-only deployments identify that limitation in the API and UI.
5. The API exposes validated cache observations and rolling aggregates. The UI applies existing repository, branch, period, mission, and session filters and keeps partial, stale, and retention disclosures visible.

## Aggregates and alerts

For each project and cache kind, the API computes sample count, hit rate, p50, p95, and mean restore/save seconds over selectable 7-day and 14-day windows. A group needs at least five valid samples before a percentile or alert is shown. The dashboard labels small groups as insufficient data.

The first regression rules are deterministic and visible-only:

- execution p95 is at least 2× the preceding comparable window with at least five samples in both windows;
- cache restore or save p95 reaches 90% of the configured 90-second maintenance budget;
- hit rate falls below 50% with at least ten operations.

Alerts carry project, cache kind, metric, current value, baseline value, sample counts, and the observation window. They are informational API/UI records; no email, chat, or automatic pipeline mutation is performed.

## Compatibility and failure behavior

Old timing records remain readable. Missing cache fields produce no cache aggregate. Invalid or oversized bundles are rejected as a whole with a bounded diagnostic, while other bundles continue. Duplicate bundle IDs and operation IDs are idempotent. Ingestion failure never changes the wrapped build's exit status.

## Verification

- runner unit tests cover schema bounds, redaction, timeout behavior, hit/miss classification, and idempotent operation IDs;
- relay tests cover validation, duplicate replay, partial source directories, and bounded upload failures;
- HerdR API tests cover aggregation, minimum samples, windows, stale/memory-only scope, and alert thresholds;
- browser tests cover cache cards, p50/p95 trends, insufficient-data states, filter propagation, escaping, refresh focus, and mobile overflow;
- a live pilot publishes sanitized records for InferWeave and one Go project, then compares cold/warm measurements without changing build commands.
