# Automatic discovery and freshness

Discover repos from authorized GitHub installations, configured local checkouts and explicit project manifests; never assume every repo in an organization is in scope. Initial full scan, then webhooks + incremental scans; track default branch, monorepos, submodules, generated code, and renames. Resolve ownership through manifests, package metadata, CI, deploy configs, imports and actual runtime traces. Mark inferred links separately.

Capture mission lifecycle and handoffs from existing Pi/Herdr event contracts; include task status, ownership, review outcomes, retries, blocking reasons, source and destination workers, model identities and artifacts. No polling-only agent instrumentation if native events exist.

Use OpenTelemetry trace/span IDs for cross-service request flow; correlate gateway queue wait, prefill, decode, retries, session affinity, cache, model selection and GPU placement from InferWeave. Sample high-volume spans with transparent coverage labels; never invent missing spans.

Freshness targets (initial SLO proposals): mission events visible p95 < 2s; request flow p95 < 5s for sampled spans; Git webhook changes < 30s; scheduled repository reconciliation every 15min; full dependency reconciliation daily. Make thresholds configurable and validate under load.

Disconnected hosts buffer events durably, replay with idempotency and source ordering, show offline/stale badges. Dead-letter queue and operator replay for malformed events. Backfill with checkpointing and rate-limit-aware provider adapters.
